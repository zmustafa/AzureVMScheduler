"""Encrypted connector registry backed by an atomically written JSON file."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import get_settings
from ..connections import decrypt_value, encrypt_value
from . import email, servicenow, slack, teams, webhook
from .base import ConnectorError, ConnectorType, Message


CONNECTOR_TYPES: dict[str, ConnectorType] = {
    item.id: item
    for item in (email.CONNECTOR, teams.CONNECTOR, slack.CONNECTOR, servicenow.CONNECTOR, webhook.CONNECTOR)
}

_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return get_settings().data_dir / "connectors.json"


def _load_raw() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Connector store must contain a JSON array")
    return data


def _write_atomic(items: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="connectors-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def connector_type(type_id: str) -> ConnectorType:
    found = CONNECTOR_TYPES.get(type_id)
    if not found:
        raise ValueError(f"Unsupported connector type: {type_id}")
    return found


def type_metadata() -> list[dict[str, Any]]:
    return [item.as_dict() for item in CONNECTOR_TYPES.values()]


def _secret_fields(type_id: str, mode: str) -> set[str]:
    specs = connector_type(type_id).modes.get(mode, ())
    return {spec.key for spec in specs if spec.secret}


def _allowed_fields(type_id: str, mode: str) -> set[str]:
    return {spec.key for spec in connector_type(type_id).modes.get(mode, ())}


def public_connector(item: dict[str, Any]) -> dict[str, Any]:
    """Secrets never leave the process; callers only learn whether each one is populated."""
    secrets = _secret_fields(item["type"], item["mode"])
    stored = item.get("config") or {}
    config = {key: value for key, value in stored.items() if key not in secrets}
    config.update({f"{key}_set": bool(stored.get(key)) for key in secrets})
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "type": item["type"],
        "mode": item["mode"],
        "disabled": bool(item.get("disabled", False)),
        "status": item.get("status", "unknown"),
        "status_detail": item.get("status_detail", ""),
        "last_tested": item.get("last_tested"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "config": config,
    }


def _decrypt(item: dict[str, Any]) -> dict[str, Any]:
    secrets = _secret_fields(item["type"], item["mode"])
    config = dict(item.get("config") or {})
    for key in secrets:
        if config.get(key):
            config[key] = decrypt_value(str(config[key]))
    return {**item, "config": config}


async def list_connectors(public: bool = False) -> list[dict[str, Any]]:
    async with _lock:
        items = _load_raw()
    return [public_connector(item) for item in items] if public else [_decrypt(item) for item in items]


async def get_connector(connector_id: str) -> dict[str, Any] | None:
    async with _lock:
        items = _load_raw()
    found = next((item for item in items if item["id"] == connector_id), None)
    return _decrypt(found) if found else None


async def upsert_connector(payload: dict[str, Any], allow_incomplete: bool = False) -> dict[str, Any]:
    """`allow_incomplete` is used by the settings importer: the document cannot carry secrets, so the
    connector lands disabled with its required secret fields still blank."""
    type_id = str(payload.get("type") or "")
    definition = connector_type(type_id)
    mode = str(payload.get("mode") or next(iter(definition.modes)))
    if mode not in definition.modes:
        raise ValueError(f"{definition.label} does not support the '{mode}' mode")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    allowed, secrets = _allowed_fields(type_id, mode), _secret_fields(type_id, mode)
    incoming = {key: value for key, value in (payload.get("config") or {}).items() if key in allowed}
    async with _lock:
        items = _load_raw()
        connector_id = payload.get("id") or str(uuid4())
        current = next((item for item in items if item["id"] == connector_id), None)
        if payload.get("id") and not current:
            raise ValueError("Connector does not exist")
        stored = dict((current or {}).get("config") or {}) if current and current.get("mode") == mode else {}
        for key, value in incoming.items():
            if key in secrets:
                text = str(value or "")
                if text:  # a blank secret on edit keeps whatever is already stored
                    stored[key] = encrypt_value(text)
            else:
                stored[key] = value
        missing = [spec.key for spec in definition.modes[mode] if not spec.optional and not str(stored.get(spec.key) or "").strip()]
        if missing and not allow_incomplete:
            raise ValueError(f"Missing required field(s): {', '.join(missing)}")
        now = _now()
        merged = {
            "id": connector_id,
            "name": name[:200],
            "type": type_id,
            "mode": mode,
            "disabled": True if (missing and allow_incomplete) else bool(payload.get("disabled", (current or {}).get("disabled", False))),
            "status": (current or {}).get("status", "unknown"),
            "status_detail": (current or {}).get("status_detail", ""),
            "last_tested": (current or {}).get("last_tested"),
            "created_at": (current or {}).get("created_at", now),
            "updated_at": now,
            "config": stored,
        }
        if current:
            items[items.index(current)] = merged
        else:
            items.append(merged)
        _write_atomic(items)
    return public_connector(merged)


async def delete_connector(connector_id: str) -> bool:
    async with _lock:
        items = _load_raw()
        remaining = [item for item in items if item["id"] != connector_id]
        if len(remaining) == len(items):
            return False
        _write_atomic(remaining)
        return True


async def update_connector_status(connector_id: str, ok: bool, detail: str) -> None:
    async with _lock:
        items = _load_raw()
        for item in items:
            if item["id"] == connector_id:
                item.update(status="ok" if ok else "error", status_detail=detail[:500], last_tested=_now(), updated_at=_now())
        _write_atomic(items)


def _runtime_config(connector: dict[str, Any]) -> dict[str, Any]:
    return {**(connector.get("config") or {}), "mode": connector["mode"], "connector_id": connector["id"], "connector_name": connector.get("name", "")}


async def send_via_connector(connector: dict[str, Any], message: Message) -> dict[str, Any]:
    if connector.get("disabled"):
        raise ConnectorError("Connector is disabled")
    return await connector_type(connector["type"]).send(_runtime_config(connector), message)


async def test_connector(connector: dict[str, Any]) -> dict[str, Any]:
    return await connector_type(connector["type"]).test(_runtime_config(connector))
