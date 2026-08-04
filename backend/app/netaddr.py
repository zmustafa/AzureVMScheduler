"""One definition of "who is calling", and the CIDR helpers built on top of it.

Both the per-IP login throttle and the IP allowlist decide what to do based on the caller's
address, so the address had better be derived the same way in both places — and derived safely.

The subtlety is ``X-Forwarded-For``. It is a list that each proxy *appends* to, so the entry a
given hop can vouch for is counted from the **right**, not the left. Reading the leftmost entry
is the classic mistake: it is whatever the client sent, so an attacker sets
``X-Forwarded-For: 10.0.0.1`` and inherits any trust placed in that address. Behind Azure
Container Apps there is exactly one trusted hop, so the real client is the last entry the ingress
appended — ``FORWARDED_HOPS`` (default 1) from the right.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

#: Stored addresses are bounded by the column width in `models`; keep the two in step.
MAX_IP_LENGTH = 64


def client_ip(request) -> str | None:
    """The caller's address, trusting a forwarded header only when configured to.

    Returns the peer address as a string, or None when it cannot be determined.
    """
    from .config import get_settings

    settings = get_settings()
    if settings.trust_forwarded_headers:
        chain = [item.strip() for item in (request.headers.get("x-forwarded-for") or "").split(",") if item.strip()]
        hops = max(1, settings.forwarded_hops)
        if len(chain) >= hops:
            # Counted from the right: only the hops we actually trust get a say.
            candidate = _strip_port(chain[-hops])
            if _parseable(candidate):
                return candidate[:MAX_IP_LENGTH]
    peer = request.client.host if request and request.client else None
    return peer[:MAX_IP_LENGTH] if peer else None


def client_address(request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`client_ip` parsed, for callers that need to match it against networks."""
    return parse_address(client_ip(request))


def parse_address(value: str | None):
    if not value:
        return None
    try:
        return ipaddress.ip_address(_strip_port(value))
    except ValueError:
        return None


def parse_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Normalize a rule.

    A bare address becomes a single-host network (/32 or /128). ``strict=False`` means
    ``10.0.0.5/24`` is accepted and stored as ``10.0.0.0/24`` rather than rejected, so a rule can
    never be saved in a form that reads differently from how it behaves.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("Enter an IP address or CIDR range")
    if "/" not in text:
        return ipaddress.ip_network(_strip_port(text), strict=False)
    return ipaddress.ip_network(text, strict=False)


def matches(address, networks: Iterable) -> bool:
    if address is None:
        return False
    for network in networks:
        # An IPv4 address is never in an IPv6 network and vice versa; versions must agree.
        if address.version == network.version and address in network:
            return True
    return False


def _strip_port(value: str) -> str:
    """Accept ``1.2.3.4:5678`` and ``[::1]:5678``, which some proxies emit."""
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            return text[1:end]
        return text
    if text.count(":") == 1 and "." in text:
        return text.split(":", 1)[0]
    return text


def _parseable(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
