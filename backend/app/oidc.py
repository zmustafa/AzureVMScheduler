"""OpenID Connect relying party.

Works with any compliant issuer — Microsoft Entra ID, Okta, Auth0, Google, Keycloak — by reading
the provider's discovery document rather than hard-coding Microsoft endpoints. A provider of type
``entra`` is just an OIDC provider whose issuer is derived from its directory (tenant) ID.

Provider configuration (``identity_providers.config_json``):
  issuer                 : the OIDC issuer URL (derived from tenant_id for the entra type)
  discovery_url          : optional override; defaults to <issuer>/.well-known/openid-configuration
  tenant_id              : Entra directory id (entra type only; also pins the ID token's tid claim)
  client_id              : application/client id
  client_secret_encrypted: Fernet-encrypted client secret
  scopes                 : space separated, defaults to "openid profile email"
  group_claim            : ID token claim carrying group membership, defaults to "groups"
  group_role_map         : { "<idp group>": "<role name>" }
  auto_provision         : create users on first successful sign-in
  default_role           : role granted to a provisioned user with no group match
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import HTTPException, Request

from .config import get_settings
from .connections import decrypt_value, encrypt_value
from .models import IdentityProvider

#: Discovery documents are stable; re-fetching one on every sign-in would add a round trip and make
#: the identity provider a hard dependency of rendering the login page.
_DISCOVERY_TTL_SECONDS = 3600
_DISCOVERY_CACHE_MAX = 32
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}

DEFAULT_SCOPES = "openid profile email"
DEFAULT_GROUP_CLAIM = "groups"

#: The only shape an outbound identity-provider URL is allowed to take: https, a plain DNS
#: hostname or IP literal, an optional port, and a path/query drawn from the unreserved and
#: percent-encoded character sets. No userinfo, no other scheme, no control characters, no
#: whitespace. The whole string is matched against this before it is ever parsed, so a hostile
#: provider configuration cannot smuggle in a different scheme or a credential-bearing authority.
_SAFE_URL_PATTERN = (
    r"https://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?::[1-9][0-9]{0,4})?"
    r"(?:/[A-Za-z0-9._~\-/%]*)?"
    r"(?:\?[A-Za-z0-9._~\-/%&=+,:@]*)?"
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _is_public_address(host: str) -> bool:
    """True only when every address the host resolves to is a routable public address."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global or address.is_multicast or address.is_reserved:
            return False
    return True


async def require_public_https_url(url: str) -> str:
    """Guard every outbound identity-provider call against server-side request forgery.

    Endpoint URLs come from provider configuration and from the provider's own discovery
    document, so they are attacker-influenced from CodeQL's point of view and, more practically,
    from the point of view of anyone who can reach the connections admin. Two gates apply, in
    order: the string must match ``_SAFE_URL_PATTERN`` in its entirety, and the host must resolve
    only to publicly routable addresses. Together they keep the container from being used to probe
    the cloud metadata endpoint or anything else on the private network.
    """
    candidate = (url or "").strip()
    if not re.fullmatch(_SAFE_URL_PATTERN, candidate):
        raise HTTPException(status_code=400, detail="Identity provider endpoints must be absolute https URLs")
    parsed = urlparse(candidate)
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="Identity provider endpoints must be absolute https URLs")
    if not await asyncio.to_thread(_is_public_address, host):
        raise HTTPException(status_code=400, detail="Identity provider endpoints must resolve to a public address")
    return f"https://{parsed.netloc}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


def validate_return_url(value: str | None) -> str:
    if not value:
        return "/"
    if value.startswith("/") and not value.startswith("//"):
        return value
    parsed = urlparse(value)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if parsed.scheme in {"http", "https"} and origin in get_settings().return_origins:
        return value
    raise HTTPException(status_code=400, detail="Invalid return URL")


# -- configuration -------------------------------------------------------


def issuer_for(config: dict[str, Any]) -> str:
    """The issuer to trust. An Entra directory id implies the v2.0 issuer for that directory."""
    issuer = str(config.get("issuer") or "").strip().rstrip("/")
    if issuer:
        return issuer
    tenant_id = str(config.get("tenant_id") or "").strip()
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0" if tenant_id else ""


def discovery_url_for(config: dict[str, Any]) -> str:
    override = str(config.get("discovery_url") or "").strip()
    if override:
        return override
    issuer = issuer_for(config)
    return f"{issuer}/.well-known/openid-configuration" if issuer else ""


async def discover(config: dict[str, Any]) -> dict[str, Any]:
    url = discovery_url_for(config)
    if not url:
        raise HTTPException(status_code=503, detail="This provider has no issuer configured")
    cached = _discovery_cache.get(url)
    if cached and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    safe_url = await require_public_https_url(url)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(safe_url)
            response.raise_for_status()
            document = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="The identity provider's discovery document could not be read") from exc
    for required in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise HTTPException(status_code=502, detail=f"Discovery document is missing {required}")
    if len(_discovery_cache) >= _DISCOVERY_CACHE_MAX:
        _discovery_cache.clear()
    _discovery_cache[url] = (time.monotonic(), document)
    return document


def forget_discovery() -> None:
    """Drop the cache so an edited provider is re-read on the next sign-in."""
    _discovery_cache.clear()


def is_configured(provider: IdentityProvider) -> bool:
    config = provider.config_json or {}
    return bool(issuer_for(config) and config.get("client_id") and config.get("client_secret_encrypted"))


# -- PKCE and state ------------------------------------------------------


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def encode_state(payload: dict[str, Any]) -> str:
    return encrypt_value(json.dumps(payload, separators=(",", ":")))


def read_state(state: str) -> dict[str, Any]:
    try:
        payload = json.loads(decrypt_value(state))
        issued = int(payload["iat"])
        if int(datetime.now(timezone.utc).timestamp()) - issued > get_settings().oidc_state_ttl_seconds:
            raise ValueError("expired")
        payload["return_url"] = validate_return_url(payload.get("return_url"))
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired") from exc


# -- authorization -------------------------------------------------------


async def build_authorize_url(provider: IdentityProvider, redirect_uri: str, return_url: str | None) -> str:
    if not provider.enabled or not is_configured(provider):
        raise HTTPException(status_code=404, detail="This sign-in provider is not configured")
    config = provider.config_json or {}
    verifier, challenge = new_pkce()
    nonce = secrets.token_urlsafe(32)
    state = encode_state({
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "idp": provider.id,
        "return_url": validate_return_url(return_url),
        "verifier": verifier,
        "nonce": nonce,
    })
    document = await discover(config)
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": str(config.get("scopes") or DEFAULT_SCOPES),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize = await require_public_https_url(str(document["authorization_endpoint"]))
    separator = "&" if "?" in authorize else "?"
    return f"{authorize}{separator}{urlencode(params)}"


async def exchange_and_validate(
    provider: IdentityProvider,
    code: str,
    redirect_uri: str,
    state_payload: dict[str, Any],
) -> dict[str, Any]:
    """Trade the authorization code for an ID token and verify it end to end."""
    config = provider.config_json or {}
    if not config.get("client_secret_encrypted"):
        raise HTTPException(status_code=503, detail="This provider has no client secret configured")
    document = await discover(config)
    token_endpoint = await require_public_https_url(str(document["token_endpoint"]))
    jwks_uri = await require_public_https_url(str(document["jwks_uri"]))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            token_response = await client.post(
                token_endpoint,
                data={
                    "client_id": config["client_id"],
                    "client_secret": decrypt_value(config["client_secret_encrypted"]),
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": state_payload["verifier"],
                    "scope": str(config.get("scopes") or DEFAULT_SCOPES),
                },
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("Token response did not contain an ID token")
        header = jwt.get_unverified_header(id_token)
        key_data = next(item for item in jwks_response.json().get("keys", []) if item.get("kid") == header.get("kid"))
        claims = jwt.decode(
            id_token,
            key=jwt.PyJWK.from_dict(key_data).key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=config["client_id"],
            issuer=document["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "nonce"]},
        )
        if not secrets.compare_digest(str(claims.get("nonce", "")), str(state_payload["nonce"])):
            raise ValueError("ID token nonce does not match")
        # For an Entra provider the directory is pinned, so a token minted by a different directory
        # that happens to share the client id is still rejected.
        tenant_id = str(config.get("tenant_id") or "").strip()
        if tenant_id and str(claims.get("tid", "")).lower() != tenant_id.lower():
            raise ValueError("ID token directory does not match")
        if not (claims.get("oid") or claims.get("sub")):
            raise ValueError("ID token does not contain a stable subject")
        return claims
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, StopIteration, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Single sign-on could not be validated") from exc


def extract_identity(claims: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Normalise an ID token into the fields provisioning needs."""
    group_claim = str(config.get("group_claim") or DEFAULT_GROUP_CLAIM)
    raw_groups = claims.get(group_claim) or []
    if isinstance(raw_groups, str):
        raw_groups = [item.strip() for item in raw_groups.split(",") if item.strip()]
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    return {
        # Entra's immutable object id when present, otherwise the standard subject.
        "external_id": str(claims.get("oid") or claims.get("sub")),
        "email": str(email).strip().lower(),
        "display_name": str(claims.get("name") or claims.get("given_name") or "").strip(),
        "groups": [str(item) for item in raw_groups],
        # Entra does not emit email_verified; a directory-issued address is already vouched for.
        "email_verified": bool(claims.get("email_verified", True)),
    }


async def test_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Reach the issuer and report what worked, for the provider editor's Test button."""
    checks: list[dict[str, Any]] = []
    issuer = issuer_for(config)
    checks.append({
        "name": "Issuer", "ok": bool(issuer), "critical": True,
        "detail": issuer or "Set an issuer URL, or a directory (tenant) ID for Entra ID",
    })
    if not issuer:
        return checks
    try:
        document = await discover(config)
        checks.append({
            "name": "Discovery document", "ok": True, "critical": True,
            "detail": f"Authorization endpoint {document['authorization_endpoint']}",
        })
        jwks_uri = await require_public_https_url(str(document["jwks_uri"]))
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            jwks = await client.get(jwks_uri)
            jwks.raise_for_status()
            keys = len(jwks.json().get("keys", []))
        checks.append({
            "name": "Signing keys", "ok": keys > 0, "critical": True,
            "detail": f"{keys} key(s) published" if keys else "The provider published no signing keys",
        })
    except HTTPException as exc:
        checks.append({"name": "Discovery document", "ok": False, "critical": True, "detail": str(exc.detail)})
    except httpx.HTTPError as exc:
        checks.append({"name": "Signing keys", "ok": False, "critical": True, "detail": str(exc)})
    return checks


def callback_url(request: Request, provider_id: str) -> str:
    return str(request.url_for("oidc_callback", idp_id=provider_id))
