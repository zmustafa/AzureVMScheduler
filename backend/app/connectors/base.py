"""Connector contracts: a typed field spec per mode plus an async send/test pair per type."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import get_settings


SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
SEVERITIES = tuple(SEVERITY_RANK)
SEVERITY_COLOR = {"info": "#2563eb", "warning": "#d97706", "error": "#dc2626", "critical": "#b91c1c"}
SEVERITY_LABEL = {"info": "Info", "warning": "Warning", "error": "Error", "critical": "Critical"}
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
_REDACT = re.compile(r"(?i)(client_secret|signing_secret|access_token|authorization|api[_-]?token|password|webhook_url)\s*[:=]\s*[^\s,;\"']+")


class ConnectorError(RuntimeError):
    """A delivery failure. `transient` decides whether the delivery pipeline retries."""

    def __init__(self, message: str, transient: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code


def sanitize_detail(value: object) -> str:
    return _REDACT.sub(r"\1=[redacted]", str(value))[:500] or "Operation failed"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: str = "text"
    placeholder: str = ""
    secret: bool = False
    optional: bool = False
    help: str = ""
    options: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "type": self.type, "placeholder": self.placeholder, "secret": self.secret, "optional": self.optional, "help": self.help, "options": list(self.options)}


@dataclass(frozen=True)
class Message:
    """One outbound notification. `html` is optional rich content, `link` points back at the run."""

    title: str
    body: str
    severity: str = "info"
    event_type: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    html: str | None = None
    link: str | None = None
    correlation_key: str | None = None
    resolve: bool = False


Sender = Callable[[dict[str, Any], Message], Awaitable[dict[str, Any]]]
Prober = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ConnectorType:
    id: str
    label: str
    description: str
    modes: dict[str, tuple[FieldSpec, ...]]
    send: Sender
    test: Prober
    allow_send_test: bool = True

    def secret_fields(self) -> set[str]:
        return {spec.key for specs in self.modes.values() for spec in specs if spec.secret}

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description, "allow_send_test": self.allow_send_test, "modes": {mode: [spec.as_dict() for spec in specs] for mode, specs in self.modes.items()}}


def require(config: dict[str, Any], *keys: str) -> list[str]:
    values = []
    for key in keys:
        value = str(config.get(key) or "").strip()
        if not value:
            raise ConnectorError(f"{key} is required for this connector")
        values.append(value)
    return values


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def severity_of(value: str) -> str:
    return value if value in SEVERITY_RANK else "info"


def color_for(severity: str) -> str:
    return SEVERITY_COLOR[severity_of(severity)]


def fact_pairs(message: Message) -> list[tuple[str, str]]:
    return [(str(key).replace("_", " ").title(), str(value)) for key, value in message.facts.items() if value not in (None, "", [])]


def http_client(timeout: float | None = None, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout or get_settings().connector_http_timeout_seconds, **kwargs)


def raise_for_connector(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    raise ConnectorError(f"{action} failed ({response.status_code}): {sanitize_detail(response.text[:200])}", transient=response.status_code in TRANSIENT_STATUS, status_code=response.status_code)


def transport_error(exc: Exception, action: str) -> ConnectorError:
    """Connection resets and timeouts are always worth retrying."""
    return ConnectorError(f"{action} failed: {type(exc).__name__}", transient=True)


def require_https(url: str, label: str = "url") -> str:
    candidate = (url or "").strip()
    if not candidate.lower().startswith("https://"):
        raise ConnectorError(f"{label} must be an https:// URL")
    return candidate
