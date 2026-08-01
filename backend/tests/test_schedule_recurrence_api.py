"""Weekly and cron schedules through the API, plus the live recurrence preview."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Schedule

from test_listing_sort import api_client, seed_export_estate


async def _target_group(session) -> str:
    from app.models import Group

    return (await session.scalars(select(Group.id).where(Group.name == "Payments"))).first()


async def test_a_weekly_schedule_is_created_and_lands_on_its_weekday(session) -> None:
    await seed_export_estate(session)
    group_id = await _target_group(session)
    async with api_client(session) as client:
        response = await client.post("/api/schedules", json={
            "name": "Monday warm-up", "schedule_type": "weekly", "start_time": "08:00", "weekday": 0,
            "timezone": "UTC", "target_type": "group", "target_id": group_id,
        })

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["schedule_type"], body["weekday"]) == ("weekly", 0)
    schedule = await session.scalar(select(Schedule).where(Schedule.name == "Monday warm-up"))
    assert schedule.next_run_at.weekday() == 0


async def test_a_cron_schedule_is_created_and_normalised(session) -> None:
    await seed_export_estate(session)
    group_id = await _target_group(session)
    async with api_client(session) as client:
        response = await client.post("/api/schedules", json={
            "name": "Weekday mornings", "schedule_type": "cron", "cron_expression": "  0 9 * *  1,2,3,4,5 ",
            "timezone": "UTC", "target_type": "group", "target_id": group_id,
        })

    assert response.status_code == 201, response.text
    assert response.json()["cron_expression"] == "0 9 * * 1,2,3,4,5"


async def test_an_invalid_cron_is_refused_with_a_readable_reason(session) -> None:
    await seed_export_estate(session)
    group_id = await _target_group(session)
    async with api_client(session) as client:
        response = await client.post("/api/schedules", json={
            "name": "Nonsense", "schedule_type": "cron", "cron_expression": "0 99 * * *",
            "timezone": "UTC", "target_type": "group", "target_id": group_id,
        })

    assert response.status_code == 422
    assert "between 0 and 23" in response.text


async def test_a_start_and_a_stop_may_share_a_recurrence(session) -> None:
    await seed_export_estate(session)
    group_id = await _target_group(session)
    body = {"schedule_type": "daily", "start_time": "08:00", "timezone": "UTC", "target_type": "group", "target_id": group_id}
    async with api_client(session) as client:
        first = await client.post("/api/schedules", json={**body, "name": "Up", "action": "start"})
        second = await client.post("/api/schedules", json={**body, "name": "Down", "action": "stop"})
        third = await client.post("/api/schedules", json={**body, "name": "Up again", "action": "start"})

    assert (first.status_code, second.status_code) == (201, 201)
    # Same action, same recurrence, same target: that one really is a duplicate.
    assert third.status_code == 409


async def test_two_cron_schedules_differing_only_by_expression_are_not_duplicates(session) -> None:
    await seed_export_estate(session)
    group_id = await _target_group(session)
    body = {"schedule_type": "cron", "timezone": "UTC", "target_type": "group", "target_id": group_id}
    async with api_client(session) as client:
        first = await client.post("/api/schedules", json={**body, "name": "Nine", "cron_expression": "0 9 * * *"})
        second = await client.post("/api/schedules", json={**body, "name": "Ten", "cron_expression": "0 10 * * *"})

    assert (first.status_code, second.status_code) == (201, 201)


async def test_a_schedule_created_without_an_explicit_enabled_flag_still_runs(session) -> None:
    """`enabled` defaults to true, so omitting it must not leave a schedule with no next run."""
    await seed_export_estate(session)
    group_id = await _target_group(session)
    async with api_client(session) as client:
        response = await client.post("/api/schedules", json={
            "name": "Implicitly enabled", "schedule_type": "daily", "start_time": "08:00",
            "timezone": "UTC", "target_type": "group", "target_id": group_id,
        })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["status"] == "scheduled"
    assert body["next_run_at"] is not None


# -- the preview the editor calls on every keystroke ---------------------


async def test_preview_describes_a_weekly_recurrence_and_lists_occurrences(session) -> None:
    async with api_client(session) as client:
        response = await client.post("/api/schedules/preview", json={
            "schedule_type": "weekly", "start_time": "08:00", "weekday": 0, "timezone": "UTC",
        })

    body = response.json()
    assert body["valid"] is True
    assert body["description"] == "Weekly on Mon at 08:00 (UTC)"
    assert body["cron"] == "0 8 * * 1"
    assert len(body["upcoming"]) == 5


async def test_preview_reports_a_bad_cron_instead_of_failing(session) -> None:
    async with api_client(session) as client:
        response = await client.post("/api/schedules/preview", json={
            "schedule_type": "cron", "cron_expression": "not a cron", "timezone": "UTC",
        })

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["upcoming"] == []
    assert "not valid" in body["error"]


async def test_preview_honours_the_run_limit_already_spent(session) -> None:
    async with api_client(session) as client:
        response = await client.post("/api/schedules/preview", json={
            "schedule_type": "daily", "start_time": "08:00", "timezone": "UTC", "run_limit": 3, "run_count": 1,
        })

    assert len(response.json()["upcoming"]) == 2


async def test_preview_honours_calendar_bounds(session) -> None:
    async with api_client(session) as client:
        response = await client.post("/api/schedules/preview", json={
            "schedule_type": "daily", "start_time": "08:00", "timezone": "UTC", "end_date": "2000-01-01",
        })

    body = response.json()
    assert body["valid"] is False
    assert "not valid" in body["error"]
