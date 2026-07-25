"""ServiceNow incidents keyed by correlation_id so repeat failures update instead of duplicating."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .base import (
    ConnectorError,
    ConnectorType,
    FieldSpec,
    Message,
    as_bool,
    http_client,
    raise_for_connector,
    require,
    require_https,
    transport_error,
)


INCIDENT_NUMBER = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
CORRELATION_PREFIX = "azureops"
CLOSED_STATES = {"6", "7", "8"}

FIELDS = (
    FieldSpec("instance_url", "Instance URL", type="url", placeholder="https://zava.service-now.com"),
    FieldSpec("username", "Integration user"),
    FieldSpec("password", "Password", type="password", secret=True),
    FieldSpec("default_urgency", "Default urgency", type="select", options=("1", "2", "3"), optional=True),
    FieldSpec("default_impact", "Default impact", type="select", options=("1", "2", "3"), optional=True),
    FieldSpec("default_assignment_group", "Default assignment group", optional=True),
    FieldSpec("default_caller_id", "Default caller", optional=True),
    FieldSpec("create_on", "Open incidents for", placeholder="run.failed, run.partially_failed, schedule.missed", optional=True, help="Comma separated event types; blank means every routed event"),
    FieldSpec("auto_resolve", "Close the incident on the next success", type="checkbox", optional=True),
)


def correlation_id(schedule_id: str | None) -> str:
    return f"{CORRELATION_PREFIX}:{schedule_id or 'unknown'}"


def validate_number(value: str) -> str:
    candidate = (value or "").strip()
    if not INCIDENT_NUMBER.fullmatch(candidate):
        raise ConnectorError("ServiceNow incident number is not in the expected format")
    return candidate


def _create_on(config: dict[str, Any]) -> set[str]:
    return {item.strip() for item in str(config.get("create_on") or "").replace(";", ",").split(",") if item.strip()}


def incident_payload(config: dict[str, Any], message: Message, correlation: str) -> dict[str, Any]:
    payload = {
        "short_description": " ".join((message.title or "AzureOps notification").split())[:160],
        "description": message.body or message.title,
        "correlation_id": correlation,
        "correlation_display": "AzureOps",
    }
    for key, target in (("default_urgency", "urgency"), ("default_impact", "impact"), ("default_assignment_group", "assignment_group"), ("default_caller_id", "caller_id")):
        value = str(config.get(key) or "").strip()
        if value:
            payload[target] = value
    return payload


def _auth(config: dict[str, Any]) -> tuple[str, str, str]:
    instance, username, password = require(config, "instance_url", "username", "password")
    return require_https(instance, "instance_url").rstrip("/"), username, password


async def _request(config: dict[str, Any], method: str, path: str, **kwargs: Any) -> httpx.Response:
    instance, username, password = _auth(config)
    try:
        async with http_client(auth=(username, password), headers={"Accept": "application/json"}) as client:
            return await client.request(method, f"{instance}{path}", **kwargs)
    except httpx.HTTPError as exc:
        raise transport_error(exc, "ServiceNow request") from exc


async def _find_correlated(config: dict[str, Any], correlation: str) -> dict[str, Any] | None:
    response = await _request(config, "GET", "/api/now/table/incident", params={"sysparm_query": f"correlation_id={correlation}^stateNOT IN{','.join(sorted(CLOSED_STATES))}", "sysparm_limit": "1", "sysparm_fields": "number,sys_id,state"})
    raise_for_connector(response, "ServiceNow incident lookup")
    records = response.json().get("result") or []
    return records[0] if records else None


async def send(config: dict[str, Any], message: Message) -> dict[str, Any]:
    correlation = message.correlation_key or correlation_id(None)
    existing = await _find_correlated(config, correlation)

    if message.resolve:
        if not existing:
            return {"detail": "No open correlated incident to resolve", "external_ref": "", "skipped": True}
        number, sys_id = validate_number(str(existing.get("number") or "")), str(existing.get("sys_id") or "")
        response = await _request(config, "PATCH", f"/api/now/table/incident/{sys_id}", json={
            "work_notes": message.body or "AzureOps recorded a successful run for this schedule.",
            "state": "6",
            "close_code": "Solved (Permanently)",
            "close_notes": message.body or "Resolved automatically by AzureOps after a successful run.",
        })
        raise_for_connector(response, "ServiceNow incident resolution")
        return {"detail": f"Resolved incident {number}", "external_ref": number}

    allowed = _create_on(config)
    if allowed and message.event_type and message.event_type not in allowed:
        return {"detail": f"{message.event_type} is not configured to open incidents", "external_ref": "", "skipped": True}

    if existing:
        number, sys_id = validate_number(str(existing.get("number") or "")), str(existing.get("sys_id") or "")
        response = await _request(config, "PATCH", f"/api/now/table/incident/{sys_id}", json={"work_notes": f"{message.title}\n\n{message.body}"})
        raise_for_connector(response, "ServiceNow work note")
        return {"detail": f"Updated incident {number}", "external_ref": number}

    response = await _request(config, "POST", "/api/now/table/incident", json=incident_payload(config, message, correlation))
    raise_for_connector(response, "ServiceNow incident creation")
    record = response.json().get("result") or {}
    number = validate_number(str(record.get("number") or ""))
    return {"detail": f"Created incident {number}", "external_ref": number, "sys_id": str(record.get("sys_id") or "")}


async def test(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only probe: never creates an incident."""
    response = await _request(config, "GET", "/api/now/table/incident", params={"sysparm_limit": "1", "sysparm_fields": "number"})
    raise_for_connector(response, "ServiceNow authentication")
    return {"detail": "Authenticated against the incident table"}


def resolves_automatically(config: dict[str, Any]) -> bool:
    return as_bool(config.get("auto_resolve"))


CONNECTOR = ConnectorType(
    id="servicenow",
    label="ServiceNow",
    description="Open and update incidents, correlated per schedule so repeat failures never duplicate.",
    modes={"basic": FIELDS},
    send=send,
    test=test,
    allow_send_test=False,
)
