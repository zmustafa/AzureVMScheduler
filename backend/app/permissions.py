"""The capability catalog and the built-in system roles.

Permissions are coarse capability strings checked by ``require_permission`` in the API. Roles bundle
permissions; access groups bundle roles. A user's effective permissions are the union across their
directly-assigned roles and the roles of every access group they belong to.

The catalog is ordered into sections mirroring the product's navigation so the role editor can
render readable groups. Adding a feature? Add its permission here and gate the route with
``require_permission`` so the capability both appears in the editor and is actually enforced.

Note on wording: ``groups.read``/``groups.write`` govern the **application and ring hierarchy**, not
user groups. The strings are kept as-is because they are load-bearing across every route, but the
labels below say "applications and rings" so the UI is never ambiguous.
"""

from __future__ import annotations


PERMISSION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Overview", [
        ("dashboard.read", "View the overview dashboard and readiness checks"),
    ]),
    ("Estate", [
        ("groups.read", "View applications and rings"),
        ("groups.write", "Create, rename, move, and delete applications and rings"),
        ("vms.read", "View the virtual machine inventory"),
        ("vms.write", "Add, edit, move, and delete virtual machines"),
    ]),
    ("Scheduling", [
        ("schedules.read", "View schedules, the timeline, and upcoming waves"),
        ("schedules.write", "Create and edit schedules, run waves on demand, and start or stop machines"),
    ]),
    ("Operations", [
        ("runs.read", "View run history, attempts, and the activity log"),
        ("imports.write", "Import inventory and schedules from CSV"),
    ]),
    ("Integrations", [
        ("connectors.read", "View notification connectors"),
        ("connectors.manage", "Create, edit, and test notification connectors"),
        ("notifications.read", "View notification rules, events, and deliveries"),
        ("notifications.manage", "Create and edit notification rules"),
    ]),
    ("Administration", [
        ("settings.read", "View application settings"),
        ("settings.write", "Change application settings and the default timezone"),
        ("connections.manage", "Manage Azure tenant connections and their safety gates"),
        ("users.manage", "Manage users, roles, access groups, identity providers, and sessions"),
        ("audit.read", "View the audit log"),
        ("backup.manage", "Export and import the settings document, and reset the estate"),
    ]),
]

#: Flat capability -> label lookup. Canonical for API guards and role validation.
PERMISSIONS: dict[str, str] = {key: label for _section, items in PERMISSION_GROUPS for key, label in items}

ALL_PERMISSIONS: list[str] = list(PERMISSIONS)

#: Capabilities reserved for full administrators.
_ADMIN_ONLY: set[str] = {"settings.write", "connections.manage", "users.manage", "audit.read", "backup.manage"}

# These three lists reproduce the historical ROLE_PERMISSIONS exactly and are pinned by a test.
# Grant a seeded role something new only deliberately: it changes what existing users can do.
_OPERATOR: list[str] = [
    "dashboard.read", "groups.read", "groups.write", "vms.read", "vms.write",
    "schedules.read", "schedules.write", "runs.read", "imports.write",
    "connectors.read", "notifications.read", "notifications.manage",
]
_AUDITOR: list[str] = [
    "dashboard.read", "groups.read", "vms.read", "schedules.read", "runs.read",
    "audit.read", "connectors.read", "notifications.read",
]
_VIEWER: list[str] = [
    "dashboard.read", "groups.read", "vms.read", "schedules.read", "runs.read",
    "connectors.read", "notifications.read",
]

#: (name, description, permissions). Seeded on startup; these roles can never be deleted.
#: The permission sets reproduce the historical ROLE_PERMISSIONS exactly, so the move to
#: role rows does not silently grant or revoke anything.
SYSTEM_ROLES: list[tuple[str, str, list[str]]] = [
    ("admin", "Full administrator — every permission, including access control.", ["*"]),
    ("operator", "Runs the estate day to day: schedules, waves, inventory and imports, but not security or settings.", _OPERATOR),
    ("auditor", "Read-only oversight across the product, plus the audit log.", _AUDITOR),
    ("viewer", "Read-only view of the estate, schedules, and run history.", _VIEWER),
    ("noaccess", "Blocked from the whole application. The safe default for auto-provisioned SSO users until an administrator grants a real role.", []),
]

SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(name for name, _description, _permissions in SYSTEM_ROLES)

#: A user whose only role is this — or who holds no role at all — has zero permissions and is
#: blocked server-side from every path except the minimal self/sign-out allowlist in ``auth``.
NO_ACCESS_ROLE = "noaccess"

#: Highest wins when caching a user's single display role on ``User.role``.
_ROLE_RANK = {"noaccess": -1, "viewer": 0, "auditor": 1, "operator": 2, "admin": 3}


def role_rank(name: str) -> int:
    return _ROLE_RANK.get(name, 0)


def expand(permissions: list[str] | set[str]) -> set[str]:
    """Resolve a stored permission list, honouring the ``*`` wildcard."""
    granted = set(permissions)
    return set(ALL_PERMISSIONS) | {"*"} if "*" in granted else granted


def unknown_permissions(permissions: list[str]) -> list[str]:
    """Permissions that are not in the catalog, so a typo cannot be saved silently."""
    return [item for item in permissions if item != "*" and item not in PERMISSIONS]
