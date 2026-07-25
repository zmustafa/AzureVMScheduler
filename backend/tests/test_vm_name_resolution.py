from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.azure import AzurePermanentError, _resolve_via_resource_graph, resolve_vm_names


def _graph_response(request: httpx.Request, pages: list[dict[str, Any]], seen: list[dict[str, Any]]) -> httpx.Response:
    import json as _json

    body = _json.loads(request.content)
    seen.append(body)
    index = 1 if body.get("options", {}).get("$skipToken") else 0
    return httpx.Response(200, json=pages[index])


def _client_factory(handler: Any) -> Any:
    """Patch httpx.AsyncClient so azure.py talks to a mock transport instead of Azure."""
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return factory


async def test_resource_graph_query_quotes_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    pages = [
        {"data": [{"id": "/subscriptions/s1/resourceGroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm-web-01", "name": "vm-web-01", "resourceGroup": "rg-a", "subscriptionId": "s1", "location": "eastus"}], "$skipToken": "more"},
        {"data": [{"id": "/subscriptions/s2/resourceGroups/rg-b/providers/Microsoft.Compute/virtualMachines/vm-web-02", "name": "vm-web-02", "resourceGroup": "rg-b", "subscriptionId": "s2", "location": "westus"}]},
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(lambda request: _graph_response(request, pages, seen)))

    results = await _resolve_via_resource_graph("token", ["vm-web-01", "o'brien-vm"], ["s1", "s2"])

    assert [item["name"] for item in results] == ["vm-web-01", "vm-web-02"]
    assert results[0]["subscription_id"] == "s1"
    # Both pages were requested, and the second used the skip token.
    assert len(seen) == 2
    assert seen[1]["options"]["$skipToken"] == "more"
    # Single quotes in a name must be escaped so the KQL string literal stays valid.
    assert "'o''brien-vm'" in seen[0]["query"]
    assert seen[0]["subscriptions"] == ["s1", "s2"]


async def test_resolve_falls_back_to_subscription_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_token(_connection: dict[str, Any]) -> tuple[str, None]:
        return "token", None

    def handler(request: httpx.Request) -> httpx.Response:
        if "ResourceGraph" in str(request.url):
            return httpx.Response(403, text="ResourceGraph is not permitted")
        return httpx.Response(200, json={"value": [{"subscriptionId": "sub-1"}]})

    async def fake_list(_connection: dict[str, Any], subscription_id: str, _max: int | None = None) -> list[dict[str, Any]]:
        return [
            {"id": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/VM-Web-01", "name": "VM-Web-01", "resource_group": "rg", "location": "eastus", "power_state": "running"},
            {"id": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/other", "name": "other", "resource_group": "rg", "location": "eastus", "power_state": None},
        ]

    monkeypatch.setattr("app.azure.arm_token", fake_token)
    monkeypatch.setattr("app.azure.list_virtual_machines", fake_list)
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))

    results, source = await resolve_vm_names({}, ["vm-web-01"], None)

    assert source == "subscription_scan"
    # Matching is case-insensitive, and unrelated machines are excluded.
    assert [item["name"] for item in results] == ["VM-Web-01"]
    assert results[0]["subscription_id"] == "sub-1"


async def test_resolve_returns_nothing_for_blank_names() -> None:
    results, _ = await resolve_vm_names({}, ["", "   "], None)
    assert results == []


async def test_resource_graph_permanent_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(lambda _request: httpx.Response(404, text="no such API")))
    with pytest.raises(AzurePermanentError):
        await _resolve_via_resource_graph("token", ["vm-a"], None)
