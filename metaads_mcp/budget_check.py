"""Campaigns whose budget the daily total could never re-activate.

Run once per process, the first time get_guardrails is called -- not at
startup, where it spent the account's rate limit on every launch and its
warnings went to stderr, which the agent never reads.
"""

from __future__ import annotations

from typing import Any


def budget_cap_report(state) -> dict[str, Any]:
    """The check's result, computed once and kept on ``state``. A failure
    (usually a rate limit) is reported and retried on the next call."""
    cached = getattr(state, "budget_cap_cache", None)
    if cached is not None:
        return cached
    try:
        report: dict[str, Any] = {"warnings": budget_cap_warnings(state)}
    except Exception as exc:  # noqa: BLE001 - advisory; must not fail get_guardrails
        return {"unavailable": str(exc).splitlines()[0]}
    state.budget_cap_cache = report
    return report


def budget_cap_warnings(state) -> list[str]:
    """Campaigns whose budget is over max_projected_delta_per_day_total.

    An activation is counted in full against the daily total, so such a
    campaign could never be re-activated through this server. Said up front
    rather than discovered at the worst moment.
    """
    from metaads_mcp.changes.common import campaign_activation_bound
    from metaads_reporting.account_id import account_path
    from metaads_reporting.write import campaigns
    from metaads_reporting.write.lookup import edge_search

    ctx = state.engine.ctx
    currency = ctx.currency()
    ceiling = state.limits.max_projected_delta_per_day_total
    # One account-wide ad-set read, not one per campaign, asking only for what
    # the bound uses and only for ad sets switched on: Meta's per-account
    # limit is spent by CPU time per field read.
    by_campaign: dict[str, list[dict]] = {}
    switched_on, _ = edge_search(
        ctx.client, account_path(ctx.client.resolve_account_id(None)), "adsets",
        ("id", "campaign_id", "status", "daily_budget", "lifetime_budget",
         "budget_remaining", "end_time"),
        needle=None, limit=2000,
        statuses=["ACTIVE", "CAMPAIGN_PAUSED", "IN_PROCESS", "WITH_ISSUES"],
    )
    for child in switched_on:
        by_campaign.setdefault(str(child.get("campaign_id")), []).append(child)
    found = []
    for row in campaigns.search(ctx.client, None, limit=200):
        bound, why = campaign_activation_bound(
            ctx, row, currency, adsets=by_campaign.get(str(row.get("id")), [])
        )
        if bound > ceiling:
            found.append(
                f"campaign {row.get('id')} can spend {bound:.2f}/day ({why}), over "
                f"max_projected_delta_per_day_total = {ceiling}; it could not be "
                "re-activated here. Raise the total in guardrails.toml if that is wrong."
            )
    return found
