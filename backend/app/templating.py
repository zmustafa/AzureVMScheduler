"""Placeholder-only templating. No expressions are ever evaluated, and HTML output is escaped."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from typing import Any

from .config import get_settings
from .connectors.base import SEVERITY_COLOR, SEVERITY_LABEL, Message, severity_of


PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
PLACEHOLDERS = (
    "event_type", "severity", "application", "ring", "schedule_name", "scheduled_for",
    "vm_count", "succeeded", "failed", "failed_vm_names", "tenant", "run_url", "error",
)

DEFAULT_SUBJECT = "[{{severity}}] {{event_type}} — {{schedule_name}}"
DEFAULT_BODY = (
    "{{event_type}} for {{schedule_name}} ({{application}} / {{ring}}).\n"
    "Scheduled for {{scheduled_for}} · {{succeeded}}/{{vm_count}} succeeded · {{failed}} failed.\n"
    "Failed VMs: {{failed_vm_names}}\n"
    "Tenant: {{tenant}}\n"
    "{{error}}\n"
    "{{run_url}}"
)


def render(template: str, context: Mapping[str, Any], html: bool = False) -> str:
    def replace(match: re.Match[str]) -> str:
        value = context.get(match.group(1))
        text = "" if value is None else str(value)
        return escape(text, quote=True) if html else text

    return PLACEHOLDER.sub(replace, template or "")


def run_url(run_id: str | None) -> str:
    return f"{get_settings().base_url}/runs/{run_id}" if run_id else get_settings().base_url


def build_context(facts: Mapping[str, Any], event_type: str, severity: str, run_id: str | None = None) -> dict[str, Any]:
    failed_names = facts.get("failed_vm_names") or []
    if isinstance(failed_names, str):
        failed_names = [failed_names]
    context = {key: "" for key in PLACEHOLDERS}
    context.update({key: value for key, value in facts.items() if key in PLACEHOLDERS and value is not None})
    context.update({
        "event_type": event_type,
        "severity": SEVERITY_LABEL[severity_of(severity)],
        "failed_vm_names": ", ".join(str(item) for item in failed_names),
        "run_url": str(facts.get("run_url") or "") or (run_url(run_id) if run_id else ""),
    })
    return context


def render_html(title: str, body: str, context: Mapping[str, Any], severity: str, failed_vms: list[Any] | None = None, link: str | None = None) -> str:
    color = SEVERITY_COLOR[severity_of(severity)]
    rows = "".join(f"<tr><td style='padding:4px 10px;border-bottom:1px solid #e5e7eb;font-family:monospace'>{escape(str(item))}</td></tr>" for item in (failed_vms or []))
    table = f"<table style='border-collapse:collapse;margin-top:12px;font-size:13px'><thead><tr><th style='text-align:left;padding:4px 10px;background:#f3f4f6'>Failed virtual machines</th></tr></thead><tbody>{rows}</tbody></table>" if rows else ""
    facts = "".join(
        f"<tr><td style='padding:2px 10px 2px 0;color:#6b7280'>{escape(str(key).replace('_', ' ').title())}</td><td style='padding:2px 0'>{escape(str(value))}</td></tr>"
        for key, value in context.items()
        if value not in (None, "") and key not in {"run_url", "failed_vm_names", "error"}
    )
    action = f"<p style='margin:16px 0 0'><a href='{escape(str(link), quote=True)}' style='background:{color};color:#ffffff;padding:8px 14px;border-radius:6px;text-decoration:none;font-size:13px'>Open in Azure VM Scheduler</a></p>" if link else ""
    return (
        "<html><body style='margin:0;background:#f9fafb;font-family:Segoe UI,Arial,sans-serif;color:#111827'>"
        f"<div style='max-width:640px;margin:0 auto;padding:20px'>"
        f"<div style='border-left:4px solid {color};background:#ffffff;border-radius:8px;padding:18px'>"
        f"<h2 style='margin:0 0 8px;font-size:17px'>{escape(title)}</h2>"
        f"<p style='margin:0;white-space:pre-line;font-size:14px;line-height:1.5'>{escape(body)}</p>"
        f"<table style='border-collapse:collapse;margin-top:14px;font-size:13px'>{facts}</table>"
        f"{table}{action}</div></div></body></html>"
    )


def build_message(
    event_type: str,
    severity: str,
    title: str,
    body: str,
    facts: Mapping[str, Any],
    run_id: str | None = None,
    correlation_key: str | None = None,
    resolve: bool = False,
) -> Message:
    context = build_context(facts, event_type, severity, run_id)
    link = context.get("run_url") or None
    rendered_title = render(title or DEFAULT_SUBJECT, context)
    rendered_body = render(body or DEFAULT_BODY, context)
    failed_names = facts.get("failed_vm_names") or []
    if isinstance(failed_names, str):
        failed_names = [failed_names]
    return Message(
        title=rendered_title,
        body=rendered_body,
        severity=severity_of(severity),
        event_type=event_type,
        facts={key: value for key, value in facts.items() if key != "failed_vm_names" and value not in (None, "", [])},
        html=render_html(rendered_title, rendered_body, context, severity, list(failed_names), link),
        link=link,
        correlation_key=correlation_key,
        resolve=resolve,
    )
