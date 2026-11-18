"""Does an ad's destination actually load? A HEAD (or a streamed GET), nothing more.

NOT ON THE GRAPH TRANSPORT, on purpose: this calls the advertiser's own site,
not Meta, so it carries no token and is not logged as an API call. It sends
only HEAD, falling back to a streamed GET when a server refuses HEAD, and reads
no body. ``tests/test_readonly_guard.py`` pins that it names no write method.

SSRF GUARD. The URL comes from an agent, so it is treated as hostile: only
http(s), the host is resolved first and every address it resolves to must be
public -- no loopback, private, link-local (169.254.169.254 is the cloud
metadata service), multicast or reserved range -- and every redirect hop is
checked again before it is followed. No credentials, cookies or proxies from
the environment are sent, and each request times out after 5 seconds.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 5.0


@dataclass
class LinkResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    hops: list[str] = field(default_factory=list)
    refused: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None and self.status is not None and self.status < 400

    @property
    def changed_host(self) -> bool:
        if not self.final_url:
            return False
        return (urlparse(self.url).hostname or "") != (urlparse(self.final_url).hostname or "")

    def describe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "redirects": len(self.hops),
            "refused": self.refused,
            "error": self.error,
        }


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def _new_session():
    import requests

    session = requests.Session()
    session.trust_env = False  # no proxies, no .netrc credentials
    return session


def public_address(address: str) -> bool:
    """True for an address an outside website could legitimately have.

    >>> public_address("93.184.216.34")
    True
    >>> any(public_address(a) for a in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"))
    False
    """
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _refusal(url: str, resolver) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) links are checked, not {parsed.scheme or 'no scheme'}"
    host = parsed.hostname
    if not host:
        return "the link has no host"
    if parsed.username or parsed.password:
        return "the link carries credentials"
    try:
        addresses = resolver(host)
    except OSError as exc:
        return f"{host} does not resolve ({exc})"
    if not addresses:
        return f"{host} does not resolve"
    private = [a for a in addresses if not public_address(a)]
    if private:
        return f"{host} resolves to a non-public address ({', '.join(private)})"
    return None


def with_url_tags(link: str, url_tags: str | None) -> str:
    """The URL a click actually lands on: the link with url_tags appended.

    >>> with_url_tags("https://example.com/a?x=1", "utm_source=meta")
    'https://example.com/a?x=1&utm_source=meta'
    >>> with_url_tags("https://example.com/a", None)
    'https://example.com/a'
    """
    if not url_tags:
        return link
    return f"{link}{'&' if '?' in link else '?'}{url_tags.lstrip('?&')}"


def check_link(url: str, *, session=None, resolver=None) -> LinkResult:
    """Follow ``url`` through up to 5 redirects and report where it ends."""
    resolver = resolver or _resolve
    result = LinkResult(url=url)
    current = url
    owned = session is None
    session = session or _new_session()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            refused = _refusal(current, resolver)
            if refused:
                result.refused = refused
                result.final_url = current
                return result
            try:
                response = session.request(
                    "HEAD", current, allow_redirects=False, timeout=TIMEOUT_SECONDS
                )
                if response.status_code in (405, 501):
                    response = session.request(
                        "GET", current, allow_redirects=False,
                        timeout=TIMEOUT_SECONDS, stream=True,
                    )
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            except Exception as exc:  # noqa: BLE001 - any transport failure is a finding
                result.error = f"{type(exc).__name__}: {exc}"
                result.final_url = current
                return result
            result.status = response.status_code
            location = (getattr(response, "headers", None) or {}).get("Location")
            if 300 <= response.status_code < 400 and location:
                current = urljoin(current, location)
                result.hops.append(current)
                continue
            result.final_url = current
            return result
        result.error = f"more than {MAX_REDIRECTS} redirects"
        result.final_url = current
        return result
    finally:
        if owned:
            close = getattr(session, "close", None)
            if callable(close):
                close()
