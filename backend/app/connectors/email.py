"""Email connector: blocking SMTP pushed onto a thread, or Microsoft Graph sendMail."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx

from ..config import get_settings
from .base import (
    ConnectorError,
    ConnectorType,
    FieldSpec,
    Message,
    as_bool,
    as_int,
    http_client,
    raise_for_connector,
    require,
    transport_error,
)


GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

SMTP_FIELDS = (
    FieldSpec("smtp_host", "SMTP host", placeholder="smtp.zava.com"),
    FieldSpec("smtp_port", "SMTP port", type="number", placeholder="587"),
    FieldSpec("smtp_username", "Username", optional=True),
    FieldSpec("smtp_password", "Password", type="password", secret=True, optional=True),
    FieldSpec("from_address", "From address", placeholder="azureops@zava.com"),
    FieldSpec("to_addresses", "To", placeholder="ops@zava.com, oncall@zava.com", help="Comma separated"),
    FieldSpec("cc_addresses", "Cc", optional=True, help="Comma separated"),
    FieldSpec("use_starttls", "Use STARTTLS", type="checkbox", optional=True, help="Recommended on port 587"),
    FieldSpec("use_ssl", "Use implicit SSL", type="checkbox", optional=True, help="Use on port 465"),
    FieldSpec("timeout_seconds", "Timeout (seconds)", type="number", optional=True, placeholder="20"),
)

GRAPH_FIELDS = (
    FieldSpec("tenant_id", "Tenant ID", optional=True, help="Leave blank to reuse the Entra sign-in registration"),
    FieldSpec("client_id", "Client ID", optional=True, help="Leave blank to reuse the Entra sign-in registration"),
    FieldSpec("client_secret", "Client secret", type="password", secret=True, optional=True),
    FieldSpec("mailbox", "Mailbox / from address", placeholder="azureops@zava.com"),
    FieldSpec("to_addresses", "To", placeholder="ops@zava.com", help="Comma separated"),
    FieldSpec("cc_addresses", "Cc", optional=True, help="Comma separated"),
)


def clean_subject(value: str) -> str:
    """Header injection guard: a subject may never carry CR/LF."""
    return " ".join((value or "").replace("\r", " ").replace("\n", " ").split())[:512] or "Azure VM Scheduler notification"


def parse_recipients(raw: Any, label: str) -> list[str]:
    parts = [item.strip() for item in str(raw or "").replace(";", ",").split(",") if item.strip()]
    recipients: list[str] = []
    for part in parts:
        if "\r" in part or "\n" in part:
            raise ConnectorError(f"{label} contains a line break")
        _, address = parseaddr(part)
        if not address or address.count("@") != 1 or any(character.isspace() for character in address) or ("<" not in part and any(character.isspace() for character in part)):
            raise ConnectorError(f"{label} contains an invalid address: {part[:80]}")
        recipients.append(address)
    return recipients


def build_email(message: Message, sender: str, to: list[str], cc: list[str]) -> EmailMessage:
    mail = EmailMessage()
    mail["Subject"] = clean_subject(message.title)
    mail["From"] = sender
    mail["To"] = ", ".join(to)
    if cc:
        mail["Cc"] = ", ".join(cc)
    mail.set_content(message.body or message.title)
    if message.html:
        mail.add_alternative(message.html, subtype="html")
    return mail


def _smtp_send(config: dict[str, Any], mail: EmailMessage, recipients: list[str]) -> None:
    host, = require(config, "smtp_host")
    port = as_int(config.get("smtp_port"), 587)
    timeout = float(as_int(config.get("timeout_seconds"), int(get_settings().smtp_timeout_seconds)))
    username, password = str(config.get("smtp_username") or ""), str(config.get("smtp_password") or "")
    client: smtplib.SMTP
    if as_bool(config.get("use_ssl")):
        client = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if as_bool(config.get("use_starttls"), True) and not as_bool(config.get("use_ssl")):
            client.starttls()
            client.ehlo()
        if username:
            client.login(username, password)
        client.send_message(mail, to_addrs=recipients)
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            client.close()


def _smtp_probe(config: dict[str, Any]) -> str:
    host, = require(config, "smtp_host")
    port = as_int(config.get("smtp_port"), 587)
    timeout = float(as_int(config.get("timeout_seconds"), int(get_settings().smtp_timeout_seconds)))
    client = smtplib.SMTP_SSL(host, port, timeout=timeout) if as_bool(config.get("use_ssl")) else smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if as_bool(config.get("use_starttls"), True) and not as_bool(config.get("use_ssl")):
            client.starttls()
            client.ehlo()
        username = str(config.get("smtp_username") or "")
        if username:
            client.login(username, str(config.get("smtp_password") or ""))
        return f"Connected to {host}:{port}"
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            client.close()


async def _graph_credentials(config: dict[str, Any]) -> tuple[str, str, str]:
    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "")
    if tenant_id and client_id and client_secret:
        return tenant_id, client_id, client_secret
    from ..connections import decrypt_value
    from ..database import SessionLocal
    from ..models import IdentityProviderSettings

    async with SessionLocal() as session:
        provider = await session.get(IdentityProviderSettings, 1)
    if not provider or not provider.tenant_id or not provider.client_id or not provider.client_secret_encrypted:
        raise ConnectorError("Provide tenant_id, client_id, and client_secret, or configure the Entra sign-in registration first")
    return tenant_id or provider.tenant_id, client_id or provider.client_id, client_secret or decrypt_value(provider.client_secret_encrypted)


async def _graph_token(config: dict[str, Any]) -> str:
    tenant_id, client_id, client_secret = await _graph_credentials(config)
    payload = {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials", "scope": GRAPH_SCOPE}
    try:
        async with http_client() as client:
            response = await client.post(f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token", data=payload)
    except httpx.HTTPError as exc:
        raise transport_error(exc, "Microsoft Graph token request") from exc
    raise_for_connector(response, "Microsoft Graph token request")
    token = response.json().get("access_token")
    if not token:
        raise ConnectorError("Microsoft Graph did not return an access token")
    return str(token)


async def send(config: dict[str, Any], message: Message) -> dict[str, Any]:
    mode = str(config.get("mode") or "smtp")
    to = parse_recipients(config.get("to_addresses"), "to_addresses")
    cc = parse_recipients(config.get("cc_addresses"), "cc_addresses")
    if not to:
        raise ConnectorError("to_addresses must contain at least one recipient")
    subject = clean_subject(message.title)
    if mode == "m365_graph":
        mailbox, = require(config, "mailbox")
        parse_recipients(mailbox, "mailbox")
        token = await _graph_token(config)
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML" if message.html else "Text", "content": message.html or message.body},
                "toRecipients": [{"emailAddress": {"address": address}} for address in to],
                "ccRecipients": [{"emailAddress": {"address": address}} for address in cc],
            },
            "saveToSentItems": True,
        }
        try:
            async with http_client(headers={"Authorization": f"Bearer {token}"}) as client:
                response = await client.post(f"{GRAPH_BASE}/users/{mailbox}/sendMail", json=payload)
        except httpx.HTTPError as exc:
            raise transport_error(exc, "Microsoft Graph sendMail") from exc
        raise_for_connector(response, "Microsoft Graph sendMail")
        return {"detail": f"Sent to {len(to) + len(cc)} recipient(s) via Microsoft Graph", "external_ref": ""}

    sender, = require(config, "from_address")
    parse_recipients(sender, "from_address")
    mail = build_email(message, sender, to, cc)
    try:
        await asyncio.to_thread(_smtp_send, config, mail, to + cc)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused) as exc:
        raise ConnectorError(f"SMTP rejected the message: {type(exc).__name__}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ConnectorError(f"SMTP delivery failed: {type(exc).__name__}", transient=True) from exc
    return {"detail": f"Sent to {len(to) + len(cc)} recipient(s) via SMTP", "external_ref": ""}


async def test(config: dict[str, Any]) -> dict[str, Any]:
    """Auth probe only: connect and authenticate, never send a message."""
    if str(config.get("mode") or "smtp") == "m365_graph":
        require(config, "mailbox")
        await _graph_token(config)
        return {"detail": "Microsoft Graph token acquired"}
    try:
        detail = await asyncio.to_thread(_smtp_probe, config)
    except smtplib.SMTPAuthenticationError as exc:
        raise ConnectorError("SMTP authentication was rejected") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ConnectorError(f"SMTP connection failed: {type(exc).__name__}", transient=True) from exc
    return {"detail": detail}


CONNECTOR = ConnectorType(
    id="email",
    label="Email",
    description="Send run summaries over SMTP or Microsoft 365 (Graph sendMail).",
    modes={"smtp": SMTP_FIELDS, "m365_graph": GRAPH_FIELDS},
    send=send,
    test=test,
)
