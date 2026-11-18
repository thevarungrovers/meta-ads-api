"""Is the access token alive, and for how long? ``/debug_token``, cached.

The same inspection ``scripts/token_info.py`` prints, as data, so the server
can surface it in get_guardrails and get_account and refuse a write with a
dead token before it reaches Meta. Cached for an hour per client: the answer
changes on the scale of days, and a check per preview would be pure rate-limit
spend.

A System User token with no expiry reports ``expires: "never"``. No part of the
token itself ever appears in the result.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

#: Warn when the token dies within this many days.
WARN_DAYS = 14
CACHE_SECONDS = 3600

_cache: dict[int, tuple[float, dict[str, Any]]] = {}


def describe_expiry(timestamp: int | None) -> tuple[str, int | None]:
    """A human line and the days remaining, or None for a token with no expiry.

    The sign is spelled out rather than printed raw: a timestamp in the PAST
    gives a negative day count, and "(-5 days)" reads as a broken clock.

    >>> describe_expiry(0)
    ('never (no expiry set)', None)
    """
    if not timestamp:
        return ("never (no expiry set)", None)
    when = dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc)
    days = (when - dt.datetime.now(dt.timezone.utc)).days
    if days < 0:
        human = f"{abs(days)} days ago"
    elif days == 0:
        human = "today"
    else:
        human = f"in {days} days"
    return (f"{when.isoformat()} ({human})", days)


def inspect(client) -> dict[str, Any]:
    """Uncached: one ``/debug_token`` call, rendered."""
    try:
        payload = client.get("debug_token", input_token=client.settings.access_token)
    except Exception as exc:  # noqa: BLE001 - health is reported, never fatal here
        return {
            "status": "unknown",
            "detail": f"could not inspect the token: {str(exc).splitlines()[0]}",
        }
    data = payload.get("data") or {}
    expires, days_left = describe_expiry(data.get("expires_at"))
    access, access_days = describe_expiry(data.get("data_access_expires_at"))
    valid = data.get("is_valid")
    if valid is False:
        status = "expired"
    elif days_left is not None and days_left < 0:
        status = "expired"
    elif days_left is not None and days_left <= WARN_DAYS:
        status = "expiring"
    else:
        status = "ok"
    out = {
        "status": status,
        "type": data.get("type"),
        "valid": valid,
        "expires": "never" if days_left is None else expires,
        "days_left": days_left,
        "data_access_expires": "never" if access_days is None else access,
        "scopes": sorted(data.get("scopes") or []),
    }
    if status == "expiring":
        out["warning"] = (
            f"the token expires in {days_left} days; generate a new System User "
            "token before then (Business Settings > System Users)"
        )
    if status == "expired":
        out["warning"] = "the token is expired or invalid; every call will fail with code 190"
    if (data.get("type") or "").upper() not in ("", "SYSTEM_USER"):
        out["note"] = (
            "not a System User token: it dies the moment its owner changes their "
            "password (code 190, subcode 460)"
        )
    return out


def token_health(client, *, refresh: bool = False) -> dict[str, Any]:
    """:func:`inspect`, cached for an hour per client."""
    key = id(client)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and not refresh and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    result = inspect(client)
    _cache[key] = (now, result)
    return result
