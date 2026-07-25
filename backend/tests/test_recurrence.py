"""The recurrence engine: cron parsing, DST behaviour, and bounded schedules."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.recurrence import (
    Recurrence,
    RecurrenceError,
    describe,
    next_occurrence,
    parse_cron,
    to_cron,
    upcoming,
    validate,
    validate_cron,
)


UTC = timezone.utc


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# -- cron parsing --------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "minutes", "hours"),
    [
        ("0 9 * * *", {0}, {9}),
        ("*/15 * * * *", {0, 15, 30, 45}, set(range(24))),
        ("30 9,17 * * *", {30}, {9, 17}),
        ("0 9-11 * * *", {0}, {9, 10, 11}),
        ("0 0-23/6 * * *", {0}, {0, 6, 12, 18}),
    ],
)
def test_cron_fields_expand(expression: str, minutes: set[int], hours: set[int]) -> None:
    spec = parse_cron(expression)
    assert set(spec.minutes) == minutes
    assert set(spec.hours) == hours


def test_cron_accepts_day_and_month_names() -> None:
    spec = parse_cron("0 9 * JAN-MAR MON,FRI")
    assert set(spec.months) == {1, 2, 3}
    assert set(spec.days_of_week) == {1, 5}


def test_cron_treats_seven_as_sunday() -> None:
    assert set(parse_cron("0 9 * * 7").days_of_week) == {0}


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("0 9 * *", "exactly 5 fields"),
        ("60 9 * * *", "between 0 and 59"),
        ("0 24 * * *", "between 0 and 23"),
        ("0 9 * * 9", "between 0 and 6"),
        ("0 9 * * MONDAY", "unrecognised value"),
        ("11-9 9 * * *", "reversed range"),
        ("*/0 9 * * *", "invalid step"),
    ],
)
def test_cron_rejects_nonsense(expression: str, message: str) -> None:
    with pytest.raises(RecurrenceError, match=message):
        parse_cron(expression)


def test_validate_cron_normalises_whitespace() -> None:
    assert validate_cron("  0   9 * * 1,2  ") == "0 9 * * 1,2"


# -- translation of the friendly types -----------------------------------


def test_daily_and_weekly_become_cron() -> None:
    assert to_cron(Recurrence("daily", "UTC", start_time="08:30")) == "30 8 * * *"
    # weekday 0 is Monday for us, which is 1 in cron.
    assert to_cron(Recurrence("weekly", "UTC", start_time="08:00", weekday=0)) == "0 8 * * 1"
    # Sunday is 6 for us and 0 in cron.
    assert to_cron(Recurrence("weekly", "UTC", start_time="08:00", weekday=6)) == "0 8 * * 0"


def test_weekly_without_a_weekday_is_rejected() -> None:
    with pytest.raises(RecurrenceError, match="needs a weekday"):
        to_cron(Recurrence("weekly", "UTC", start_time="08:00"))


# -- occurrences ---------------------------------------------------------


def test_daily_finds_tomorrow_when_today_has_passed() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00")
    assert next_occurrence(rec, at("2026-07-24 09:00")) == at("2026-07-25 08:00")


def test_daily_finds_today_when_the_time_is_still_ahead() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00")
    assert next_occurrence(rec, at("2026-07-24 07:00")) == at("2026-07-24 08:00")


def test_weekly_lands_on_the_requested_weekday() -> None:
    # 2026-07-24 is a Friday; the next Monday is the 27th.
    rec = Recurrence("weekly", "UTC", start_time="08:00", weekday=0)
    assert next_occurrence(rec, at("2026-07-24 09:00")) == at("2026-07-27 08:00")


def test_cron_with_several_hours_fires_at_each_of_them() -> None:
    rec = Recurrence("cron", "UTC", cron_expression="0 9,17 * * *")
    assert upcoming(rec, at("2026-07-24 00:00"), 3) == [
        at("2026-07-24 09:00"),
        at("2026-07-24 17:00"),
        at("2026-07-25 09:00"),
    ]


def test_weekday_cron_skips_the_weekend() -> None:
    rec = Recurrence("cron", "UTC", cron_expression="0 9 * * 1,2,3,4,5")
    # Friday 24th, then straight to Monday 27th.
    assert upcoming(rec, at("2026-07-24 10:00"), 2) == [at("2026-07-27 09:00"), at("2026-07-28 09:00")]


def test_a_yearly_cron_still_resolves() -> None:
    rec = Recurrence("cron", "UTC", cron_expression="0 0 29 2 *")
    assert next_occurrence(rec, at("2026-07-24 00:00")) == at("2028-02-29 00:00")


def test_day_of_month_and_weekday_are_an_or_the_way_cron_defines_it() -> None:
    """With both day fields restricted, cron fires when either matches."""
    rec = Recurrence("cron", "UTC", cron_expression="0 0 1 * 1")
    moments = upcoming(rec, at("2026-07-24 00:00"), 4)
    days = [moment.strftime("%Y-%m-%d") for moment in moments]
    # Mondays in late July plus the 1st of August.
    assert days == ["2026-07-27", "2026-08-01", "2026-08-03", "2026-08-10"]


# -- timezones and daylight saving ---------------------------------------


def test_occurrences_track_the_schedule_timezone_not_utc() -> None:
    rec = Recurrence("daily", "America/New_York", start_time="08:00")
    # 08:00 EDT is 12:00 UTC.
    assert next_occurrence(rec, at("2026-07-24 00:00")) == at("2026-07-24 12:00")


def test_a_daily_time_holds_its_wall_clock_across_a_dst_change() -> None:
    """The whole point of storing wall-clock text: 08:00 stays 08:00 for the operator."""
    rec = Recurrence("daily", "America/New_York", start_time="08:00")
    before = next_occurrence(rec, at("2026-10-31 00:00"))
    after = next_occurrence(rec, at("2026-11-05 00:00"))
    assert before == at("2026-10-31 12:00")  # EDT, UTC-4
    assert after == at("2026-11-05 13:00")  # EST, UTC-5


def test_a_time_inside_a_spring_forward_gap_is_skipped_not_shifted() -> None:
    """02:30 does not exist on 2026-03-08 in New York, so that day produces no run."""
    zone = ZoneInfo("America/New_York")
    rec = Recurrence("daily", "America/New_York", start_time="02:30")
    days = [moment.astimezone(zone).strftime("%Y-%m-%d") for moment in upcoming(rec, at("2026-03-07 00:00"), 2)]
    assert days == ["2026-03-07", "2026-03-09"]


def test_an_unknown_timezone_is_reported_clearly() -> None:
    with pytest.raises(RecurrenceError, match="Unknown timezone"):
        next_occurrence(Recurrence("daily", "Mars/Olympus", start_time="08:00"))


# -- bounds --------------------------------------------------------------


def test_a_start_date_holds_the_schedule_back() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", start_date="2026-08-01")
    assert next_occurrence(rec, at("2026-07-24 00:00")) == at("2026-08-01 08:00")


def test_an_end_date_finishes_the_schedule() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", end_date="2026-07-25")
    assert next_occurrence(rec, at("2026-07-25 09:00")) is None


def test_the_end_date_includes_its_own_day() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", end_date="2026-07-25")
    assert next_occurrence(rec, at("2026-07-24 09:00")) == at("2026-07-25 08:00")


def test_a_run_limit_stops_the_schedule_once_it_is_spent() -> None:
    spent = Recurrence("daily", "UTC", start_time="08:00", run_limit=3, run_count=3)
    assert next_occurrence(spent, at("2026-07-24 00:00")) is None

    remaining = Recurrence("daily", "UTC", start_time="08:00", run_limit=3, run_count=1)
    assert next_occurrence(remaining, at("2026-07-24 00:00")) == at("2026-07-24 08:00")


def test_the_preview_never_shows_more_runs_than_the_limit_allows() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", run_limit=3, run_count=1)
    assert len(upcoming(rec, at("2026-07-24 00:00"), 5)) == 2


def test_reversed_dates_are_rejected() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", start_date="2026-08-01", end_date="2026-07-01")
    with pytest.raises(RecurrenceError, match="end date is before the start date"):
        validate(rec)


def test_a_recurrence_with_no_future_is_rejected_up_front() -> None:
    rec = Recurrence("daily", "UTC", start_time="08:00", end_date="2000-01-01")
    with pytest.raises(RecurrenceError, match="no future occurrences"):
        validate(rec)


# -- one time ------------------------------------------------------------


def test_a_one_time_schedule_fires_once_and_then_never() -> None:
    rec = Recurrence("one_time", "UTC", start_time="2026-07-25T08:00:00")
    assert next_occurrence(rec, at("2026-07-24 00:00")) == at("2026-07-25 08:00")
    assert next_occurrence(rec, at("2026-07-26 00:00")) is None


def test_a_one_time_start_is_read_in_the_schedule_timezone() -> None:
    rec = Recurrence("one_time", "America/New_York", start_time="2026-07-25T08:00:00")
    assert next_occurrence(rec, at("2026-07-24 00:00")) == at("2026-07-25 12:00")


# -- descriptions --------------------------------------------------------


@pytest.mark.parametrize(
    ("recurrence", "expected"),
    [
        (Recurrence("daily", "UTC", start_time="08:00"), "Daily at 08:00 (UTC)"),
        (Recurrence("weekly", "UTC", start_time="08:00", weekday=0), "Weekly on Mon at 08:00 (UTC)"),
        (Recurrence("cron", "UTC", cron_expression="0 9 * * 1,2,3,4,5"), "Cron: 0 9 * * 1,2,3,4,5 (UTC)"),
    ],
)
def test_descriptions_read_the_way_the_preview_shows_them(recurrence: Recurrence, expected: str) -> None:
    assert describe(recurrence) == expected


# -- the run budget, as the scheduler spends it --------------------------


async def test_a_scheduled_run_spends_the_budget_and_completes_the_schedule(session) -> None:
    """A run limit is only meaningful if finishing a wave actually decrements it."""
    from app.models import Group, Schedule, VirtualMachine, VmAttempt, new_id
    from app.hierarchy import next_sequence, recompute_subtree
    from app.scheduling import create_run, finalize_run_if_complete

    group = Group(id=new_id(), name="Payments", parent_id=None, sequence=await next_sequence(session, None))
    session.add(group)
    await session.flush()
    await recompute_subtree(session, group)
    resource = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-a"
    session.add(VirtualMachine(id=new_id(), group_id=group.id, vm_resource_id=resource, normalized_resource_id=resource.lower(), vm_name="vm-a", display_name="vm-a"))
    schedule = Schedule(
        id=new_id(), name="Twice only", schedule_type="daily", start_time="08:00", timezone="UTC",
        target_type="group", target_id=group.id, run_limit=2, run_count=0, enabled=True,
    )
    session.add(schedule)
    await session.commit()

    for expected_count in (1, 2):
        run = await create_run(session, schedule, trigger="scheduler")
        for attempt in (await session.scalars(select(VmAttempt).where(VmAttempt.run_id == run.id))).all():
            attempt.status = "succeeded"
        await session.commit()
        await finalize_run_if_complete(session, run.id)
        assert schedule.run_count == expected_count

    # The budget is spent, so the schedule is finished rather than waiting for a run that cannot come.
    assert schedule.next_run_at is None
    assert schedule.status == "completed"


async def test_a_manual_run_does_not_spend_the_budget(session) -> None:
    from app.models import Group, Schedule, new_id
    from app.hierarchy import next_sequence, recompute_subtree
    from app.scheduling import create_run, finalize_run_if_complete

    group = Group(id=new_id(), name="Payments", parent_id=None, sequence=await next_sequence(session, None))
    session.add(group)
    await session.flush()
    await recompute_subtree(session, group)
    schedule = Schedule(
        id=new_id(), name="Budgeted", schedule_type="daily", start_time="08:00", timezone="UTC",
        target_type="group", target_id=group.id, run_limit=2, run_count=0, enabled=True,
    )
    session.add(schedule)
    await session.commit()

    run = await create_run(session, schedule, trigger="manual")
    await finalize_run_if_complete(session, run.id)

    assert schedule.run_count == 0
