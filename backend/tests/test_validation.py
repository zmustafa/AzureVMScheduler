import pytest

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
