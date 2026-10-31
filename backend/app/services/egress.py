"""Network egress policy (SSRF protection) for every outbound integration request."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from ..core.config import get_settings

BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal", "metadata.azure.com", "instance-data"}
METADATA_IPS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


class EgressDenied(Exception):
    pass


def _allowlisted(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in get_settings().egress_allowlist)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def domain_allowed(host: str, allowed_domains: list[str]) -> bool:
    """Organisation destination policy. An empty list means any public destination is acceptable."""
    if not allowed_domains:
        return True
    host = host.lower()
    return any(host == d.lower().lstrip(".") or host.endswith("." + d.lower().lstrip(".")) for d in allowed_domains)


def _check_ip(ip: ipaddress._BaseAddress, host: str) -> None:
    if ip in METADATA_IPS:
        raise EgressDenied(f"{host} resolves to a cloud metadata address ({ip})")
    if not ip.is_global:
        raise EgressDenied(f"{host} resolves to a private or reserved address ({ip})")


def static_check(url: str) -> None:
    """Checks that need no DNS: scheme, credentials-in-URL, literal private IPs, blocked names."""
    if _allowlisted(url):
        return
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise EgressDenied(f"Scheme {parts.scheme!r} is not allowed")
    if parts.scheme != "https":
        raise EgressDenied("Plain HTTP is only allowed for allow-listed internal destinations; use HTTPS")
    if parts.username or parts.password:
        raise EgressDenied("Credentials embedded in URLs are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise EgressDenied("URL has no host")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise EgressDenied(f"Host {host!r} is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    _check_ip(ip, host)


def check_url(url: str, allowed_domains: list[str] | None = None) -> None:
    """Full check before a live request: static rules, organisation domain policy, DNS resolution."""
    static_check(url)
    if _allowlisted(url):
        return
    host = host_of(url)
    if not domain_allowed(host, allowed_domains or []):
        raise EgressDenied(f"{host} is not in the organisation's allowed destinations")
    try:
        port = urlsplit(url).port or 443
    except ValueError:  # e.g. ":99999"
        raise EgressDenied("The URL has an invalid port")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise EgressDenied(f"Cannot resolve {host}")
    for info in infos:
        _check_ip(ipaddress.ip_address(info[4][0]), host)
