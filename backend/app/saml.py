"""SAML 2.0 service provider.

Covers Microsoft Entra ID, Okta, ADFS and PingFederate. SP-initiated only: we emit an
AuthnRequest over HTTP-Redirect and validate the signed assertion the identity provider POSTs
back to the ACS endpoint. Signature verification uses signxml (lxml + cryptography wheels, so no
native xmlsec build is needed).

Provider configuration (``identity_providers.config_json``):
  entity_id      : the IdP's EntityID, expected as the assertion's Issuer
  sso_url        : the IdP's SSO redirect endpoint
  certificate    : the IdP's signing certificate (PEM or bare base64 DER)
  email_attr     : attribute carrying email (optional; common URIs are tried)
  name_attr      : attribute carrying display name (optional)
  group_attr     : attribute carrying group membership (optional)
  group_role_map : { "<idp group>": "<role name>" }
  auto_provision : create users on first successful sign-in
  default_role   : role granted to a provisioned user with no group match
"""

from __future__ import annotations

import base64
import json
import secrets
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request

from .connections import decrypt_value, encrypt_value
from .models import IdentityProvider

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}

RELAY_TTL_SECONDS = 600
#: Identity providers and this host rarely share a clock exactly; two minutes is the usual allowance.
_CLOCK_SKEW = timedelta(seconds=120)

_EMAIL_ATTRS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "urn:oid:0.9.2342.19200300.100.1.3",
    "email",
    "mail",
    "emailaddress",
)
_NAME_ATTRS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.microsoft.com/identity/claims/displayname",
    "displayName",
    "name",
)
_GROUP_ATTRS = (
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
    "http://schemas.xmlsoap.org/claims/Group",
    "groups",
    "memberOf",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_configured(provider: IdentityProvider) -> bool:
    config = provider.config_json or {}
    return bool(config.get("entity_id") and config.get("sso_url") and config.get("certificate"))


def public_base_url(request: Request) -> str:
    """The externally visible origin, which the IdP must see in metadata and the ACS URL."""
    from .config import get_settings

    configured = get_settings().base_url
    if configured and not configured.startswith("http://127.0.0.1") and not configured.startswith("http://localhost"):
        return configured.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


def sp_entity_id(base_url: str) -> str:
    return f"{base_url}/api/auth/saml/metadata"


def acs_url(base_url: str, idp_id: str) -> str:
    return f"{base_url}/api/auth/saml/{idp_id}/acs"


def sp_metadata(base_url: str, idp_id: str) -> str:
    """Minimal SP metadata. No SP signing key: we only consume signed assertions."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<md:EntityDescriptor xmlns:md="{NS["md"]}" entityID="{sp_entity_id(base_url)}">'
        '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true"'
        ' protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>'
        '<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
        f' Location="{acs_url(base_url, idp_id)}" index="0" isDefault="true"/>'
        '</md:SPSSODescriptor></md:EntityDescriptor>'
    )


# -- relay state ---------------------------------------------------------


def encode_relay(payload: dict[str, Any]) -> str:
    return encrypt_value(json.dumps(payload, separators=(",", ":")))


def decode_relay(token: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(decrypt_value(token))
        if int(_now().timestamp()) - int(payload["iat"]) > RELAY_TTL_SECONDS:
            return None
        return payload
    except Exception:
        return None


# -- request -------------------------------------------------------------


def build_authn_request(provider: IdentityProvider, base_url: str, return_url: str) -> str:
    config = provider.config_json or {}
    if not is_configured(provider):
        raise HTTPException(status_code=404, detail="This sign-in provider is not configured")
    request_id = f"_{secrets.token_hex(16)}"
    issued = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}"'
        f' ID="{request_id}" Version="2.0" IssueInstant="{issued}"'
        f' Destination="{config["sso_url"]}"'
        ' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
        f' AssertionConsumerServiceURL="{acs_url(base_url, provider.id)}">'
        f'<saml:Issuer>{sp_entity_id(base_url)}</saml:Issuer>'
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>'
        '</samlp:AuthnRequest>'
    )
    # HTTP-Redirect binding carries the request DEFLATE-compressed and base64 encoded.
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflated = compressor.compress(xml.encode()) + compressor.flush()
    relay = encode_relay({
        "iat": int(_now().timestamp()),
        "idp": provider.id,
        "request_id": request_id,
        "return_url": return_url,
    })
    query = urlencode({"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay})
    separator = "&" if "?" in str(config["sso_url"]) else "?"
    return f"{config['sso_url']}{separator}{query}"


# -- response ------------------------------------------------------------


def _pem(certificate: str) -> str:
    text = (certificate or "").strip()
    if "BEGIN CERTIFICATE" in text:
        return text
    body = "".join(text.split())
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def _attribute_values(assertion, wanted: tuple[str, ...], configured: str = "") -> list[str]:
    names = (configured,) + wanted if configured else wanted
    for name in names:
        for node in assertion.findall(f'.//{{{NS["saml"]}}}Attribute'):
            attr_name = node.get("Name") or ""
            friendly = node.get("FriendlyName") or ""
            if attr_name.lower() == name.lower() or friendly.lower() == name.lower():
                values = [
                    (value.text or "").strip()
                    for value in node.findall(f'{{{NS["saml"]}}}AttributeValue')
                    if (value.text or "").strip()
                ]
                if values:
                    return values
    return []


def validate_response(
    provider: IdentityProvider,
    saml_response: str,
    relay_state: str,
    base_url: str,
) -> tuple[dict[str, Any], str]:
    """Verify a posted SAML response and return the identity plus the return URL.

    The signature is verified first and every subsequent read is taken from the **verified**
    subtree signxml hands back — never from the raw document. That is what defeats signature
    wrapping, where an attacker keeps a validly signed assertion and bolts an unsigned forged one
    alongside it.
    """
    from lxml import etree
    from signxml import XMLVerifier

    config = provider.config_json or {}
    if not is_configured(provider):
        raise HTTPException(status_code=404, detail="This sign-in provider is not configured")

    relay = decode_relay(relay_state) or {}
    if relay.get("idp") != provider.id:
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired")

    try:
        raw = base64.b64decode(saml_response, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="SAML response could not be decoded") from exc

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        document = etree.fromstring(raw, parser=parser)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="SAML response is not well-formed XML") from exc

    try:
        verified = XMLVerifier().verify(document, x509_cert=_pem(str(config["certificate"]))).signed_xml
    except Exception as exc:
        raise HTTPException(status_code=401, detail="SAML assertion signature could not be verified") from exc

    # signxml returns whatever was signed: the assertion itself, or the response wrapping it.
    assertion = verified
    if verified.tag == f'{{{NS["samlp"]}}}Response':
        assertion = verified.find(f'{{{NS["saml"]}}}Assertion')
    if assertion is None or assertion.tag != f'{{{NS["saml"]}}}Assertion':
        raise HTTPException(status_code=401, detail="The signature did not cover a SAML assertion")

    issuer = assertion.findtext(f'{{{NS["saml"]}}}Issuer') or ""
    if issuer.strip() != str(config["entity_id"]).strip():
        raise HTTPException(status_code=401, detail="SAML assertion came from an unexpected issuer")

    expected_audience = sp_entity_id(base_url)
    audiences = [
        (node.text or "").strip()
        for node in assertion.findall(f'.//{{{NS["saml"]}}}Audience')
    ]
    if audiences and expected_audience not in audiences:
        raise HTTPException(status_code=401, detail="SAML assertion was issued for a different audience")

    now = _now()
    conditions = assertion.find(f'{{{NS["saml"]}}}Conditions')
    if conditions is not None:
        not_before = conditions.get("NotBefore")
        not_on_or_after = conditions.get("NotOnOrAfter")
        if not_before and _parse_instant(not_before) - _CLOCK_SKEW > now:
            raise HTTPException(status_code=401, detail="SAML assertion is not valid yet")
        if not_on_or_after and _parse_instant(not_on_or_after) + _CLOCK_SKEW <= now:
            raise HTTPException(status_code=401, detail="SAML assertion has expired")

    # Bind the assertion to the request we actually sent, so a captured assertion cannot be
    # replayed into a different sign-in.
    for confirmation in assertion.findall(f'.//{{{NS["saml"]}}}SubjectConfirmationData'):
        in_response_to = confirmation.get("InResponseTo")
        if in_response_to and relay.get("request_id") and in_response_to != relay["request_id"]:
            raise HTTPException(status_code=401, detail="SAML assertion does not match this sign-in request")

    name_id = (assertion.findtext(f'.//{{{NS["saml"]}}}NameID') or "").strip()
    emails = _attribute_values(assertion, _EMAIL_ATTRS, str(config.get("email_attr") or ""))
    names = _attribute_values(assertion, _NAME_ATTRS, str(config.get("name_attr") or ""))
    groups = _attribute_values(assertion, _GROUP_ATTRS, str(config.get("group_attr") or ""))
    email = (emails[0] if emails else name_id if "@" in name_id else "").strip().lower()
    if not name_id and not email:
        raise HTTPException(status_code=401, detail="SAML assertion carried no subject")

    identity = {
        "external_id": name_id or email,
        "email": email,
        "display_name": names[0] if names else "",
        "groups": groups,
        # The assertion is signed by the directory, so its addresses are as vouched-for as an
        # OIDC email_verified claim.
        "email_verified": True,
    }
    return identity, str(relay.get("return_url") or "/")


def _parse_instant(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def test_config(config: dict[str, Any], base_url: str, idp_id: str) -> list[dict[str, Any]]:
    """Static checks for the provider editor's Test button; SAML has nothing to call."""
    entity_id = str(config.get("entity_id") or "").strip()
    sso_url = str(config.get("sso_url") or "").strip()
    certificate = str(config.get("certificate") or "").strip()
    cert_ok = False
    cert_detail = "Paste the IdP's signing certificate"
    if certificate:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding

            loaded = x509.load_pem_x509_certificate(_pem(certificate).encode())
            loaded.public_bytes(Encoding.PEM)
            cert_ok = True
            cert_detail = f"Valid until {loaded.not_valid_after_utc:%Y-%m-%d}"
        except Exception as exc:
            cert_detail = f"Certificate could not be parsed: {type(exc).__name__}"
    return [
        {"name": "IdP Entity ID", "ok": bool(entity_id), "critical": True,
         "detail": entity_id or "Copy the Identifier / Issuer from the identity provider"},
        {"name": "IdP SSO URL", "ok": sso_url.startswith("https://"), "critical": True,
         "detail": sso_url or "Copy the login / SSO URL from the identity provider"},
        {"name": "Signing certificate", "ok": cert_ok, "critical": True, "detail": cert_detail},
        {"name": "Reply URL (ACS)", "ok": True, "critical": False, "detail": acs_url(base_url, idp_id)},
        {"name": "Identifier (Entity ID)", "ok": True, "critical": False, "detail": sp_entity_id(base_url)},
    ]
