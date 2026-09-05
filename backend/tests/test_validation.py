import pytest
from pydantic import ValidationError

from app.schemas import ConnectionInput, GroupInput, GroupPatch, SchedulePatch, UserUpdate, VmBulkAdd, VmPatch
from app.validation import parse_vm_resource_id


VALID = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-01"


def test_parse_vm_resource_id() -> None:
    parsed = parse_vm_resource_id(VALID)
    assert parsed.subscription_id == "12345678-1234-1234-1234-123456789abc"
    assert parsed.resource_group == "rg-app"
    assert parsed.vm_name == "vm-01"


@pytest.mark.parametrize("value", ["", "/subscriptions/nope/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm", VALID + "/extensions/x", VALID.replace("virtualMachines", "disks")])
def test_rejects_invalid_vm_resource_ids(value: str) -> None:
    with pytest.raises(ValueError):
        parse_vm_resource_id(value)


@pytest.mark.parametrize(
    ("schema", "field"),
    [
        (UserUpdate, "disabled"),
        (GroupPatch, "description"),
        (GroupPatch, "enabled"),
        (VmPatch, "group_id"),
        (VmPatch, "notes"),
        (SchedulePatch, "action"),
        (SchedulePatch, "enabled"),
        (SchedulePatch, "notes"),
    ],
)
def test_patch_fields_backed_by_non_null_columns_reject_explicit_null(schema, field: str) -> None:
    with pytest.raises(ValidationError, match="field may not be null"):
        schema.model_validate({field: None})


def test_free_text_and_connection_credentials_are_bounded() -> None:
    with pytest.raises(ValidationError):
        GroupInput(name="Application", description="x" * 4001)
    with pytest.raises(ValidationError):
        VmBulkAdd(vm_resource_ids=[VALID], notes="x" * 4001)
    with pytest.raises(ValidationError):
        SchedulePatch(notes="x" * 4001)
    with pytest.raises(ValidationError):
        ConnectionInput(display_name="Tenant", client_secret="x" * 4001)
    with pytest.raises(ValidationError):
        ConnectionInput(display_name="Tenant", certificate_pem="x" * 100_001)
