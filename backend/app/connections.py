from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .config import get_settings


AUTH_METHODS = {"azure_cli", "default_chain", "service_principal", "service_principal_cert", "az_cli_token"}
SECRET_FIELDS = {"client_secret", "certificate_pem", "access_token_json"}
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths() -> tuple[Path, Path]:
    data = get_settings().data_dir
    return data / "azure_connections.json", data / "secret.key"


#: A fixed application constant, not a secret. It only pins the derivation so the same passphrase
#: always yields the same key; the strength comes from the passphrase and the iteration count.
_KDF_SALT = b"azureops.secrets.fernet.v1"
_KDF_ITERATIONS = 480_000


def _derive_fernet_key(passphrase: str) -> bytes:
    """Turn an arbitrary passphrase into a valid Fernet key via PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT, iterations=_KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _fernet() -> Fernet:
    settings = get_settings()
    _, key_path = _paths()
    if settings.fernet_key:
        # Accept either a real Fernet key or any passphrase. Deployment templates and operators
        # cannot reliably produce 32 url-safe base64 bytes, so a plain string must work too.
        candidate = settings.fernet_key.strip()
        try:
            Fernet(candidate.encode())
            key = candidate.encode()
        except (ValueError, TypeError):
            key = _derive_fernet_key(candidate)
    elif key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    return Fernet(key)


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Encrypted value cannot be decrypted with the configured key") from exc


def _validate_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a valid UUID") from exc


def _token_expiration(raw: str) -> str | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Pasted tokens must use az account get-access-token JSON so expiration can be validated") from exc
    expires = parsed.get("expiresOn") or parsed.get("expires_on") or parsed.get("expiresOnTimestamp")
    if expires is None:
        raise ValueError("Pasted token JSON must include expiresOn or expiresOnTimestamp")
    try:
        if isinstance(expires, (int, float)) or str(expires).isdigit():
            value = datetime.fromtimestamp(int(expires), timezone.utc)
        else:
            value = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, TypeError, OSError) as exc:
        raise ValueError("Pasted token expiration is invalid") from exc
    if value <= datetime.now(timezone.utc):
        raise ValueError("Pasted Azure token is expired")
    return value.isoformat()


#: Every permission gate defaults to closed. A record written before a flag existed simply omits
#: it, and a missing gate must never read as permission granted.
PERMISSION_DEFAULTS: dict[str, Any] = {
    "allow_vm_start": False,
    "allow_vm_stop": False,
    "read_only": False,
    "disabled": False,
    "is_default": False,
}


def _load_raw() -> list[dict[str, Any]]:
    path, _ = _paths()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Azure connections store must contain a JSON array")
    return [{**PERMISSION_DEFAULTS, **item} for item in data]


def _write_atomic(items: list[dict[str, Any]]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="connections-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _decrypt(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    fernet = _fernet()
    for field in SECRET_FIELDS:
        value = result.get(field)
        if value:
            try:
                result[field] = fernet.decrypt(value.encode()).decode()
            except InvalidToken as exc:
                raise ValueError(f"Cannot decrypt {field}; check the Fernet key") from exc
    return result


def public_connection(item: dict[str, Any]) -> dict[str, Any]:
    safe = {key: value for key, value in item.items() if key not in SECRET_FIELDS}
    for field in SECRET_FIELDS:
        safe[f"has_{field}"] = bool(item.get(field))
    safe["client_secret_hint"] = "••••••••" if item.get("client_secret") else None
    return safe


async def list_connections(public: bool = False) -> list[dict[str, Any]]:
    async with _lock:
        items = _load_raw()
    return [public_connection(item) for item in items] if public else [_decrypt(item) for item in items]


async def get_connection(connection_id: str | None) -> dict[str, Any] | None:
    items = await list_connections()
    if connection_id:
        return next((item for item in items if item["id"] == connection_id), None)
    return next((item for item in items if item.get("is_default")), items[0] if items else None)


POLICY_FIELDS = ("id", "display_name", "allow_vm_start", "allow_vm_stop", "read_only", "disabled", "is_default")


async def connection_policy(connection_id: str | None) -> dict[str, Any] | None:
    """Just the permission flags, read fresh from the store and without decrypting any secrets.

    Safety gates are re-evaluated on every VM attempt, so this has to stay cheap — and it must
    always reflect what is on disk right now, never a cached copy.
    """
    async with _lock:
        items = _load_raw()
    if connection_id:
        found = next((item for item in items if item["id"] == connection_id), None)
    else:
        found = next((item for item in items if item.get("is_default")), items[0] if items else None)
    if not found:
        return None
    return {key: found.get(key) for key in POLICY_FIELDS}


async def resolve_enabled_connection(connection_id: str | None) -> dict[str, Any]:
    connection = await get_connection(connection_id)
    if not connection:
        raise ValueError("Select an Azure connection or configure an enabled default connection")
    if connection.get("disabled"):
        raise ValueError("The selected Azure connection is disabled")
    return connection


async def upsert_connection(payload: dict[str, Any]) -> dict[str, Any]:
    auth_method = payload.get("auth_method", "azure_cli")
    if auth_method not in AUTH_METHODS:
        raise ValueError("Unsupported auth_method")
    async with _lock:
        items = _load_raw()
        connection_id = payload.get("id") or str(uuid4())
        current = next((item for item in items if item["id"] == connection_id), None)
        if payload.get("id") and not current:
            raise ValueError("Connection does not exist")
        tenant_id = payload.get("tenant_id", (current or {}).get("tenant_id", ""))
        client_id = payload.get("client_id", (current or {}).get("client_id", ""))
        if tenant_id:
            _validate_uuid(tenant_id, "tenant_id")
        if client_id:
            _validate_uuid(client_id, "client_id")
        default_subscription = payload.get("default_subscription", (current or {}).get("default_subscription"))
        if default_subscription:
            _validate_uuid(str(default_subscription), "default_subscription")
        if auth_method in {"service_principal", "service_principal_cert"} and (not tenant_id or not client_id):
            raise ValueError("tenant_id and client_id are required for service principal authentication")
        if auth_method == "service_principal" and not payload.get("client_secret") and not (current or {}).get("client_secret"):
            raise ValueError("client_secret is required for service principal authentication")
        if auth_method == "service_principal_cert" and not payload.get("certificate_pem") and not (current or {}).get("certificate_pem"):
            raise ValueError("certificate_pem is required for certificate authentication")
        if auth_method == "az_cli_token" and not payload.get("access_token_json") and not (current or {}).get("access_token_json"):
            raise ValueError("access_token_json is required for pasted token authentication")
        now = _now()
        merged = dict(current or {})
        merged.update({key: value for key, value in payload.items() if key not in SECRET_FIELDS and value is not None})
        merged.update({"id": connection_id, "auth_method": auth_method, "display_name": payload.get("display_name", merged.get("display_name", "Azure tenant")), "tenant_id": payload.get("tenant_id", merged.get("tenant_id", "")), "allow_vm_start": bool(merged.get("allow_vm_start", False)), "allow_vm_stop": bool(merged.get("allow_vm_stop", False)), "read_only": bool(merged.get("read_only", False)), "is_default": bool(merged.get("is_default", not items)), "disabled": bool(merged.get("disabled", False)), "status": merged.get("status", "unknown"), "created_at": merged.get("created_at", now), "updated_at": now})
        for field in ("allow_vm_start", "allow_vm_stop", "read_only", "disabled", "is_default"):
            if payload.get(field) is not None:
                merged[field] = bool(payload[field])
        if merged["disabled"] and merged["is_default"]:
            raise ValueError("A disabled connection cannot be the default")
        if auth_method == "az_cli_token" and payload.get("access_token_json"):
            merged["token_expires_at"] = _token_expiration(str(payload["access_token_json"]))
        fernet = _fernet()
        for field in SECRET_FIELDS:
            if payload.get(field):
                merged[field] = fernet.encrypt(str(payload[field]).encode()).decode()
        if merged["is_default"]:
            for item in items:
                item["is_default"] = False
        if current:
            items[items.index(current)] = merged
        else:
            items.append(merged)
        _write_atomic(items)
    return public_connection(merged)


async def delete_connection(connection_id: str) -> bool:
    async with _lock:
        items = _load_raw()
        remaining = [item for item in items if item["id"] != connection_id]
        if len(remaining) == len(items):
            return False
        if remaining and not any(item.get("is_default") for item in remaining):
            remaining[0]["is_default"] = True
        _write_atomic(remaining)
        return True


async def set_default(connection_id: str) -> dict[str, Any]:
    async with _lock:
        items = _load_raw()
        found = None
        for item in items:
            if item["id"] == connection_id and item.get("disabled"):
                raise ValueError("A disabled connection cannot be the default")
            item["is_default"] = item["id"] == connection_id
            if item["is_default"]:
                found = item
        if not found:
            raise KeyError(connection_id)
        _write_atomic(items)
    return public_connection(found)


async def update_connection_status(connection_id: str, ok: bool, detail: str) -> None:
    async with _lock:
        items = _load_raw()
        for item in items:
            if item["id"] == connection_id:
                item.update(status="ok" if ok else "error", status_detail=detail[:500], last_tested=_now(), updated_at=_now())
        _write_atomic(items)
