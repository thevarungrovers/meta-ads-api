"""Building blocks several change kinds share."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from metaads_mcp.changes.base import (
    SETTABLE_STATUSES,
    ChangeKind,
    amount_from_stored,
)
from metaads_mcp.guardrails import Check, check_ceiling, check_currency, check_pct_increase
from metaads_reporting.currency import format_money, from_minor_units


def wanted_status(new_status: str) -> str:
    wanted = (new_status or "").strip().upper()
    if wanted not in SETTABLE_STATUSES:
        raise ValueError(
            f"new_status must be ACTIVE or PAUSED, got {new_status!r}. "
            "DELETED and ARCHIVED are not available here: both are "
            "irreversible through the API, and PAUSED achieves the same "
            "thing reversibly."
        )
    return wanted


def money(row: dict[str, Any], field: str, currency: str) -> Decimal | None:
    return from_minor_units(row.get(field), currency)


def rendered(row: dict[str, Any], field: str, currency: str) -> str:
    return format_money(money(row, field, currency), currency)


def budget_checks(ctx, currency: str, before: Decimal | None, after: Decimal) -> list[Check]:
    return [
        check_currency(ctx.limits, currency),
        check_ceiling("max_daily_budget", after, ctx.limits.max_daily_budget, currency),
        check_pct_increase(
            "max_budget_change_pct", before, after, ctx.limits.max_budget_change_pct
        ),
    ]


def budget_warnings(before: Decimal | None, after: Decimal, what: str) -> list[str]:
    warnings = []
    if before and after < before:
        warnings.append(
            "lowering a daily budget can stop delivery part-way through "
            f"today if the {what} has already spent more than the new cap"
        )
    if before and after > before * 2:
        warnings.append(
            "more than doubling a budget re-enters the learning phase on "
            "most optimization goals; performance is unstable until it exits"
        )
    return warnings


def stored_amount(entry: dict[str, Any], key: str = "before_amount") -> Decimal:
    """The amount to put back: from inverse_json, or parsed off a legacy row."""
    inverse = entry.get("inverse") or {}
    if isinstance(inverse, dict) and inverse.get(key) is not None:
        return Decimal(str(inverse[key]))
    return amount_from_stored(entry["before"])


class StatusKind(ChangeKind):
    """Shared shape of the three status kinds: ACTIVE or PAUSED, nothing else."""

    entity_type: str = ""
    #: The write module (campaigns/adsets/ads) and its id keyword.
    module: Any = None
    id_arg: str = ""
    level: str = ""

    def find(self, ctx, entity_id: str) -> dict[str, Any]:
        return self.module.find(ctx.client, **{self.id_arg: str(entity_id)})

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self.find(ctx, payload["entity_id"]).get("status") or ""

    def build(self, ctx, payload: dict[str, Any]):
        return self.module.plan_set_status(
            ctx.client, status=payload["after"], **{self.id_arg: payload["entity_id"]}
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self.find(ctx, entry["entity_id"]).get("status")

    def inverse(self, ctx, entry: dict[str, Any]):
        return self.module.plan_set_status(
            ctx.client, status=entry["before"], **{self.id_arg: entry["entity_id"]}
        )

    def activation_bound(self, ctx, row: dict[str, Any], currency: str):  # pragma: no cover
        raise NotImplementedError

    def revert_projection(self, ctx, entry: dict[str, Any]):
        """Undoing a PAUSE resumes delivery: it projects what activating would.

        Not the inverse of the original's 0. Counted like any activation --
        against the daily and session totals, exempt from the per-change limit.
        """
        if entry.get("before") != "ACTIVE":
            return Decimal("0"), False
        row = self.find(ctx, entry["entity_id"])
        bound, _ = self.activation_bound(ctx, row, ctx.currency())
        return bound, bound > 0


# ---------------------------------------------------------------------------
# What activating something can spend
# ---------------------------------------------------------------------------


def _remaining_days(row: dict[str, Any], end_field: str) -> Decimal | None:
    import datetime as dt

    raw = row.get(end_field)
    if not raw:
        return None
    try:
        end = dt.datetime.fromisoformat(str(raw).replace("+0000", "+00:00"))
    except ValueError:
        return None
    seconds = (end - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return max(Decimal(str(seconds)) / Decimal(86400), Decimal("1"))


def daily_bound(row: dict[str, Any], currency: str, end_field: str) -> tuple[Decimal | None, str]:
    """An object's own daily spend ceiling: its daily budget, or its lifetime
    budget spread over the days it has left."""
    daily = money(row, "daily_budget", currency)
    if daily is not None:
        return daily, "its daily budget"
    lifetime = money(row, "lifetime_budget", currency)
    if lifetime is not None:
        remaining = money(row, "budget_remaining", currency)
        days = _remaining_days(row, end_field)
        pot = remaining if remaining is not None else lifetime
        if days:
            return (pot / days).quantize(Decimal("0.01")), "its lifetime budget left / days left"
        return pot, "its whole remaining lifetime budget (no end date)"
    return None, ""


def campaign_activation_bound(ctx, campaign: dict[str, Any], currency: str) -> tuple[Decimal, str]:
    """Upper bound on daily spend a campaign can resume."""
    from metaads_reporting.write import adsets as adset_writes

    own, why = daily_bound(campaign, currency, "stop_time")
    if own is not None:
        return own, f"upper bound: {why}"
    total = Decimal("0")
    counted = 0
    for child in adset_writes.search(ctx.client, None, campaign_id=str(campaign["id"]), limit=200):
        if (child.get("status") or "") != "ACTIVE":
            continue
        bound, _ = daily_bound(child, currency, "end_time")
        if bound is not None:
            total += bound
            counted += 1
    if counted:
        return total, f"upper bound: the summed budgets of its {counted} ACTIVE ad set(s)"
    return ctx.limits.max_daily_budget, "no budget readable; bounded by max_daily_budget"


def ad_set_activation_bound(ctx, adset: dict[str, Any], currency: str) -> tuple[Decimal, str]:
    """Upper bound on daily spend an ad set can resume."""
    from metaads_reporting.write import campaigns as campaign_writes

    own, why = daily_bound(adset, currency, "end_time")
    if own is not None:
        return own, f"upper bound: {why}"
    try:
        campaign = campaign_writes.find(ctx.client, campaign_id=str(adset.get("campaign_id")))
        parent, why = daily_bound(campaign, currency, "stop_time")
        if parent is not None:
            return parent, f"upper bound: the campaign budget it shares ({why})"
    except Exception:  # noqa: BLE001 - fall through to the configured ceiling
        pass
    return ctx.limits.max_daily_budget, "no budget readable; bounded by max_daily_budget"


def ad_activation_bound(ctx, ad: dict[str, Any], currency: str) -> tuple[Decimal, str]:
    """An ad restarts its ad set's spend only when it is the only active one."""
    from metaads_reporting.write import ads as ad_writes
    from metaads_reporting.write import adsets as adset_writes

    siblings = [
        a for a in ad_writes.search(ctx.client, None, ad_set_id=str(ad.get("adset_id")), limit=200)
        if str(a.get("id")) != str(ad.get("id")) and (a.get("status") or "") == "ACTIVE"
    ]
    if siblings:
        return Decimal("0"), (
            f"{len(siblings)} other ACTIVE ad(s) already spend the ad set's budget, "
            "which does not move"
        )
    adset = adset_writes.find(ctx.client, ad_set_id=str(ad.get("adset_id")))
    bound, why = ad_set_activation_bound(ctx, adset, currency)
    return bound, f"this would be the ad set's only ACTIVE ad, restarting its spend ({why})"
