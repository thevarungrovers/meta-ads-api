"""Does an ad's destination actually load? A HEAD (or a streamed GET), nothing more.

NOT ON THE GRAPH TRANSPORT, on purpose: this calls the advertiser's own site,
not Meta, so it carries no token and is not logged as an API call. It sends
only HEAD, falling back to a streamed GET when a server refuses HEAD, and reads
no body. ``tests/test_readonly_guard.py`` pins that it names no write method.

SSRF GUARD. The URL comes from an agent, so it is treated as hostile: only
http(s), the host is resolved ONCE and every address it resolves to must be
globally routable -- no loopback, private, link-local (169.254.169.254 is the
cloud metadata service), carrier-grade NAT (100.64.0.0/10), multicast, reserved
or documentation range, and no IPv6 wrapper (mapped, 6to4, Teredo, NAT64)
around one. The request then CONNECTS TO THAT CHECKED ADDRESS, with the host
name sent as the Host header and, for https, as SNI and the name the
certificate is verified against -- so a DNS answer that changes between the
check and the connection (rebinding) cannot redirect it. A host with several
addresses has EVERY one checked, then they are tried in a stable order, IPv4
first, moving to the next only when the connection itself fails -- each still
pinned. An internationalised host is sent (Host, SNI, certificate name) in its
ASCII punycode form. Every redirect hop is
resolved, checked and pinned the same way before it is followed. No
credentials, cookies or proxies from the environment are sent, and each
request times out after 5 seconds.
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
    seen: list[str] = []
    for info in socket.getaddrinfo(host, None):
        if info[4][0] not in seen:
            seen.append(info[4][0])
    return seen


def ascii_host(host: str) -> str:
    """The host in the ASCII (punycode) form DNS, the Host header and TLS use.

    >>> ascii_host("bücher.example")
    'xn--bcher-kva.example'
    >>> ascii_host("example.com")
    'example.com'
    """
    try:
        host.encode("ascii")
        return host
    except UnicodeEncodeError:
        pass
    try:
        import idna  # the IDNA 2008 codec, when installed

        return idna.encode(host, uts46=True).decode("ascii")
    except ImportError:
        return host.encode("idna").decode("ascii")


def connect_order(addresses) -> list[str]:
    """IPv4 first, then IPv6, each in the order given, without duplicates.

    >>> connect_order(["2001:db8::1", "93.184.216.34", "93.184.216.34"])
    ['93.184.216.34', '2001:db8::1']
    """
    unique = list(dict.fromkeys(addresses))
    return ([a for a in unique if ":" not in a] + [a for a in unique if ":" in a])


def _new_session():
    import requests

    session = requests.Session()
    session.trust_env = False  # no proxies, no .netrc credentials
    return session


def _tls_adapter_class():
    from requests.adapters import HTTPAdapter

    class PinnedTLSAdapter(HTTPAdapter):
        """https to an IP literal, verified as ``hostname``: SNI and the
        certificate check both use the name, the socket the checked address."""

        def __init__(self, hostname: str) -> None:
            self._hostname = hostname
            super().__init__(max_retries=0)

        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs["server_hostname"] = self._hostname
            pool_kwargs["assert_hostname"] = self._hostname
            super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    return PinnedTLSAdapter


def _PinnedTLSAdapter(hostname: str):  # noqa: N802 - a class, built lazily
    return _tls_adapter_class()(hostname)


def _pinned_url(url: str, address: str) -> tuple[str, str]:
    """(``url`` with its host replaced by ``address``, the Host header)."""
    parsed = urlparse(url)
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    host = ascii_host(parsed.hostname or "")
    host_header = f"[{host}]" if ":" in host else host
    if parsed.port:
        host_header = f"{host_header}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl(), host_header


def _request_pinned(session, method: str, url: str, address: str, **kwargs):
    """Send ``method`` to ``url`` over a connection to ``address`` only.

    No second DNS lookup happens: the URL handed to the transport is the IP
    literal. https gets an adapter that verifies the certificate for the host
    NAME (a real session only; a test double has no ``mount``).
    """
    pinned, host_header = _pinned_url(url, address)
    parsed = urlparse(url)
    if parsed.scheme == "https" and hasattr(session, "mount"):
        prefix = f"https://{urlparse(pinned).netloc}/"
        session.mount(prefix, _PinnedTLSAdapter(ascii_host(parsed.hostname or "")))
    headers = {**(kwargs.pop("headers", None) or {}), "Host": host_header}
    return session.request(method, pinned, headers=headers, allow_redirects=False,
                           timeout=TIMEOUT_SECONDS, **kwargs)


def public_address(address: str) -> bool:
    """True for an address an outside website could legitimately have.

    >>> public_address("93.184.216.34")
    True
    >>> any(public_address(a) for a in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"))
    False
    """
    ip = ipaddress.ip_address(address)
    # An IPv6 address that only wraps an IPv4 one is judged by what it wraps.
    if ip.version == 6:
        if not all(public_address(str(inner)) for inner in _embedded_v4(ip)):
            return False
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and ip not in _SHARED_ADDRESS_SPACE
    )


#: RFC 6598 carrier-grade NAT space. Not global, and refused by name as well,
#: because inside a cloud provider it is where internal services live.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out = []
    if ip.ipv4_mapped:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour:
        out.append(ip.sixtofour)
    if ip.teredo:
        out.extend(ip.teredo)
    if ip in _NAT64:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def _vet(url: str, resolver) -> tuple[list[str], str | None]:
    """(the checked addresses to connect to, in order; the refusal, or None).

    The host is resolved exactly once here, EVERY address it resolves to must
    be public, and the ones returned (IPv4 first) are the only ones the
    request may connect to.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return [], f"only http(s) links are checked, not {parsed.scheme or 'no scheme'}"
    host = parsed.hostname
    if not host:
        return [], "the link has no host"
    if parsed.username or parsed.password:
        return [], "the link carries credentials"
    try:
        host = ascii_host(host)
    except UnicodeError as exc:
        return [], f"{host} is not a valid host name ({exc})"
    try:
        addresses = list(resolver(host))
    except OSError as exc:
        return [], f"{host} does not resolve ({exc})"
    except ValueError as exc:
        return [], f"{host} does not resolve ({exc})"
    if not addresses:
        return [], f"{host} does not resolve"
    try:
        private = [a for a in addresses if not public_address(a)]
    except ValueError:
        return [], f"{host} resolves to something that is not an IP address"
    if private:
        return [], f"{host} resolves to a non-public address ({', '.join(private)})"
    return connect_order(addresses), None


def _connect_failed(exc: Exception) -> bool:
    """A failure to REACH the address (try the next one), not an answer."""
    try:
        import requests

        if isinstance(exc, requests.exceptions.ConnectionError) and not isinstance(
                exc, requests.exceptions.ReadTimeout):
            return True
    except ImportError:  # pragma: no cover - requests is a dependency
        pass
    return isinstance(exc, ConnectionError)


def _request_any(session, method: str, url: str, addresses: list[str], **kwargs):
    """``method`` to the first vetted address that accepts a connection."""
    last: Exception | None = None
    for address in addresses:
        try:
            return _request_pinned(session, method, url, address, **dict(kwargs))
        except Exception as exc:  # noqa: BLE001 - classified below
            if not _connect_failed(exc):
                raise
            last = exc
    assert last is not None
    raise last


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
            addresses, refused = _vet(current, resolver)
            if refused:
                result.refused = refused
                result.final_url = current
                return result
            try:
                response = _request_any(session, "HEAD", current, addresses)
                if response.status_code in (405, 501):
                    response = _request_any(session, "GET", current, addresses, stream=True)
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
