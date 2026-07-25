"""Slack incoming webhook: Block Kit attachment with a severity colour bar."""

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
    transport_error,
)


FIELDS = (
    FieldSpec("webhook_url", "Incoming webhook URL", type="password", secret=True, placeholder="https://hooks.slack.com/services/..."),
)


def build_blocks(message: Message) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": message.title[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (message.body or message.title)[:2900]}},
    ]
    facts = fact_pairs(message)[:10]
    if facts:
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{title}*\n{value}"[:2000]} for title, value in facts]})
    if message.link:
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open in AzureOps"}, "url": message.link}]})
    return {"text": message.title, "attachments": [{"color": color_for(message.severity), "blocks": blocks}]}


async def send(config: dict[str, Any], message: Message) -> dict[str, Any]:
    url, = require(config, "webhook_url")
    try:
        async with http_client() as client:
            response = await client.post(require_https(url, "webhook_url"), json=build_blocks(message))
    except httpx.HTTPError as exc:
        raise transport_error(exc, "Slack webhook post") from exc
    raise_for_connector(response, "Slack webhook post")
    return {"detail": "Posted a Block Kit message to Slack", "external_ref": ""}


async def test(config: dict[str, Any]) -> dict[str, Any]:
    """Configuration check only; posting to a webhook would notify the channel."""
    url, = require(config, "webhook_url")
    require_https(url, "webhook_url")
    return {"detail": "Webhook URL looks valid; use Send test to post a message"}


CONNECTOR = ConnectorType(
    id="slack",
    label="Slack",
    description="Post a Block Kit message to a Slack channel through an incoming webhook.",
    modes={"webhook": FIELDS},
    send=send,
    test=test,
)
