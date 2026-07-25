"""Stop waves: per-action resolution, protection, independent gates and the conflict guard."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.azure import MockVmAdapter, stop_operation
from app.hierarchy import effective_schedule, is_stop_protected, load_schedule_index, load_tree, resolve_schedule_vms
from app.models import VmAttempt, new_id
from app.scheduling import create_run

from test_hierarchy import make_group, make_schedule, make_vm


async def test_start_and_stop_resolve_independently(session) -> None:
    """The same ring can carry both a start and a stop wave without either shadowing the other."""
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-1")

    start = await make_schedule(session, "group", ring.id, name="up", action="start")
    stop = await make_schedule(session, "group", ring.id, name="down", action="stop")

    tree = await load_tree(session)
    index = await load_schedule_index(session)
    assert effective_schedule(index, tree, vm, "start").id == start.id
    assert effective_schedule(index, tree, vm, "stop").id == stop.id
    assert [item.id for item in await resolve_schedule_vms(session, start)] == [vm.id]
    assert [item.id for item in await resolve_schedule_vms(session, stop)] == [vm.id]


async def test_a_nearer_stop_schedule_shadows_an_ancestor_stop_but_not_a_start(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-1")

    app_stop = await make_schedule(session, "group", application.id, name="app down", action="stop")
    app_start = await make_schedule(session, "group", application.id, name="app up", action="start")
    ring_stop = await make_schedule(session, "group", ring.id, name="ring down", action="stop")

    tree = await load_tree(session)
    index = await load_schedule_index(session)
    # The deeper stop wins, and the ancestor start is untouched by it.
    assert effective_schedule(index, tree, vm, "stop").id == ring_stop.id
    assert effective_schedule(index, tree, vm, "start").id == app_start.id
    assert await resolve_schedule_vms(session, app_stop) == []


async def test_never_stop_shields_a_vm_from_stop_waves_only(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    protected = await make_vm(session, ring, "vm-critical", never_stop=True)
    ordinary = await make_vm(session, ring, "vm-ordinary")

    stop = await make_schedule(session, "group", ring.id, action="stop")
    start = await make_schedule(session, "group", ring.id, action="start")

    tree = await load_tree(session)
    assert is_stop_protected(tree, protected) is True
    assert is_stop_protected(tree, ordinary) is False
    assert [item.id for item in await resolve_schedule_vms(session, stop)] == [ordinary.id]
    # A protected machine is still started normally; protection is about outages, not uptime.
    assert {item.id for item in await resolve_schedule_vms(session, start)} == {protected.id, ordinary.id}


async def test_never_stop_is_inherited_from_any_ancestor_group(session) -> None:
    application = await make_group(session, "Payments", never_stop=True)
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-1")

    stop = await make_schedule(session, "group", ring.id, action="stop")

    tree = await load_tree(session)
    assert is_stop_protected(tree, vm) is True
    assert await resolve_schedule_vms(session, stop) == []


async def test_a_vm_targeted_directly_by_a_stop_is_still_protected(session) -> None:
    """Protection must not be bypassable by pointing a schedule straight at the machine."""
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-1", never_stop=True)

    stop = await make_schedule(session, "vm", vm.id, action="stop")

    assert await resolve_schedule_vms(session, stop) == []


async def test_rings_unwind_in_reverse_for_stops(session) -> None:
    application = await make_group(session, "Payments")
    first = await make_group(session, "Ring 1", application)
    second = await make_group(session, "Ring 2", application)
    await make_vm(session, first, "vm-a")
    await make_vm(session, second, "vm-b")

    forward = await make_schedule(session, "group", application.id, name="up", action="start")
    unwind = await make_schedule(session, "group", application.id, name="down", action="stop", ring_order="reverse")

    # Starts roll forward through the rings; stops unwind the last ring first.
    assert [item.vm_name for item in await resolve_schedule_vms(session, forward)] == ["vm-a", "vm-b"]
    assert [item.vm_name for item in await resolve_schedule_vms(session, unwind)] == ["vm-b", "vm-a"]

    unwind.ring_order = "sequence"
    await session.commit()
    assert [item.vm_name for item in await resolve_schedule_vms(session, unwind)] == ["vm-a", "vm-b"]


async def test_a_run_stamps_the_action_onto_every_attempt(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    await make_vm(session, ring, "vm-1")

    stop = await make_schedule(session, "group", ring.id, action="stop", stop_mode="power_off")
    run = await create_run(session, stop, trigger="manual")

    assert (run.action, run.stop_mode) == ("stop", "power_off")
    attempts = list((await session.scalars(select(VmAttempt))).all())
    assert [(item.action, item.stop_mode) for item in attempts] == [("stop", "power_off")]


# -- the conflict guard --------------------------------------------------


async def test_a_stop_is_skipped_while_a_start_for_the_same_vm_is_in_flight(session, monkeypatch) -> None:
    """A start and a stop racing over one machine would leave it in whichever state landed last."""
    from app import scheduling

    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-1")

    start = await make_schedule(session, "group", ring.id, name="up", action="start")
    stop = await make_schedule(session, "group", ring.id, name="down", action="stop")
    await create_run(session, start, trigger="manual")
    stop_run = await create_run(session, stop, trigger="manual")

    stop_attempt = await session.scalar(select(VmAttempt).where(VmAttempt.run_id == stop_run.id))
    assert stop_attempt.vm_id == vm.id

    service = scheduling.SchedulerService()
    monkeypatch.setattr(scheduling, "SessionLocal", _session_factory(session))
    await service._submit_start(stop_attempt.id)

    await session.refresh(stop_attempt)
    assert stop_attempt.status == "skipped"
    assert "start for this virtual machine is still in flight" in stop_attempt.message


def _session_factory(session):
    """Hand the scheduler the test session without letting it close the shared connection."""

    class _Factory:
        def __call__(self):
            return _Ctx()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    return _Factory()


# -- gates ---------------------------------------------------------------


async def test_enabling_real_starts_does_not_enable_real_stops(monkeypatch) -> None:
    """The two gates are deliberately independent: a wrong stop is an outage."""
    from app.azure import action_allowed
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_REAL_AZURE_STARTS", "true")
    settings = get_settings()
    assert settings.enable_real_azure_starts is True
    assert settings.enable_real_azure_stops is False
    get_settings.cache_clear()

    connection = {"allow_vm_start": True, "allow_vm_stop": False, "read_only": False}
    assert action_allowed(connection, "start") is True
    assert action_allowed(connection, "stop") is False


async def test_a_read_only_or_disabled_connection_can_never_stop() -> None:
    from app.azure import action_allowed

    permissive = {"allow_vm_start": True, "allow_vm_stop": True}
    assert action_allowed({**permissive, "read_only": True}, "stop") is False
    assert action_allowed({**permissive, "disabled": True}, "stop") is False
    assert action_allowed(None, "stop") is False
    assert action_allowed(permissive, "stop") is True


async def test_revoking_a_tenants_rights_takes_effect_immediately(monkeypatch) -> None:
    """A cached adapter must never outlive the permission decision it was built under.

    Withdrawing rights is how an operator halts a wave, so it has to bite on the very next attempt
    rather than whenever a cache happens to expire.
    """
    from app import azure, scheduling
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_REAL_AZURE_STARTS", "true")
    monkeypatch.setenv("ENABLE_REAL_AZURE_STOPS", "true")

    policy = {"id": "c1", "allow_vm_start": True, "allow_vm_stop": True, "read_only": False, "disabled": False}
    monkeypatch.setattr(azure, "connection_policy", lambda _id: _async(policy))
    monkeypatch.setattr(azure, "get_connection", lambda _id: _async(policy))
    monkeypatch.setattr(azure, "arm_token", lambda _connection: _async(("token", None)))

    service = scheduling.SchedulerService()
    _, _, mode = await service._adapter("c1", "stop")
    assert mode == "real"

    # The operator flips the tenant to read-only; the very next attempt must refuse.
    policy["read_only"] = True
    with pytest.raises(ValueError, match="read-only"):
        await service._adapter("c1", "stop")

    policy["read_only"] = False
    policy["allow_vm_stop"] = False
    with pytest.raises(ValueError, match="does not allow VM stops"):
        await service._adapter("c1", "stop")
    # Starts are a separate grant and are untouched by withdrawing the stop permission.
    _, _, mode = await service._adapter("c1", "start")
    assert mode == "real"

    get_settings.cache_clear()


async def test_a_connection_predating_a_permission_flag_is_treated_as_denied(tmp_path, monkeypatch) -> None:
    """A record written before allow_vm_stop existed omits it; a missing gate must mean 'no'."""
    import json

    from app import connections

    store = tmp_path / "azure_connections.json"
    monkeypatch.setattr(connections, "_paths", lambda: (store, tmp_path / "secret.key"))
    store.write_text(json.dumps([{
        "id": "legacy", "display_name": "Old tenant", "auth_method": "azure_cli",
        "allow_vm_start": True, "is_default": True,
    }]), encoding="utf-8")

    policy = await connections.connection_policy("legacy")
    assert policy["allow_vm_stop"] is False
    assert policy["read_only"] is False
    # The public payload must not omit the field either, or the UI reads undefined.
    public = (await connections.list_connections(public=True))[0]
    assert public["allow_vm_stop"] is False

    from app.azure import action_allowed

    assert action_allowed(policy, "start") is True
    assert action_allowed(policy, "stop") is False


def _async(value):
    async def _wrapped():
        return value

    return _wrapped()


# -- adapters ------------------------------------------------------------


async def test_mock_adapter_reports_the_state_each_stop_mode_settles_on() -> None:
    adapter = MockVmAdapter()
    resource_id = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"

    await adapter.stop_vm(resource_id, "deallocate")
    assert "deallocated" in await adapter.wait_until_stopped(resource_id, "deallocate")

    await adapter.stop_vm(resource_id, "power_off")
    assert "stopped" in await adapter.wait_until_stopped(resource_id, "power_off")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("deallocate", "deallocate"), ("power_off", "powerOff")],
)
def test_the_stop_mode_picks_the_right_arm_operation(mode: str, expected: str) -> None:
    assert stop_operation(mode) == expected
