from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from app.models import Group, VirtualMachine, new_id

from test_runs import GROUP_ID, api_client, make_group, make_run

RING_ID = "55555555-5555-5555-5555-555555555555"

EXPORT_HEADER = ["application", "ring_path", "vm_resource_id", "vm_name", "display_name", "enabled", "never_stop", "notes", "azure_connection"]

RESOURCE_TEMPLATE = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{name}"


async def make_ring(session, group_id: str = RING_ID, name: str = "Ring 1", parent_id: str = GROUP_ID) -> Group:
    parent = await session.get(Group, parent_id)
    ring = Group(id=group_id, parent_id=parent_id, name=name, path=f"{parent.path}{group_id}/", depth=1, sequence=1)
    session.add(ring)
    await session.commit()
    return ring


async def make_vm(session, name: str, *, group_id: str = GROUP_ID, resource_group: str = "rg-app", enabled: bool = True, notes: str = "") -> VirtualMachine:
    resource_id = RESOURCE_TEMPLATE.format(resource_group=resource_group, name=name)
    vm = VirtualMachine(
        id=new_id(),
        group_id=group_id,
        vm_resource_id=resource_id,
        normalized_resource_id=resource_id.lower(),
        vm_name=name,
        display_name=name,
        subscription_id="12345678-1234-1234-1234-123456789abc",
        resource_group=resource_group,
        enabled=enabled,
        notes=notes,
    )
    session.add(vm)
    await session.commit()
    return vm


async def seed_mixed_case_vms(session) -> list[str]:
    """VM names whose alphabetical order only holds if the sort ignores case."""
    await make_group(session)
    await make_vm(session, "Bravo-vm", resource_group="rg-b")
    await make_vm(session, "charlie-vm", resource_group="RG-a")
    await make_vm(session, "alpha-vm", resource_group="rg-c")
    return ["alpha-vm", "Bravo-vm", "charlie-vm"]


# -- VM sorting --------------------------------------------------------


async def test_vm_sort_by_name_is_case_insensitive_and_reverses_with_direction(session) -> None:
    expected = await seed_mixed_case_vms(session)
    async with api_client(session) as client:
        ascending = await client.get("/api/vms", params={"sort": "vm_name", "direction": "asc"})
        descending = await client.get("/api/vms", params={"sort": "vm_name", "direction": "desc"})

    assert ascending.status_code == 200
    # A binary collation would put "Bravo-vm" first; func.lower() must not.
    assert [item["vm_name"] for item in ascending.json()["items"]] == expected
    assert [item["vm_name"] for item in descending.json()["items"]] == list(reversed(expected))


async def test_vm_sort_by_resource_group_is_case_insensitive(session) -> None:
    await seed_mixed_case_vms(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms", params={"sort": "resource_group", "direction": "asc"})

    assert response.status_code == 200
    assert [item["vm_name"] for item in response.json()["items"]] == ["charlie-vm", "Bravo-vm", "alpha-vm"]


async def test_unknown_vm_sort_key_falls_back_to_the_default_order(session) -> None:
    expected = await seed_mixed_case_vms(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms", params={"sort": "not_a_column"})

    assert response.status_code == 200
    assert [item["vm_name"] for item in response.json()["items"]] == expected


async def test_vm_sort_rejects_an_unknown_direction(session) -> None:
    await seed_mixed_case_vms(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms", params={"sort": "vm_name", "direction": "sideways"})

    assert response.status_code == 422


async def test_group_vm_listing_honours_sort_and_direction(session) -> None:
    expected = await seed_mixed_case_vms(session)
    async with api_client(session) as client:
        ascending = await client.get(f"/api/groups/{GROUP_ID}/vms", params={"sort": "vm_name", "direction": "asc"})
        descending = await client.get(f"/api/groups/{GROUP_ID}/vms", params={"sort": "display_name", "direction": "desc"})

    assert ascending.status_code == 200
    assert [item["vm_name"] for item in ascending.json()["items"]] == expected
    assert [item["vm_name"] for item in descending.json()["items"]] == list(reversed(expected))


async def test_group_vm_listing_filters_before_pagination(session) -> None:
    """Group-page search must cover the whole subtree, not only the current browser page."""
    await seed_export_estate(session)
    async with api_client(session) as client:
        searched = await client.get(
            f"/api/groups/{GROUP_ID}/vms",
            params={"recursive": "true", "q": "charlie", "limit": 1},
        )
        disabled = await client.get(
            f"/api/groups/{GROUP_ID}/vms",
            params={"recursive": "true", "enabled": "false", "limit": 1},
        )

    assert searched.status_code == disabled.status_code == 200
    assert searched.json()["total"] == 1
    assert [item["vm_name"] for item in searched.json()["items"]] == ["charlie-vm"]
    assert disabled.json()["total"] == 1
    assert [item["vm_name"] for item in disabled.json()["items"]] == ["charlie-vm"]


# -- run sorting -------------------------------------------------------

NEWEST_AT = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


async def seed_runs_for_sorting(session) -> None:
    # created_at deliberately runs opposite to the alphabetical status order.
    await make_run(session, "cccccccc-0000-0000-0000-000000000001", "Wave one", NEWEST_AT - timedelta(hours=2), status="succeeded")
    await make_run(session, "cccccccc-0000-0000-0000-000000000002", "Wave two", NEWEST_AT - timedelta(hours=1), status="running")
    await make_run(session, "cccccccc-0000-0000-0000-000000000003", "Wave three", NEWEST_AT, status="failed")


async def test_runs_sort_by_status_in_both_directions(session) -> None:
    await seed_runs_for_sorting(session)
    async with api_client(session) as client:
        ascending = await client.get("/api/runs", params={"sort": "status", "direction": "asc"})
        descending = await client.get("/api/runs", params={"sort": "status", "direction": "desc"})

    assert ascending.status_code == 200
    assert [item["status"] for item in ascending.json()["items"]] == ["failed", "running", "succeeded"]
    assert [item["status"] for item in descending.json()["items"]] == ["succeeded", "running", "failed"]


async def test_runs_without_a_sort_key_stay_newest_first(session) -> None:
    await seed_runs_for_sorting(session)
    async with api_client(session) as client:
        response = await client.get("/api/runs")

    assert response.status_code == 200
    assert [item["schedule_name"] for item in response.json()["items"]] == ["Wave three", "Wave two", "Wave one"]


# -- CSV export --------------------------------------------------------


def read_csv(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body)))


async def seed_export_estate(session) -> None:
    """One application holding a direct VM plus a ring with two more."""
    await make_group(session)
    await make_ring(session)
    await make_vm(session, "alpha-vm", notes="first")
    await make_vm(session, "bravo-vm", group_id=RING_ID)
    await make_vm(session, "charlie-vm", group_id=RING_ID, enabled=False)


async def test_vm_export_is_an_attachment_with_the_importer_header(session) -> None:
    await seed_export_estate(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"].startswith('attachment; filename="azure-vm-scheduler-vms-')
    assert read_csv(response.text)[0] == EXPORT_HEADER


async def test_vm_export_returns_every_vm_with_application_ring_and_enabled_columns(session) -> None:
    await seed_export_estate(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms/export.csv")

    rows = read_csv(response.text)[1:]
    by_name = {row[3]: row for row in rows}
    # The export ignores paging entirely, so every seeded VM is present.
    assert set(by_name) == {"alpha-vm", "bravo-vm", "charlie-vm"}
    assert by_name["alpha-vm"][0] == "Payments"
    assert by_name["alpha-vm"][1] == ""  # sits directly on the application
    assert by_name["alpha-vm"][5] == "true"
    assert by_name["alpha-vm"][6] == "false"  # never_stop defaults off
    assert by_name["alpha-vm"][7] == "first"
    assert by_name["bravo-vm"][:2] == ["Payments", "Ring 1"]
    assert by_name["charlie-vm"][5] == "false"


async def test_vm_export_applies_the_enabled_filter(session) -> None:
    await seed_export_estate(session)
    async with api_client(session) as client:
        response = await client.get("/api/vms/export.csv", params={"enabled": "false"})

    rows = read_csv(response.text)[1:]
    assert [row[3] for row in rows] == ["charlie-vm"]


async def test_vm_export_without_recursion_only_includes_direct_members(session) -> None:
    await seed_export_estate(session)
    async with api_client(session) as client:
        shallow = await client.get("/api/vms/export.csv", params={"group_id": GROUP_ID, "recursive": "false"})
        deep = await client.get("/api/vms/export.csv", params={"group_id": GROUP_ID, "recursive": "true"})

    assert [row[3] for row in read_csv(shallow.text)[1:]] == ["alpha-vm"]
    assert [row[3] for row in read_csv(deep.text)[1:]] == ["alpha-vm", "bravo-vm", "charlie-vm"]
