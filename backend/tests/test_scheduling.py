from datetime import datetime, timezone

import pytest

from app.models import Schedule
from app.scheduling import next_occurrence, parse_schedule_time


def test_one_time_with_timezone_becomes_utc() -> None:
    result = parse_schedule_time("one_time", "2027-01-15T09:30:00", "America/New_York")
    assert result == datetime(2027, 1, 15, 14, 30, tzinfo=timezone.utc)


def test_daily_uses_next_local_occurrence() -> None:
    now = datetime(2027, 1, 15, 16, 0, tzinfo=timezone.utc)
    result = parse_schedule_time("daily", "09:30", "America/New_York", now)
    assert result == datetime(2027, 1, 16, 14, 30, tzinfo=timezone.utc)


def test_one_time_has_no_followup() -> None:
    schedule = Schedule(name="once", schedule_type="one_time", start_time="2027-01-15T09:30:00Z", timezone="UTC", target_type="vm", target_id="vm-1")
    # Before its moment it reports that moment; once the moment has gone it never comes round again.
    assert next_occurrence(schedule, datetime(2027, 1, 15, 9, 0, tzinfo=timezone.utc)) == datetime(2027, 1, 15, 9, 30, tzinfo=timezone.utc)
    assert next_occurrence(schedule, datetime(2027, 1, 15, 10, 0, tzinfo=timezone.utc)) is None


def test_rejects_nonexistent_daylight_saving_time() -> None:
    with pytest.raises(ValueError, match="daylight-saving time gap"):
        parse_schedule_time("one_time", "2027-03-14T02:30:00", "America/New_York")
