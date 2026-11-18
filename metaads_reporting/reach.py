"""Reach estimates from ``act_/delivery_estimate`` -- a GET, nothing created.

It accepts custom audiences inside the targeting spec, which also makes it the
OVERLAP engine: Meta's Ads Manager overlap tool has no Marketing API endpoint,
so overlap(A, B) is estimated as est(A) + est(B) - est(A or B), on the midpoint
of each bound. Meta returns ROUNDED bounds, so every number derived here is an
estimate and says so. ``estimate_ready: false`` means "ask again shortly".
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .account_id import account_path


def estimate(client, targeting: dict[str, Any], optimization_goal: str = "REACH") -> dict[str, Any]:
    """{lower, upper, midpoint, ready} for one targeting spec."""
    answer = client.get(
        f"{account_path(client.resolve_account_id())}/delivery_estimate",
        targeting_spec=json.dumps(targeting),
        optimization_goal=optimization_goal,
    )
    data = (answer.get("data") or [{}])[0] or {}
    low = data.get("estimate_mau_lower_bound")
    high = data.get("estimate_mau_upper_bound")
    mid = None
    if low is not None and high is not None:
        mid = int((Decimal(str(low)) + Decimal(str(high))) / 2)
    return {
        "lower": low,
        "upper": high,
        "midpoint": mid,
        "daily_active": data.get("estimate_dau"),
        "ready": bool(data.get("estimate_ready", True)),
    }


def overlap_pct(a: int, b: int, union: int) -> float | None:
    """Share of the SMALLER audience that is also in the other, from midpoints.

    >>> overlap_pct(1000, 1000, 1500)
    50.0
    >>> overlap_pct(1000, 0, 1000) is None
    True
    """
    smaller = min(a, b)
    if smaller <= 0:
        return None
    both = max(a + b - union, 0)
    return round(min(both / smaller, 1.0) * 100, 1)
