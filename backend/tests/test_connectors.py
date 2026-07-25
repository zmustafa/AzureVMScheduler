from __future__ import annotations

import json

import httpx
import pytest

from app import connections
from app.connectors import email, servicenow, teams, webhook
from app.connectors import registry as registry_module
from app.connectors.base import ConnectorError, Message, raise_for_connector
from app.delivery import is_transient, retry_delay
from app.templating import build_message, render, render_html


@pytest.fixture
def registry_files(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "_paths", lambda: (tmp_path / "azure_connections.json", tmp_path / "secret.key"))
    monkeypatch.setattr(registry_module, "_path", lambda: tmp_path / "connectors.json")
    return tmp_path


def mock_http(handler, module):
    def factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


# -- templating --------------------------------------------------------


def test_placeholders_are_substituted_and_unknown_tokens_blanked() -> None:
    context = {"schedule_name": "Nightly", "failed": 6}
    assert render("{{schedule_name}} had {{failed}} failures ({{nope}})", context) == "Nightly had 6 failures ()"


def test_templating_never_evaluates_expressions() -> None:
    assert render("{{ __import__('os').getcwd() }}", {}) == "{{ __import__('os').getcwd() }}"


def test_html_rendering_escapes_untrusted_values() -> None:
    message = build_message(
        "run.failed",
        "error",
        "{{application}} failed",
        "{{failed_vm_names}}",
        {"application": "<script>alert(1)</script>", "failed_vm_names": ["vm<img src=x>"], "vm_count": 1, "succeeded": 0, "failed": 1},
        run_id="run-1",
    )
    assert message.title == "<script>alert(1)</script> failed"
    assert message.html is not None
    assert "<script>alert(1)</script>" not in message.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in message.html
    assert "vm&lt;img src=x&gt;" in message.html


def test_html_lists_failed_vms_and_links_back_to_the_run() -> None:
    html = render_html("Title", "Body", {"application": "Payments"}, "error", ["vm-a", "vm-b"], "https://example.test/runs/run-1")
    assert "Failed virtual machines" in html and "vm-a" in html and "vm-b" in html
    assert "https://example.test/runs/run-1" in html


# -- email -------------------------------------------------------------


def test_subject_strips_header_injection_attempts() -> None:
    assert email.clean_subject("Wave failed\r\nBcc: attacker@evil.test") == "Wave failed Bcc: attacker@evil.test"
    assert "\n" not in email.clean_subject("a\nb") and "\r" not in email.clean_subject("a\rb")


def test_recipients_with_line_breaks_are_rejected() -> None:
    with pytest.raises(ConnectorError, match="line break"):
        email.parse_recipients("ops@zava.com\nBcc: attacker@evil.test", "to_addresses")


def test_invalid_recipients_are_rejected() -> None:
    for value in ("not-an-address", "a@b@c.com", "ops@ zava.com"):
        with pytest.raises(ConnectorError, match="invalid address"):
            email.parse_recipients(value, "to_addresses")


def test_display_names_are_reduced_to_the_address() -> None:
    assert email.parse_recipients("Ops Team <ops@zava.com>, oncall@zava.com", "to_addresses") == ["ops@zava.com", "oncall@zava.com"]


def test_multipart_email_carries_text_and_html() -> None:
    message = Message(title="Wave failed", body="24/30 succeeded", severity="error", html="<p>24/30 succeeded</p>")
    mail = email.build_email(message, "azureops@zava.com", ["ops@zava.com"], ["audit@zava.com"])
    assert mail["Subject"] == "Wave failed" and mail["Cc"] == "audit@zava.com"
    assert {part.get_content_type() for part in mail.walk()} >= {"text/plain", "text/html"}


# -- servicenow --------------------------------------------------------


def _servicenow_config() -> dict[str, str]:
    return {"instance_url": "https://zava.service-now.com", "username": "svc", "password": "pw", "default_urgency": "2", "default_impact": "1", "default_assignment_group": "Cloud Ops", "default_caller_id": "azureops"}


async def test_incident_payload_maps_defaults_and_correlation(monkeypatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"result": []})
        return httpx.Response(201, json={"result": {"number": "INC0012345", "sys_id": "abc123"}})

    monkeypatch.setattr(servicenow, "http_client", mock_http(handler, servicenow))
    message = Message(title="Payments wave failed", body="6 of 30 VMs failed", severity="error", event_type="run.failed", correlation_key=servicenow.correlation_id("sched-1"))
    result = await servicenow.send(_servicenow_config(), message)
    payload = json.loads(captured[-1].content)
    assert payload["correlation_id"] == "azureops:sched-1"
    assert payload["short_description"] == "Payments wave failed"
    assert (payload["urgency"], payload["impact"], payload["assignment_group"], payload["caller_id"]) == ("2", "1", "Cloud Ops", "azureops")
    assert result["external_ref"] == "INC0012345"


async def test_repeat_failures_update_the_correlated_incident(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"result": [{"number": "INC0012345", "sys_id": "abc123", "state": "2"}]})
        return httpx.Response(200, json={"result": {"number": "INC0012345", "sys_id": "abc123"}})

    monkeypatch.setattr(servicenow, "http_client", mock_http(handler, servicenow))
    result = await servicenow.send(_servicenow_config(), Message(title="Failed again", body="still failing", event_type="run.failed", correlation_key="azureops:sched-1"))
    assert "POST" not in calls and "PATCH" in calls
    assert result["detail"] == "Updated incident INC0012345"


async def test_auto_resolve_closes_the_correlated_incident(monkeypatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"result": [{"number": "INC0012345", "sys_id": "abc123", "state": "2"}]})
        return httpx.Response(200, json={"result": {"number": "INC0012345"}})

    monkeypatch.setattr(servicenow, "http_client", mock_http(handler, servicenow))
    result = await servicenow.send(_servicenow_config(), Message(title="Recovered", body="All 30 VMs started", event_type="run.succeeded", correlation_key="azureops:sched-1", resolve=True))
    payload = json.loads(captured[-1].content)
    assert payload["state"] == "6" and payload["close_code"] and payload["close_notes"]
    assert result["detail"] == "Resolved incident INC0012345"


async def test_auto_resolve_is_a_no_op_without_an_open_incident(monkeypatch) -> None:
    monkeypatch.setattr(servicenow, "http_client", mock_http(lambda request: httpx.Response(200, json={"result": []}), servicenow))
    result = await servicenow.send(_servicenow_config(), Message(title="Recovered", body="", event_type="run.succeeded", correlation_key="azureops:sched-1", resolve=True))
    assert result["skipped"] is True


async def test_create_on_limits_which_events_open_incidents(monkeypatch) -> None:
    monkeypatch.setattr(servicenow, "http_client", mock_http(lambda request: httpx.Response(200, json={"result": []}), servicenow))
    config = {**_servicenow_config(), "create_on": "run.failed, schedule.missed"}
    assert (await servicenow.send(config, Message(title="Partial", body="", event_type="run.partially_failed", correlation_key="azureops:s1")))["skipped"] is True


def test_incident_numbers_are_validated() -> None:
    assert servicenow.validate_number("INC0012345") == "INC0012345"
    for value in ("", "INC^ORDERBYnumber", "INC 0012345", "a" * 41):
        with pytest.raises(ConnectorError):
            servicenow.validate_number(value)


async def test_servicenow_test_probe_is_read_only(monkeypatch) -> None:
    methods: list[str] = []
    monkeypatch.setattr(servicenow, "http_client", mock_http(lambda request: (methods.append(request.method), httpx.Response(200, json={"result": []}))[1], servicenow))
    await servicenow.test(_servicenow_config())
    assert methods == ["GET"]


def test_servicenow_send_test_is_not_offered() -> None:
    assert servicenow.CONNECTOR.allow_send_test is False


# -- webhook and chat --------------------------------------------------


async def test_webhook_signs_the_body_with_a_timestamp_and_nonce(monkeypatch) -> None:
    captured: list[httpx.Request] = []
    monkeypatch.setattr(webhook, "http_client", mock_http(lambda request: (captured.append(request), httpx.Response(200))[1], webhook))
    await webhook.send({"url": "https://hooks.example.test/azureops", "signing_secret": "topsecret", "custom_headers": '{"X-Env": "prod"}'}, Message(title="Wave failed", body="body", severity="error", event_type="run.failed"))
    request = captured[0]
    timestamp, nonce = request.headers[webhook.TIMESTAMP_HEADER], request.headers[webhook.NONCE_HEADER]
    assert request.headers["X-Env"] == "prod"
    assert request.headers[webhook.SIGNATURE_HEADER] == f"sha256={webhook.sign('topsecret', timestamp, nonce, request.content)}"


async def test_webhook_requires_https() -> None:
    with pytest.raises(ConnectorError, match="https"):
        await webhook.send({"url": "http://hooks.example.test/azureops"}, Message(title="t", body="b"))


def test_webhook_headers_must_be_a_json_object() -> None:
    with pytest.raises(ConnectorError, match="JSON object"):
        webhook.parse_headers("not json")
    with pytest.raises(ConnectorError, match="line breaks"):
        webhook.parse_headers('{"X-Bad": "a\\nb"}')


def test_teams_card_uses_the_severity_style_and_action_link() -> None:
    card = teams.build_card(Message(title="Payments failed", body="6 failed", severity="critical", facts={"failed": 6}, link="https://example.test/runs/run-1"))
    content = card["attachments"][0]["content"]
    assert content["body"][0]["style"] == "attention"
    assert content["actions"][0]["url"] == "https://example.test/runs/run-1"


# -- retry classification ---------------------------------------------


def test_only_transient_failures_are_retried() -> None:
    assert is_transient(ConnectorError("throttled", transient=True))
    assert is_transient(TimeoutError("slow"))
    assert is_transient(ConnectionError("reset"))
    assert not is_transient(ConnectorError("401 unauthorized"))
    assert not is_transient(ValueError("bad config"))


def test_http_status_decides_transience() -> None:
    for status in (429, 500, 503, 408):
        with pytest.raises(ConnectorError) as info:
            raise_for_connector(httpx.Response(status, text="busy"), "Post")
        assert info.value.transient is True
    for status in (400, 401, 403, 404, 422):
        with pytest.raises(ConnectorError) as info:
            raise_for_connector(httpx.Response(status, text="nope"), "Post")
        assert info.value.transient is False


def test_backoff_grows_and_stays_capped() -> None:
    assert retry_delay(1) < retry_delay(4)
    assert retry_delay(20) <= 900 + 5


# -- registry ----------------------------------------------------------


async def test_secrets_are_never_present_in_a_public_payload(registry_files) -> None:
    created = await registry_module.upsert_connector({"name": "Ops webhook", "type": "webhook", "mode": "https", "config": {"url": "https://hooks.example.test/a", "signing_secret": "super-secret-value"}})
    public = await registry_module.list_connectors(public=True)
    serialized = json.dumps([created, *public, registry_module.type_metadata()])
    assert "super-secret-value" not in serialized
    assert created["config"]["signing_secret_set"] is True
    assert "signing_secret" not in created["config"]
    assert (registry_files / "connectors.json").read_text(encoding="utf-8").find("super-secret-value") == -1


async def test_a_blank_secret_on_edit_keeps_the_stored_value(registry_files) -> None:
    created = await registry_module.upsert_connector({"name": "Ops webhook", "type": "webhook", "mode": "https", "config": {"url": "https://hooks.example.test/a", "signing_secret": "keep-me"}})
    await registry_module.upsert_connector({"id": created["id"], "name": "Renamed", "type": "webhook", "mode": "https", "config": {"url": "https://hooks.example.test/b", "signing_secret": ""}})
    stored = await registry_module.get_connector(created["id"])
    assert stored and stored["config"]["signing_secret"] == "keep-me"
    assert stored["config"]["url"] == "https://hooks.example.test/b"
    assert stored["name"] == "Renamed"


async def test_required_fields_are_enforced(registry_files) -> None:
    with pytest.raises(ValueError, match="Missing required field"):
        await registry_module.upsert_connector({"name": "Broken", "type": "email", "mode": "smtp", "config": {"smtp_host": "smtp.example.test"}})
    with pytest.raises(ValueError, match="Unsupported connector type"):
        await registry_module.upsert_connector({"name": "Nope", "type": "pagerduty", "config": {}})


def test_every_connector_type_declares_its_field_specs() -> None:
    for definition in registry_module.CONNECTOR_TYPES.values():
        assert definition.modes
        for specs in definition.modes.values():
            assert all(spec.key and spec.label for spec in specs)
