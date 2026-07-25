from unittest.mock import AsyncMock, patch

import pytest

from app.csv_import import MAX_GROUP_NAME, parse_ring_path, validate_csv


VM = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
VM2 = VM.replace("vm1", "vm2")
CONNECTIONS = [{"id": "connection-1", "display_name": "Default", "is_default": True, "disabled": False}]


@pytest.mark.asyncio
async def test_csv_preview_normalizes_valid_row() -> None:
    content = f"schedule_type,start_time,vm_resource_id,timezone,enabled\ndaily,08:30,{VM},UTC,yes\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["valid"] == 1
    assert result["rows"][0]["data"]["enabled"] is True
    assert result["rows"][0]["data"]["azure_connection_id"] == "connection-1"


@pytest.mark.asyncio
async def test_csv_reports_row_errors() -> None:
    content = b"schedule_type,start_time,vm_resource_id\ndaily,not-a-time,not-an-id\n"
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 1
    assert len(result["rows"][0]["errors"]) == 2


@pytest.mark.asyncio
async def test_csv_requires_columns() -> None:
    # schedule_type marks the legacy format, so the missing schedule columns are reported as such.
    with pytest.raises(ValueError, match="Missing required columns"):
        await validate_csv(b"schedule_type,name\ndaily,test\n")


@pytest.mark.asyncio
async def test_csv_rejects_duplicate_schedule_rows() -> None:
    content = f"schedule_type,start_time,vm_resource_id\ndaily,08:30,{VM}\ndaily,08:30,{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["valid"] == 1
    assert result["invalid"] == 1
    assert "Duplicate schedule" in result["rows"][1]["errors"][0]


@pytest.mark.asyncio
async def test_csv_imports_stop_schedules() -> None:
    content = f"schedule_type,start_time,vm_resource_id,action,stop_mode,ring_order\ndaily,19:00,{VM},stop,power_off,reverse\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["valid"] == 1
    data = result["rows"][0]["data"]
    assert (data["action"], data["stop_mode"], data["ring_order"]) == ("stop", "power_off", "reverse")


@pytest.mark.asyncio
async def test_csv_defaults_omitted_action_columns_to_a_start() -> None:
    content = f"schedule_type,start_time,vm_resource_id\ndaily,08:30,{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    data = result["rows"][0]["data"]
    assert (data["action"], data["stop_mode"], data["ring_order"]) == ("start", "deallocate", "sequence")


@pytest.mark.asyncio
async def test_csv_rejects_an_unknown_action() -> None:
    content = f"schedule_type,start_time,vm_resource_id,action\ndaily,08:30,{VM},pause\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 1
    assert "action must be start or stop" in result["rows"][0]["errors"][0]


@pytest.mark.asyncio
async def test_a_start_and_a_stop_on_one_vm_are_not_duplicates() -> None:
    """Same VM, same time, opposite actions: two legitimate rows, not a collision."""
    content = f"schedule_type,start_time,vm_resource_id,action\ndaily,08:30,{VM},start\ndaily,08:30,{VM},stop\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["valid"] == 2
    assert result["invalid"] == 0


@pytest.mark.asyncio
async def test_inventory_csv_carries_never_stop() -> None:
    content = f"application,vm_resource_id,never_stop\nPayments,{VM},true\nPayments,{VM2},\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["format"] == "inventory"
    assert [row["data"]["never_stop"] for row in result["rows"]] == [True, False]


def test_ring_path_accepts_at_most_one_ring() -> None:
    assert parse_ring_path("") == []
    assert parse_ring_path("ring1") == ["ring1"]
    assert parse_ring_path("  ring1  ") == ["ring1"]

    with pytest.raises(ValueError, match="rings cannot contain other rings"):
        parse_ring_path("ring2/batch")


def test_ring_path_rejects_an_over_length_ring_name() -> None:
    assert parse_ring_path("R" * MAX_GROUP_NAME) == ["R" * MAX_GROUP_NAME]

    with pytest.raises(ValueError, match="ring names cannot exceed 200 characters"):
        parse_ring_path("R" * (MAX_GROUP_NAME + 1))


@pytest.mark.asyncio
async def test_inventory_csv_rejects_an_over_length_application_name() -> None:
    content = f"application,vm_resource_id\n{'A' * (MAX_GROUP_NAME + 1)},{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 1
    assert any("application names cannot exceed 200 characters" in error for error in result["rows"][0]["errors"])


@pytest.mark.asyncio
async def test_inventory_csv_accepts_an_application_name_at_the_limit() -> None:
    content = f"application,vm_resource_id\n{'A' * MAX_GROUP_NAME},{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 0
    assert result["rows"][0]["data"]["application"] == "A" * MAX_GROUP_NAME


@pytest.mark.asyncio
async def test_inventory_csv_rejects_an_over_length_ring_path() -> None:
    content = f"application,ring_path,vm_resource_id\nPayments,{'R' * (MAX_GROUP_NAME + 1)},{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 1
    assert any("ring names cannot exceed 200 characters" in error for error in result["rows"][0]["errors"])


@pytest.mark.asyncio
async def test_inventory_csv_plans_applications_and_rings() -> None:
    content = (
        "application,ring_path,vm_resource_id,display_name,enabled,notes\n"
        f"Payments,Ring 1,{VM},Web,yes,first\n"
        f"Payments,Ring 2,{VM2},Api,no,second\n"
    ).encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["format"] == "inventory"
    assert result["valid"] == 2
    assert result["applications_to_create"] == 1
    assert result["rings_to_create"] == 2
    assert result["vms_to_create"] == 2
    assert result["rows"][1]["data"]["enabled"] is False
    assert {item["path"] for item in result["groups_to_create"]} == {"Payments", "Payments / Ring 1", "Payments / Ring 2"}


@pytest.mark.asyncio
async def test_inventory_csv_rejects_a_nested_ring_path() -> None:
    content = f"application,ring_path,vm_resource_id\nPayments,Ring 1/Ring 1a,{VM}\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 1
    assert any("rings cannot contain other rings" in error for error in result["rows"][0]["errors"])


@pytest.mark.asyncio
async def test_inventory_csv_reports_row_errors() -> None:
    content = (
        "application,ring_path,vm_resource_id\n"
        f",Ring 1,{VM}\n"
        f"Payments,Ring 1//Ring 2,{VM}\n"
        "Payments,,not-a-resource-id\n"
    ).encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 3
    assert any("application is required" in error for error in result["rows"][0]["errors"])
    assert any("empty segments" in error for error in result["rows"][1]["errors"])
    assert any("Duplicate VM" in error for error in result["rows"][1]["errors"])


@pytest.mark.asyncio
async def test_inventory_csv_rejects_unknown_columns() -> None:
    with pytest.raises(ValueError, match="Unknown columns"):
        await validate_csv(b"application,vm_resource_id,region\nPayments,x,eu\n")


def _candidate(name: str, resource_group: str = "rg", subscription: str = "12345678-1234-1234-1234-123456789abc") -> dict[str, str]:
    return {
        "id": f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{name}",
        "name": name,
        "resource_group": resource_group,
        "subscription_id": subscription,
        "location": "eastus",
    }


@pytest.mark.asyncio
async def test_name_only_csv_resolves_against_azure() -> None:
    content = b"application,ring_path,vm_name\nPayments,ring1,vm-web-01\nPayments,ring1,VM-WEB-02\n"
    resolver = AsyncMock(return_value=([_candidate("vm-web-01"), _candidate("vm-web-02")], "resource_graph"))
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)), \
         patch("app.connections.get_connection", new=AsyncMock(return_value={"id": "connection-1", "disabled": False})), \
         patch("app.azure.resolve_vm_names", new=resolver):
        result = await validate_csv(content, connection_id="connection-1")
    assert result["invalid"] == 0
    assert result["resolved_from_names"] == 2
    assert result["rows"][0]["resolved_from_name"] is True
    assert result["rows"][0]["data"]["vm_resource_id"].endswith("/vm-web-01")
    # Matching is case-insensitive and the resolving tenant is carried onto the row.
    assert result["rows"][1]["data"]["vm_resource_id"].endswith("/vm-web-02")
    assert result["rows"][1]["data"]["azure_connection_id"] == "connection-1"
    # One batched lookup covers the whole file.
    assert resolver.await_count == 1
    assert sorted(resolver.await_args.args[1]) == ["VM-WEB-02", "vm-web-01"]


@pytest.mark.asyncio
async def test_name_only_csv_needs_a_tenant() -> None:
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(b"application,vm_name\nPayments,vm-web-01\n")
    assert result["invalid"] == 1
    assert any("Azure tenant" in error for error in result["rows"][0]["errors"])


@pytest.mark.asyncio
async def test_name_only_csv_flags_missing_and_ambiguous_names() -> None:
    content = b"application,vm_name\nPayments,vm-shared\nPayments,vm-ghost\n"
    candidates = [_candidate("vm-shared", "rg-a"), _candidate("vm-shared", "rg-b", "22345678-1234-1234-1234-123456789abc")]
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)), \
         patch("app.connections.get_connection", new=AsyncMock(return_value={"id": "connection-1", "disabled": False})), \
         patch("app.azure.resolve_vm_names", new=AsyncMock(return_value=(candidates, "resource_graph"))):
        result = await validate_csv(content, connection_id="connection-1")
    assert result["invalid"] == 2
    assert any("exists in 2 places" in error for error in result["rows"][0]["errors"])
    assert any("No virtual machine named 'vm-ghost'" in error for error in result["rows"][1]["errors"])


@pytest.mark.asyncio
async def test_default_destination_replaces_the_application_column() -> None:
    content = b"vm_name\nvm-web-01\n"
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)), \
         patch("app.connections.get_connection", new=AsyncMock(return_value={"id": "connection-1", "disabled": False})), \
         patch("app.azure.resolve_vm_names", new=AsyncMock(return_value=([_candidate("vm-web-01")], "resource_graph"))):
        result = await validate_csv(content, connection_id="connection-1", default_path=["ABC app", "ring1"])
    assert result["invalid"] == 0
    assert result["rows"][0]["data"]["application"] == "ABC app"
    assert result["rows"][0]["data"]["ring_path"] == "ring1"
    assert result["rows"][0]["data"]["display_name"] == "vm-web-01"


@pytest.mark.asyncio
async def test_row_may_still_supply_a_full_resource_id() -> None:
    content = f"application,vm_resource_id,vm_name\nPayments,{VM},ignored-name\n".encode()
    with patch("app.csv_import.list_connections", new=AsyncMock(return_value=CONNECTIONS)):
        result = await validate_csv(content)
    assert result["invalid"] == 0
    assert result["resolved_from_names"] == 0
    assert result["rows"][0]["data"]["vm_resource_id"] == VM
