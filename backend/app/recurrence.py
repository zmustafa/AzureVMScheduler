"""One place that answers "when does this schedule run next".

Every recurring type is evaluated through the same cron matcher, so there is exactly one occurrence
engine to reason about and test. `daily` and `weekly` are stored as friendly fields and translated
to cron on the way in; only `one_time` is a genuine special case.

Times are wall-clock text plus an IANA zone, evaluated with `zoneinfo`, so recurrences are DST-safe:
an occurrence that lands in a spring-forward gap is skipped rather than silently shifted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEDULE_TYPES: tuple[str, ...] = ("one_time", "daily", "weekly", "cron")
RECURRING_TYPES: frozenset[str] = frozenset({"daily", "weekly", "cron"})

# Monday-first, matching datetime.weekday() and how operators read a week.
WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_CRON_DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_CRON_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# A yearly cron (say 29 February) must still find its next occurrence, and a leap day is at most
# four years out.
_MAX_DAYS_SCANNED = 366 * 4 + 1


class RecurrenceError(ValueError):
    """A recurrence that cannot be evaluated, phrased for the person editing the schedule."""


@dataclass(frozen=True)
class Recurrence:
    """Everything needed to place a schedule on a calendar, independent of the ORM."""

    schedule_type: str
    timezone: str
    start_time: str = ""
    cron_expression: str = ""
    # 0 = Monday .. 6 = Sunday, weekly only.
    weekday: int | None = None
    # Local calendar bounds in the schedule's own timezone; "" means unbounded.
    start_date: str = ""
    end_date: str = ""
    run_limit: int | None = None
    run_count: int = 0


@dataclass(frozen=True)
class _CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool = field(default=False)
    dow_restricted: bool = field(default=False)

    def matches_date(self, value: date) -> bool:
        if value.month not in self.months:
            return False
        dow = (value.weekday() + 1) % 7  # cron counts Sunday as 0
        # Standard cron: when both day fields are restricted the day matches if *either* does.
        if self.dom_restricted and self.dow_restricted:
            return value.day in self.days_of_month or dow in self.days_of_week
        return value.day in self.days_of_month and dow in self.days_of_week


# -- parsing -------------------------------------------------------------


def resolve_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise RecurrenceError(f"Unknown timezone: {name}") from exc


def localize(value: datetime, zone: ZoneInfo) -> datetime | None:
    """Attach a zone to a wall-clock time, or return None if that time does not exist there.

    A spring-forward gap has no such wall-clock instant, so the caller skips the occurrence rather
    than firing an hour early or late.
    """
    localized = value.replace(tzinfo=zone)
    if localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != value:
        return None
    return localized


def _parse_field(raw: str, low: int, high: int, names: dict[str, int], label: str) -> tuple[frozenset[int], bool]:
    """Expand one cron field. Returns the matching values and whether it was restricted."""
    text = raw.strip().lower()
    if not text:
        raise RecurrenceError(f"The {label} field of the cron expression is empty")
    restricted = text != "*"
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise RecurrenceError(f"The {label} field has an empty item")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise RecurrenceError(f"The {label} field has an invalid step: /{step_text}")
            step = int(step_text)
            part = part.strip() or "*"
        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_text, _, end_text = part.partition("-")
            start, end = _single(start_text, low, high, names, label), _single(end_text, low, high, names, label)
            if start > end:
                raise RecurrenceError(f"The {label} field has a reversed range: {part}")
        else:
            start = end = _single(part, low, high, names, label)
        values.update(range(start, end + 1, step))
    if not values:
        raise RecurrenceError(f"The {label} field matches nothing")
    return frozenset(values), restricted


def _single(text: str, low: int, high: int, names: dict[str, int], label: str) -> int:
    token = text.strip().lower()
    if token in names:
        return names[token]
    try:
        value = int(token)
    except ValueError as exc:
        raise RecurrenceError(f"The {label} field has an unrecognised value: {text.strip()}") from exc
    # Cron accepts 7 for Sunday alongside 0.
    if label == "day-of-week" and value == 7:
        return 0
    if not low <= value <= high:
        raise RecurrenceError(f"The {label} field must be between {low} and {high}, not {value}")
    return value


def parse_cron(expression: str) -> _CronSpec:
    """Parse a standard five-field cron expression: minute hour day-of-month month day-of-week."""
    fields = expression.split()
    if len(fields) != 5:
        raise RecurrenceError(f"A cron expression needs exactly 5 fields (minute hour day month weekday), got {len(fields)}")
    minutes, _ = _parse_field(fields[0], 0, 59, {}, "minute")
    hours, _ = _parse_field(fields[1], 0, 23, {}, "hour")
    days_of_month, dom_restricted = _parse_field(fields[2], 1, 31, {}, "day-of-month")
    months, _ = _parse_field(fields[3], 1, 12, _CRON_MONTH_NAMES, "month")
    days_of_week, dow_restricted = _parse_field(fields[4], 0, 6, _CRON_DAY_NAMES, "day-of-week")
    return _CronSpec(minutes, hours, days_of_month, months, days_of_week, dom_restricted, dow_restricted)


def validate_cron(expression: str) -> str:
    parse_cron(expression)
    return " ".join(expression.split())


def _parse_wall_time(value: str, label: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise RecurrenceError(f"{label} must be HH:MM or HH:MM:SS") from exc
    if parsed.tzinfo is not None:
        raise RecurrenceError(f"{label} must be a local wall-clock time without an offset")
    return parsed


def _parse_date(value: str, label: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise RecurrenceError(f"{label} must be a YYYY-MM-DD date") from exc


def to_cron(recurrence: Recurrence) -> str:
    """Translate the friendly types into the cron the matcher actually runs."""
    if recurrence.schedule_type == "cron":
        return validate_cron(recurrence.cron_expression)
    if recurrence.schedule_type == "daily":
        wall = _parse_wall_time(recurrence.start_time, "Time of day")
        return f"{wall.minute} {wall.hour} * * *"
    if recurrence.schedule_type == "weekly":
        wall = _parse_wall_time(recurrence.start_time, "Time of day")
        if recurrence.weekday is None or not 0 <= recurrence.weekday <= 6:
            raise RecurrenceError("A weekly schedule needs a weekday between Monday and Sunday")
        return f"{wall.minute} {wall.hour} * * {(recurrence.weekday + 1) % 7}"
    raise RecurrenceError(f"{recurrence.schedule_type} has no cron form")


# -- occurrences ---------------------------------------------------------


def _budget_exhausted(recurrence: Recurrence) -> bool:
    return recurrence.run_limit is not None and recurrence.run_count >= recurrence.run_limit


def _one_time_at(recurrence: Recurrence, zone: ZoneInfo) -> datetime:
    text = recurrence.start_time.strip()
    if "T" not in text and " " not in text:
        raise RecurrenceError("A one-time schedule needs a date and a time")
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecurrenceError("A one-time start must be an ISO-8601 date and time") from exc
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    localized = localize(value.replace(second=0, microsecond=0), zone)
    if localized is None:
        raise RecurrenceError("That start time does not exist in this timezone — it falls in a daylight-saving time gap")
    return localized.astimezone(timezone.utc)


def one_time_at(recurrence: Recurrence) -> datetime:
    """The absolute moment a one-time schedule names, past or future.

    Distinct from next_occurrence, which reports None once that moment has gone: creating a
    one-time schedule a minute late is allowed, and the missed-run grace decides how late is late.
    """
    return _one_time_at(recurrence, resolve_zone(recurrence.timezone))


def next_occurrence(recurrence: Recurrence, after: datetime | None = None) -> datetime | None:
    """The first occurrence strictly after `after`, or None if the schedule has no future left."""
    now = (after or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = resolve_zone(recurrence.timezone)

    if recurrence.schedule_type == "one_time":
        moment = _one_time_at(recurrence, zone)
        return moment if moment > now and not _budget_exhausted(recurrence) else None

    if _budget_exhausted(recurrence):
        return None

    spec = parse_cron(to_cron(recurrence))
    window_start = _parse_date(recurrence.start_date, "Start date")
    window_end = _parse_date(recurrence.end_date, "End date")
    if window_start and window_end and window_start > window_end:
        raise RecurrenceError("The end date is before the start date")

    cursor = now.astimezone(zone).date()
    if window_start and window_start > cursor:
        cursor = window_start
    hours, minutes = sorted(spec.hours), sorted(spec.minutes)

    for _ in range(_MAX_DAYS_SCANNED):
        if window_end and cursor > window_end:
            return None
        if spec.matches_date(cursor):
            for hour in hours:
                for minute in minutes:
                    moment = localize(datetime.combine(cursor, time(hour, minute)), zone)
                    if moment is None:
                        continue  # daylight-saving gap: this wall-clock time does not exist today
                    moment = moment.astimezone(timezone.utc)
                    if moment > now:
                        return moment
        cursor += timedelta(days=1)
    return None


def upcoming(recurrence: Recurrence, after: datetime | None = None, count: int = 5) -> list[datetime]:
    """The next few occurrences, for the preview shown while editing."""
    moments: list[datetime] = []
    cursor = after or datetime.now(timezone.utc)
    remaining = recurrence.run_limit - recurrence.run_count if recurrence.run_limit is not None else None
    for _ in range(count):
        if remaining is not None and len(moments) >= remaining:
            break
        moment = next_occurrence(recurrence, cursor)
        if moment is None:
            break
        moments.append(moment)
        cursor = moment
    return moments


def describe(recurrence: Recurrence) -> str:
    """A one-line summary, e.g. "Weekly on Mon at 08:00 (UTC)"."""
    zone = recurrence.timezone
    if recurrence.schedule_type == "one_time":
        return f"Once at {recurrence.start_time} ({zone})"
    if recurrence.schedule_type == "daily":
        return f"Daily at {_hhmm(recurrence.start_time)} ({zone})"
    if recurrence.schedule_type == "weekly":
        day = WEEKDAY_LABELS[recurrence.weekday] if recurrence.weekday is not None and 0 <= recurrence.weekday <= 6 else "?"
        return f"Weekly on {day} at {_hhmm(recurrence.start_time)} ({zone})"
    return f"Cron: {' '.join(recurrence.cron_expression.split())} ({zone})"


def _hhmm(value: str) -> str:
    try:
        return _parse_wall_time(value, "Time of day").strftime("%H:%M")
    except RecurrenceError:
        return value


def validate(recurrence: Recurrence) -> None:
    """Raise RecurrenceError if the recurrence could never produce an occurrence."""
    if recurrence.schedule_type not in SCHEDULE_TYPES:
        raise RecurrenceError(f"Unknown frequency: {recurrence.schedule_type}")
    resolve_zone(recurrence.timezone)
    if recurrence.run_limit is not None and recurrence.run_limit < 1:
        raise RecurrenceError("The run limit must be at least 1")
    start = _parse_date(recurrence.start_date, "Start date")
    end = _parse_date(recurrence.end_date, "End date")
    if start and end and start > end:
        raise RecurrenceError("The end date is before the start date")
    if recurrence.schedule_type == "one_time":
        _one_time_at(recurrence, resolve_zone(recurrence.timezone))
        return
    to_cron(recurrence)
    # A bounded recurrence can be valid in shape yet describe an empty calendar; say so now rather
    # than silently creating a schedule that never fires.
    if next_occurrence(recurrence) is None and not _budget_exhausted(recurrence):
        raise RecurrenceError("This recurrence has no future occurrences — check the start and end dates")
