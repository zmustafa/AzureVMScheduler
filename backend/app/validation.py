from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VM_RESOURCE_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/resourceGroups/(?P<resource_group>[^/]+)/providers/Microsoft\.Compute/virtualMachines/(?P<vm_name>[^/]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VmResource:
    subscription_id: str
    resource_group: str
    vm_name: str


def parse_vm_resource_id(value: str) -> VmResource:
    match = VM_RESOURCE_ID.fullmatch(value.strip())
    if not match:
        raise ValueError("Expected /subscriptions/{uuid}/resourceGroups/{name}/providers/Microsoft.Compute/virtualMachines/{name}")
    parts = match.groupdict()
    if any(not item.strip() for item in parts.values()):
        raise ValueError("VM resource ID contains an empty segment")
    return VmResource(parts["subscription"].lower(), parts["resource_group"], parts["vm_name"])


def normalize_resource_id(value: str) -> str:
    return value.strip().lower()


SUBSCRIPTION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_subscription_id(value: str) -> str:
    candidate = (value or "").strip()
    if not SUBSCRIPTION_ID.fullmatch(candidate):
        raise ValueError("subscription_id must be a valid UUID")
    return candidate.lower()


def validate_timezone(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc
    return name
