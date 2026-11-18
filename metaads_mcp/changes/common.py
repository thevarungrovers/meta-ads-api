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
