from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf at runtime
    from .models import SecurityPolicy


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


def resolve_default_timezone(policy: SecurityPolicy | None = None) -> str:
    """The configured default zone, falling back through policy, environment, then UTC.

    Lives here rather than in the scheduler so that reading a timezone does not require importing
    the scheduler, which drags in the Azure adapters and the firewall behind it.
    """
    for candidate in ((policy.default_timezone if policy else None), get_settings().default_timezone):
        try:
            return validate_timezone(candidate or "")
        except ValueError:
            continue
    return "UTC"
