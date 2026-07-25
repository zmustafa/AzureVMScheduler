"""Microsoft Teams incoming webhook: severity-coloured Adaptive Card."""

from __future__ import annotations

from typing import Any

import httpx

from .base import (
    ConnectorType,
    FieldSpec,
    Message,
    color_for,
    fact_pairs,
    http_client,
    raise_for_connector,
    require,
    require_https,
    severity_of,
    transport_error,
)


CARD_STYLE = {"info": "accent", "warning": "warning", "error": "attention", "critical": "attention"}

FIELDS = (
    FieldSpec("webhook_url", "Incoming webhook URL", type="password", secret=True, placeholder="https://zava.webhook.office.com/..."),
)


def build_card(message: Message) -> dict[str, Any]:
    severity = severity_of(message.severity)
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": message.title, "weight": "Bolder", "size": "Medium", "wrap": True, "color": "Attention" if severity in {"error", "critical"} else "Default"},
        {"type": "TextBlock", "text": message.body, "wrap": True},
    ]
    facts = fact_pairs(message)
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": title, "value": value} for title, value in facts]})
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [{"type": "Container", "style": CARD_STYLE[severity], "bleed": True, "items": body}],
    }
    if message.link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in AzureOps", "url": message.link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}], "summary": message.title, "themeColor": color_for(severity).lstrip("#")}


async def send(config: dict[str, Any], message: Message) -> dict[str, Any]:
    url, = require(config, "webhook_url")
    try:
        async with http_client() as client:
            response = await client.post(require_https(url, "webhook_url"), json=build_card(message))
    except httpx.HTTPError as exc:
        raise transport_error(exc, "Teams webhook post") from exc
    raise_for_connector(response, "Teams webhook post")
    return {"detail": "Posted an Adaptive Card to Teams", "external_ref": ""}


async def test(config: dict[str, Any]) -> dict[str, Any]:
    """Configuration check only; posting to a webhook would notify the channel."""
    url, = require(config, "webhook_url")
    require_https(url, "webhook_url")
    return {"detail": "Webhook URL looks valid; use Send test to post a card"}


CONNECTOR = ConnectorType(
    id="teams",
    label="Microsoft Teams",
    description="Post an Adaptive Card to a Teams channel through an incoming webhook.",
    modes={"webhook": FIELDS},
    send=send,
    test=test,
)
