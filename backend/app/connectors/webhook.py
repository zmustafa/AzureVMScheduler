"""Generic HTTPS webhook with an HMAC signature over timestamp + nonce + body (replay protection)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timezone
from typing import Any

import httpx

from ..models import utcnow
from .base import (
    ConnectorError,
    ConnectorType,
    FieldSpec,
    Message,
    http_client,
    raise_for_connector,
    require,
    require_https,
    severity_of,
    transport_error,
)


SIGNATURE_HEADER = "X-AzureOps-Signature"
TIMESTAMP_HEADER = "X-AzureOps-Timestamp"
NONCE_HEADER = "X-AzureOps-Nonce"

FIELDS = (
    FieldSpec("url", "Endpoint URL", type="url", placeholder="https://hooks.zava.com/azureops"),
    FieldSpec("custom_headers", "Custom headers", type="textarea", optional=True, placeholder='{"X-Env": "prod"}', help="JSON object"),
    FieldSpec("signing_secret", "Signing secret", type="password", secret=True, optional=True, help="Signs timestamp.nonce.body with HMAC-SHA256"),
)


def parse_headers(raw: Any) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConnectorError("custom_headers must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ConnectorError("custom_headers must be a JSON object")
    headers = {}
    for key, value in parsed.items():
        name, text_value = str(key).strip(), str(value)
        if not name or any(character in name + text_value for character in "\r\n"):
            raise ConnectorError("custom_headers may not contain line breaks or blank names")
        headers[name] = text_value
    return headers


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    payload = f"{timestamp}.{nonce}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def build_body(message: Message) -> dict[str, Any]:
    return {
        "event_type": message.event_type,
        "severity": severity_of(message.severity),
        "title": message.title,
        "body": message.body,
        "facts": message.facts,
        "link": message.link,
        "sent_at": utcnow().astimezone(timezone.utc).isoformat(),
    }


async def send(config: dict[str, Any], message: Message) -> dict[str, Any]:
    url, = require(config, "url")
    target = require_https(url, "url")
    headers = {"Content-Type": "application/json", **parse_headers(config.get("custom_headers"))}
    body = json.dumps(build_body(message), separators=(",", ":")).encode()
    secret = str(config.get("signing_secret") or "")
    if secret:
        timestamp, nonce = str(int(utcnow().timestamp())), secrets.token_hex(16)
        headers.update({TIMESTAMP_HEADER: timestamp, NONCE_HEADER: nonce, SIGNATURE_HEADER: f"sha256={sign(secret, timestamp, nonce, body)}"})
    try:
        async with http_client() as client:
            response = await client.post(target, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise transport_error(exc, "Webhook post") from exc
    raise_for_connector(response, "Webhook post")
    return {"detail": f"Delivered to the webhook ({response.status_code})", "external_ref": ""}


async def test(config: dict[str, Any]) -> dict[str, Any]:
    """Configuration check only; the endpoint is never called."""
    url, = require(config, "url")
    require_https(url, "url")
    parse_headers(config.get("custom_headers"))
    return {"detail": "Endpoint and headers look valid; use Send test to deliver a payload"}


CONNECTOR = ConnectorType(
    id="webhook",
    label="Webhook",
    description="POST a signed JSON payload to any HTTPS endpoint.",
    modes={"https": FIELDS},
    send=send,
    test=test,
)
