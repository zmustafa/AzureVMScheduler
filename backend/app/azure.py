from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from azure.identity.aio import AzureCliCredential, CertificateCredential, ClientSecretCredential, DefaultAzureCredential

from .config import get_settings
from .connections import connection_policy, get_connection
from .validation import normalize_resource_id, parse_subscription_id, parse_vm_resource_id


ARM_SCOPE = "https://management.azure.com/.default"
ARM_API = "2024-07-01"
RESOURCE_GRAPH_API = "2022-10-01"
ARM_BASE = "https://management.azure.com"
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


class AzureTransientError(RuntimeError):
    """Throttling or a temporary Azure failure; safe to retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AzurePermanentError(RuntimeError):
    """Authentication, authorisation, or not-found failures; retrying cannot help."""


def _retry_after(response: httpx.Response) -> float | None:
    seconds = response.headers.get("retry-after")
    if seconds:
        try:
            return float(seconds)
        except ValueError:
            return None
    millis = response.headers.get("x-ms-retry-after-ms")
    try:
        return float(millis) / 1000 if millis else None
    except ValueError:
        return None


def raise_for_arm(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text[:300]
    if response.status_code in TRANSIENT_STATUS:
        raise AzureTransientError(f"{action} was throttled or failed temporarily ({response.status_code})", _retry_after(response))
    raise AzurePermanentError(f"{action} failed ({response.status_code}): {detail}")


async def credential_for(connection: dict[str, Any]):
    method = connection.get("auth_method")
    if method == "service_principal":
        return ClientSecretCredential(connection["tenant_id"], connection["client_id"], connection["client_secret"])
    if method == "service_principal_cert":
        return CertificateCredential(connection["tenant_id"], connection["client_id"], certificate_data=connection["certificate_pem"].encode())
    if method == "azure_cli":
        tenant_id = str(connection.get("tenant_id") or "")
        return AzureCliCredential(tenant_id=tenant_id) if tenant_id else AzureCliCredential()
    if method == "default_chain":
        return DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return None


async def arm_token(connection: dict[str, Any]) -> tuple[str, Any | None]:
    if connection.get("auth_method") == "az_cli_token":
        expires = connection.get("token_expires_at")
        if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")).astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Pasted Azure token is expired")
        raw = connection.get("access_token_json", "")
        try:
            parsed = json.loads(raw)
            token = parsed.get("accessToken") or parsed.get("access_token")
            if not token:
                raise ValueError("Token JSON does not contain accessToken")
            return token, None
        except json.JSONDecodeError:
            if not raw.strip():
                raise ValueError("Access token is empty")
            return raw, None
    credential = await credential_for(connection)
    if credential is None:
        raise ValueError("Credential is not configured")
    token = await credential.get_token(ARM_SCOPE)
    return token.token, credential


async def discover(connection: dict[str, Any]) -> list[dict[str, Any]]:
    token, credential = await arm_token(connection)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get("https://management.azure.com/subscriptions?api-version=2022-12-01", headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            return [{"id": item["subscriptionId"], "name": item["displayName"], "state": item.get("state")} for item in response.json().get("value", [])]
    finally:
        if credential:
            await credential.close()


def stop_operation(mode: str) -> str:
    """Deallocate releases the host and stops compute billing; powerOff leaves it allocated and billed."""
    return "deallocate" if mode == "deallocate" else "powerOff"


def action_allowed(connection: dict[str, Any] | None, action: str) -> bool:
    """Whether a tenant permits this action. Read-only and disabled connections permit nothing."""
    if not connection or connection.get("disabled") or connection.get("read_only"):
        return False
    return bool(connection.get("allow_vm_stop" if action == "stop" else "allow_vm_start"))


class MockVmAdapter:
    async def start_vm(self, resource_id: str) -> None:
        parse_vm_resource_id(resource_id)
        await asyncio.sleep(0)

    async def stop_vm(self, resource_id: str, mode: str = "deallocate") -> None:
        parse_vm_resource_id(resource_id)
        await asyncio.sleep(0)

    async def wait_until_running(self, _resource_id: str) -> str:
        await asyncio.sleep(0)
        return "running (mock)"

    async def wait_until_stopped(self, _resource_id: str, mode: str = "deallocate") -> str:
        await asyncio.sleep(0)
        return f"{'deallocated' if mode == 'deallocate' else 'stopped'} (mock)"

    async def close(self) -> None:
        return None


class ArmVmAdapter:
    def __init__(self, token: str, credential: Any | None) -> None:
        self.token = token
        self.credential = credential
        self.client = httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"})

    async def start_vm(self, resource_id: str) -> None:
        parse_vm_resource_id(resource_id)
        try:
            response = await self.client.post(f"{ARM_BASE}{resource_id}/start?api-version={ARM_API}")
        except httpx.HTTPError as exc:
            raise AzureTransientError(f"Azure start request failed: {type(exc).__name__}") from exc
        raise_for_arm(response, "Azure start")

    async def stop_vm(self, resource_id: str, mode: str = "deallocate") -> None:
        parse_vm_resource_id(resource_id)
        operation = stop_operation(mode)
        try:
            response = await self.client.post(f"{ARM_BASE}{resource_id}/{operation}?api-version={ARM_API}")
        except httpx.HTTPError as exc:
            raise AzureTransientError(f"Azure {operation} request failed: {type(exc).__name__}") from exc
        raise_for_arm(response, f"Azure {operation}")

    async def wait_until_running(self, resource_id: str) -> str:
        return await self._wait_for(resource_id, {"PowerState/running": "running"}, "running")

    async def wait_until_stopped(self, resource_id: str, mode: str = "deallocate") -> str:
        # powerOff settles on stopped; deallocate passes through stopped on its way to deallocated.
        wanted = {"PowerState/deallocated": "deallocated"}
        if mode != "deallocate":
            wanted["PowerState/stopped"] = "stopped"
        return await self._wait_for(resource_id, wanted, "deallocated" if mode == "deallocate" else "stopped")

    async def _wait_for(self, resource_id: str, wanted: dict[str, str], label: str) -> str:
        settings = get_settings()
        deadline = asyncio.get_running_loop().time() + settings.vm_monitor_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await self.client.get(f"{ARM_BASE}{resource_id}/instanceView?api-version={ARM_API}")
                if response.status_code < 400:
                    codes = [item.get("code", "") for item in response.json().get("statuses", [])]
                    for code, state in wanted.items():
                        if code in codes:
                            return state
                elif response.status_code not in TRANSIENT_STATUS:
                    raise_for_arm(response, "Azure power-state check")
            except httpx.HTTPError:
                pass
            await asyncio.sleep(settings.vm_monitor_interval_seconds)
        raise TimeoutError(f"Timed out waiting for VM to reach {label} state")

    async def close(self) -> None:
        await self.client.aclose()
        if self.credential:
            await self.credential.close()


async def list_virtual_machines(connection: dict[str, Any], subscription_id: str, max_results: int | None = None) -> list[dict[str, Any]]:
    """Live ARM inventory lookup; only ever called from the discovery endpoint."""
    parse_subscription_id(subscription_id)
    limit = max_results or get_settings().azure_discovery_max_results
    token, credential = await arm_token(connection)
    results: list[dict[str, Any]] = []
    url: str | None = f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines?api-version={ARM_API}&statusOnly=true"
    try:
        async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as client:
            while url and len(results) < limit:
                response = await client.get(url)
                raise_for_arm(response, "Azure virtual machine listing")
                payload = response.json()
                for item in payload.get("value", []):
                    resource_id = item.get("id", "")
                    statuses = ((item.get("properties") or {}).get("instanceView") or {}).get("statuses") or []
                    power = next((code.split("/", 1)[1] for code in (entry.get("code", "") for entry in statuses) if code.startswith("PowerState/")), None)
                    results.append({
                        "id": resource_id,
                        "name": item.get("name", ""),
                        "resource_group": resource_id.split("/resourceGroups/")[1].split("/")[0] if "/resourceGroups/" in resource_id else "",
                        "location": item.get("location", ""),
                        "power_state": power,
                    })
                    if len(results) >= limit:
                        break
                url = payload.get("nextLink")
    finally:
        if credential:
            await credential.close()
    return results


async def read_power_states(connection: dict[str, Any], subscription_id: str) -> dict[str, str]:
    """Current power state of every VM in a subscription, keyed by normalized resource id.

    Read-only: this deliberately ignores the VM-start safety gates, exactly like discovery.
    """
    parse_subscription_id(subscription_id)
    token, credential = await arm_token(connection)
    states: dict[str, str] = {}
    url: str | None = f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines?api-version={ARM_API}&statusOnly=true"
    try:
        async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as client:
            while url:
                response = await client.get(url)
                raise_for_arm(response, "Azure power-state scan")
                payload = response.json()
                for item in payload.get("value", []):
                    resource_id = item.get("id", "")
                    if not resource_id:
                        continue
                    statuses = ((item.get("properties") or {}).get("instanceView") or {}).get("statuses") or []
                    power = next((code.split("/", 1)[1] for code in (entry.get("code", "") for entry in statuses) if code.startswith("PowerState/")), None)
                    states[normalize_resource_id(resource_id)] = power or "unknown"
                url = payload.get("nextLink")
    finally:
        if credential:
            await credential.close()
    return states


async def resolve_vm_names(
    connection: dict[str, Any],
    names: list[str],
    subscription_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve bare VM names to full resource IDs across the tenant.

    Prefers Azure Resource Graph (one query for every subscription at once) and falls back to
    enumerating each visible subscription when Resource Graph is unavailable to this identity.
    Returns (candidates, source).
    """
    wanted = [item for item in {name.strip() for name in names} if item]
    if not wanted:
        return [], "resource_graph"
    token, credential = await arm_token(connection)
    try:
        try:
            return await _resolve_via_resource_graph(token, wanted, subscription_ids), "resource_graph"
        except AzureTransientError:
            raise
        except Exception:
            return await _resolve_via_subscription_scan(connection, token, wanted, subscription_ids), "subscription_scan"
    finally:
        if credential:
            await credential.close()


async def _resolve_via_resource_graph(token: str, names: list[str], subscription_ids: list[str] | None) -> list[dict[str, Any]]:
    quoted = ",".join("'" + item.replace("'", "''") + "'" for item in names)
    query = (
        "Resources | where type =~ 'microsoft.compute/virtualmachines' "
        f"| where name in~ ({quoted}) "
        "| project id, name, resourceGroup, subscriptionId, location "
        "| order by name asc"
    )
    results: list[dict[str, Any]] = []
    skip_token: str | None = None
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as client:
        for _ in range(20):
            body: dict[str, Any] = {"query": query, "options": {"$top": 1000, "resultFormat": "objectArray"}}
            if subscription_ids:
                body["subscriptions"] = subscription_ids
            if skip_token:
                body["options"]["$skipToken"] = skip_token
            response = await client.post(f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources?api-version={RESOURCE_GRAPH_API}", json=body)
            raise_for_arm(response, "Azure Resource Graph lookup")
            payload = response.json()
            for item in payload.get("data", []) or []:
                results.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "resource_group": item.get("resourceGroup", ""),
                    "subscription_id": item.get("subscriptionId", ""),
                    "location": item.get("location", ""),
                })
            skip_token = payload.get("$skipToken")
            if not skip_token:
                break
    return results


async def _resolve_via_subscription_scan(connection: dict[str, Any], token: str, names: list[str], subscription_ids: list[str] | None) -> list[dict[str, Any]]:
    if subscription_ids:
        targets = [parse_subscription_id(item) for item in subscription_ids]
    else:
        async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
            response = await client.get(f"{ARM_BASE}/subscriptions?api-version=2022-12-01")
            raise_for_arm(response, "Azure subscription listing")
            targets = [item["subscriptionId"] for item in response.json().get("value", [])]
    lookup = {item.casefold() for item in names}
    results: list[dict[str, Any]] = []
    for subscription in targets:
        for machine in await list_virtual_machines(connection, subscription):
            if machine["name"].casefold() in lookup:
                results.append({
                    "id": machine["id"],
                    "name": machine["name"],
                    "resource_group": machine["resource_group"],
                    "subscription_id": subscription,
                    "location": machine.get("location", ""),
                })
    return results


async def resolve_action_mode(connection_id: str | None, action: str = "start") -> tuple[dict[str, Any] | None, str]:
    """Decide, against *current* policy, whether this action may run for real — or refuse it.

    Deliberately separate from building an adapter. Adapters are cached because acquiring an ARM
    credential is expensive, but the permission decision must be re-made on every attempt: revoking
    a tenant's rights or flipping a global gate has to take effect immediately, not whenever a
    cached adapter happens to expire.
    """
    policy = await connection_policy(connection_id)
    settings = get_settings()
    server_enabled = settings.enable_real_azure_stops if action == "stop" else settings.enable_real_azure_starts
    if not server_enabled:
        return policy, "mock"
    if not policy:
        raise ValueError("No Azure connection is configured for this operation")
    if policy.get("disabled"):
        raise ValueError("The selected Azure connection is disabled")
    if policy.get("read_only"):
        raise ValueError("The selected Azure connection is read-only")
    if not action_allowed(policy, action):
        raise ValueError(f"The selected Azure connection does not allow VM {action}s")
    return policy, "real"


async def get_vm_adapter(connection_id: str | None, action: str = "start"):
    """Resolve an adapter for one action. Start and stop have entirely independent gates, so
    enabling starts never grants permission to stop anything."""
    _, mode = await resolve_action_mode(connection_id, action)
    connection = await get_connection(connection_id)
    if mode == "mock":
        return MockVmAdapter(), connection or {}, "mock"
    if not connection:
        # The file-backed registry can change between the policy check and credential load.
        raise ValueError("The selected Azure connection no longer exists")
    token, credential = await arm_token(connection)
    return ArmVmAdapter(token, credential), connection, "real"
