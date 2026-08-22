"""Group tree, VM inventory, and schedule/connection resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Group, Schedule, VirtualMachine, new_id


# Applications sit at depth 0 and hold rings at depth 1. Rings never nest.
MAX_DEPTH = 1
# Matches the Group.name column; SQLite does not enforce VARCHAR limits itself.
MAX_GROUP_NAME = 200


def path_ids(path: str) -> list[str]:
    return [item for item in (path or "").split("/") if item]


def child_path(parent: Group | None, group_id: str) -> str:
    return f"{parent.path if parent else '/'}{group_id}/"


def group_kind(depth: int) -> str:
    return "application" if depth == 0 else "ring"


@dataclass
class GroupTree:
    """An in-memory snapshot of the group hierarchy for the life of one request.

    Every derived lookup is memoized. ``chain`` alone backs ``name_path``, ``is_active``,
    ``is_stop_protected``, ``effective_connection_id`` and the wave ordering key, so on a listing
    it is called several times per row; recomputing it from the path string each time made the
    overview quadratic in the size of the estate.
    """

    by_id: dict[str, Group] = field(default_factory=dict)
    _chains: dict[str, list[Group]] = field(default_factory=dict, repr=False, compare=False)
    _subtrees: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)
    _children: dict[str | None, list[Group]] | None = field(default=None, repr=False, compare=False)

    def get(self, group_id: str | None) -> Group | None:
        return self.by_id.get(group_id or "")

    def children_of(self, group_id: str | None) -> list[Group]:
        if self._children is None:
            index: dict[str | None, list[Group]] = {}
            for node in self.by_id.values():
                index.setdefault(node.parent_id, []).append(node)
            self._children = index
        return self._children.get(group_id, [])

    def chain(self, group_id: str | None) -> list[Group]:
        """Nearest-first chain: [self, parent, ..., root]."""
        node = self.get(group_id)
        if not node:
            return []
        cached = self._chains.get(node.id)
        if cached is None:
            cached = [self.by_id[item] for item in reversed(path_ids(node.path)) if item in self.by_id]
            self._chains[node.id] = cached
        return cached

    def name_path(self, group_id: str | None) -> str:
        return " / ".join(node.name for node in reversed(self.chain(group_id)))

    def is_active(self, group_id: str | None) -> bool:
        chain = self.chain(group_id)
        return bool(chain) and all(node.enabled for node in chain)

    def subtree_ids(self, group_id: str) -> set[str]:
        root = self.get(group_id)
        if not root:
            return set()
        cached = self._subtrees.get(root.id)
        if cached is None:
            # Walked through the child index rather than scanning every node, so listing a whole
            # tree costs one pass in total instead of one pass per group.
            found = {root.id}
            pending = [root.id]
            while pending:
                for child in self.children_of(pending.pop()):
                    if child.id not in found:
                        found.add(child.id)
                        pending.append(child.id)
            cached = found
            self._subtrees[root.id] = cached
        return cached


async def load_tree(db: AsyncSession) -> GroupTree:
    groups = (await db.scalars(select(Group))).all()
    return GroupTree({item.id: item for item in groups})


async def assert_unique_sibling_name(db: AsyncSession, parent_id: str | None, name: str, exclude_id: str | None = None) -> None:
    statement = select(Group.id).where(func.lower(Group.name) == name.strip().lower())
    statement = statement.where(Group.parent_id.is_(None)) if parent_id is None else statement.where(Group.parent_id == parent_id)
    if exclude_id:
        statement = statement.where(Group.id != exclude_id)
    if await db.scalar(statement.limit(1)):
        raise ValueError("A group with this name already exists at the same level")


async def recompute_subtree(db: AsyncSession, group: Group) -> None:
    """Refresh path/depth for a node and everything beneath it."""
    parent = await db.get(Group, group.parent_id) if group.parent_id else None
    if parent and parent.id == group.id:
        raise ValueError("A group cannot be its own parent")
    group.path = child_path(parent, group.id)
    group.depth = parent.depth + 1 if parent else 0
    pending = [group]
    visited = {group.id}
    deepest = group.depth
    while pending:
        node = pending.pop()
        children = (await db.scalars(select(Group).where(Group.parent_id == node.id))).all()
        for child in children:
            if child.id in visited:
                raise ValueError("Group hierarchy contains a cycle")
            visited.add(child.id)
            child.path = child_path(node, child.id)
            child.depth = node.depth + 1
            deepest = max(deepest, child.depth)
            pending.append(child)
    if deepest > MAX_DEPTH:
        raise ValueError("A ring cannot contain another ring")


def assert_parent_allowed(parent: Group | None) -> None:
    """Only applications may hold children, so a ring can never gain one."""
    if parent is not None and parent.depth >= MAX_DEPTH:
        raise ValueError("A ring cannot contain another ring — add the ring to its application instead")


def assert_move_allowed(tree: GroupTree, group_id: str, new_parent_id: str | None) -> None:
    # Promoting a ring to a top-level application is always fine.
    if new_parent_id is None:
        return
    if new_parent_id == group_id:
        raise ValueError("A group cannot be moved into itself")
    target = tree.get(new_parent_id)
    if not target:
        raise ValueError("Parent group not found")
    node = tree.get(group_id)
    if node and target.path.startswith(node.path):
        raise ValueError("A group cannot be moved into its own subtree")
    assert_parent_allowed(target)
    if node and any(child.parent_id == group_id for child in tree.by_id.values()):
        raise ValueError("Move the rings out of this application before turning it into a ring")


async def next_sequence(db: AsyncSession, parent_id: str | None) -> int:
    statement = select(func.max(Group.sequence))
    statement = statement.where(Group.parent_id.is_(None)) if parent_id is None else statement.where(Group.parent_id == parent_id)
    return int(await db.scalar(statement) or 0) + 1


async def ensure_group_path(db: AsyncSession, segments: list[str], created_by: str | None = None, created: list[str] | None = None) -> Group:
    """Walk an application/ring name path, creating what is missing. Shared by CSV import and settings import."""
    if len(segments) > MAX_DEPTH + 1:
        raise ValueError("A ring cannot contain another ring — use application/ring only")
    if any(len(name.strip()) > MAX_GROUP_NAME for name in segments):
        raise ValueError(f"Group names cannot exceed {MAX_GROUP_NAME} characters")
    parent: Group | None = None
    for name in segments:
        statement = select(Group).where(func.lower(Group.name) == name.strip().lower())
        statement = statement.where(Group.parent_id.is_(None)) if parent is None else statement.where(Group.parent_id == parent.id)
        found = await db.scalar(statement.limit(1))
        if not found:
            found = Group(id=new_id(), parent_id=parent.id if parent else None, name=name.strip(), sequence=await next_sequence(db, parent.id if parent else None), created_by=created_by)
            found.path = child_path(parent, found.id)
            found.depth = (parent.depth + 1) if parent else 0
            db.add(found)
            await db.flush()
            if created is not None:
                created.append(found.id)
        parent = found
    if parent is None:
        raise ValueError("application is required")
    return parent


#: A machine may be owned by one schedule per action: one start, one stop.
ACTIONS: tuple[str, ...] = ("start", "stop")


@dataclass
class ScheduleIndex:
    """Nearest-schedule lookup, kept per action so a start never shadows a stop."""

    by_vm: dict[tuple[str, str], Schedule] = field(default_factory=dict)
    by_group: dict[tuple[str, str], Schedule] = field(default_factory=dict)


async def load_schedule_index(db: AsyncSession) -> ScheduleIndex:
    schedules = (await db.scalars(select(Schedule).where(Schedule.enabled.is_(True)).order_by(Schedule.created_at, Schedule.id))).all()
    index = ScheduleIndex()
    for schedule in schedules:
        bucket = index.by_vm if schedule.target_type == "vm" else index.by_group
        bucket.setdefault((schedule.action, schedule.target_id), schedule)
    return index


def effective_schedule(index: ScheduleIndex, tree: GroupTree, vm: VirtualMachine, action: str = "start") -> Schedule | None:
    """VM binding wins; otherwise the nearest ancestor group schedule shadows the ones above it.

    Resolution is per action, so the same machine can have both a start and a stop owner.
    """
    direct = index.by_vm.get((action, vm.id))
    if direct:
        return direct
    for node in tree.chain(vm.group_id):
        found = index.by_group.get((action, node.id))
        if found:
            return found
    return None


def is_stop_protected(tree: GroupTree, vm: VirtualMachine) -> bool:
    """A machine is protected when it, or any group above it, is marked never_stop."""
    if vm.never_stop:
        return True
    return any(node.never_stop for node in tree.chain(vm.group_id))


def effective_connection_id(tree: GroupTree, vm: VirtualMachine) -> str | None:
    if vm.azure_connection_id:
        return vm.azure_connection_id
    for node in tree.chain(vm.group_id):
        if node.azure_connection_id:
            return node.azure_connection_id
    return None


#: Above this many group ids an IN clause stops being worth building and the whole inventory is
#: cheaper to fetch in one go. Also keeps us clear of SQLite's bound-parameter ceiling.
_BULK_GROUP_LIMIT = 400


def _select_for_schedule(
    schedule: Schedule,
    tree: GroupTree,
    index: ScheduleIndex,
    by_group: dict[str, list[VirtualMachine]],
    by_id: dict[str, VirtualMachine],
) -> list[VirtualMachine]:
    """The selection rule for one wave, against already-loaded machines.

    Single source of truth for what a wave acts on; both the single and bulk resolvers use it so
    they can never drift apart.
    """
    action = schedule.action or "start"
    if schedule.target_type == "vm":
        found = by_id.get(schedule.target_id)
        candidates = [found] if found else []
    else:
        candidates = [vm for group_id in tree.subtree_ids(schedule.target_id) for vm in by_group.get(group_id, ())]
    selected = [
        vm
        for vm in candidates
        if vm
        and vm.enabled
        and tree.is_active(vm.group_id)
        and (effective_schedule(index, tree, vm, action) or schedule).id == schedule.id
        and not (action == "stop" and is_stop_protected(tree, vm))
    ]
    # Rings normally unwind in reverse for stops, so the canary ring is the last one down.
    reverse = action == "stop" and (schedule.ring_order or "sequence") == "reverse"
    return sorted(selected, key=lambda item: _order_key(tree, item), reverse=reverse)


async def _load_candidates(
    db: AsyncSession,
    schedules: list[Schedule],
    tree: GroupTree,
) -> tuple[dict[str, list[VirtualMachine]], dict[str, VirtualMachine]]:
    """Fetch every machine the given schedules could possibly touch, in one query."""
    group_ids: set[str] = set()
    vm_ids: set[str] = set()
    for schedule in schedules:
        if schedule.target_type == "vm":
            vm_ids.add(schedule.target_id)
        else:
            group_ids |= tree.subtree_ids(schedule.target_id)

    statement = select(VirtualMachine)
    if not group_ids and not vm_ids:
        return {}, {}
    if len(group_ids) <= _BULK_GROUP_LIMIT:
        clauses = []
        if group_ids:
            clauses.append(VirtualMachine.group_id.in_(list(group_ids)))
        if vm_ids:
            clauses.append(VirtualMachine.id.in_(list(vm_ids)))
        statement = statement.where(or_(*clauses)) if len(clauses) > 1 else statement.where(clauses[0])

    by_group: dict[str, list[VirtualMachine]] = {}
    by_id: dict[str, VirtualMachine] = {}
    for vm in (await db.scalars(statement)).all():
        by_group.setdefault(vm.group_id, []).append(vm)
        by_id[vm.id] = vm
    return by_group, by_id


async def resolve_schedule_vms_bulk(
    db: AsyncSession,
    schedules: list[Schedule],
    tree: GroupTree | None = None,
    index: ScheduleIndex | None = None,
) -> dict[str, list[VirtualMachine]]:
    """Resolve many waves at once, keyed by schedule id.

    One query for every schedule on the page rather than one query each: the listings page 200
    schedules at a time, so the per-schedule form turned a single request into 200 round trips.
    """
    if not schedules:
        return {}
    tree = tree or await load_tree(db)
    index = index or await load_schedule_index(db)
    by_group, by_id = await _load_candidates(db, schedules, tree)
    return {schedule.id: _select_for_schedule(schedule, tree, index, by_group, by_id) for schedule in schedules}


async def resolve_schedule_vms(
    db: AsyncSession,
    schedule: Schedule,
    tree: GroupTree | None = None,
    index: ScheduleIndex | None = None,
) -> list[VirtualMachine]:
    """VMs a wave should act on: enabled, inside the target subtree, and not shadowed by a nearer
    schedule for the same action. Stop waves additionally skip protected machines."""
    resolved = await resolve_schedule_vms_bulk(db, [schedule], tree, index)
    return resolved.get(schedule.id, [])


def _order_key(tree: GroupTree, vm: VirtualMachine) -> tuple[list[tuple[int, str]], str, str]:
    chain = list(reversed(tree.chain(vm.group_id)))
    return ([(node.sequence, node.name.lower()) for node in chain], vm.vm_name.lower(), vm.id)
