"""Status, daily budget and bid: the nine original pairs' money-and-delivery half."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from metaads_mcp.changes.base import (
    APPLY_REVERSIBLE,
    APPLY_SPENDS,
    ChangeKind,
    Proposal,
    amount_from_stored,
    parse_money,
)
from metaads_mcp.changes.common import (
    bid_change_projection,
    StatusKind,
    ad_activation_bound,
    ad_set_activation_bound,
    campaign_activation_bound,
    budget_checks,
    budget_warnings,
    money,
    rendered,
    stored_amount,
    wanted_status,
)
from metaads_mcp.guardrails import check_ceiling, check_currency, check_pct_increase
from metaads_mcp.previews import pct_change, project_budget_change
from metaads_reporting.currency import format_money
from metaads_reporting.write import ads, adsets, campaigns

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class CampaignStatus(StatusKind):
    name = "campaign_status"
    preview_title = "Preview pausing or activating a campaign"
    apply_title = "Apply a campaign status change (AFFECTS DELIVERY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = (("campaign", "status"),)
    entity_type = "campaign"
    module = campaigns
    id_arg = "campaign_id"
    level = "campaign"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> Proposal:
        """Show what pausing or activating a campaign would do. Writes nothing.

        This is the widest change available here: it stops or starts every ad
        set and ad underneath.
        """
        wanted = wanted_status(new_status)
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        currency = ctx.currency_of(row)
        before = row.get("status") or ""
        metrics = ctx.metrics_7d(str(row["id"]), "campaign")

        bound_note = ""
        if wanted == "PAUSED":
            projected = Decimal("0")
        else:
            # Re-activating can resume spend up to the budget -- the budget, not
            # past spend: a campaign that spent nothing last week (an ABO one,
            # a lifetime one, a new one) can still spend it all tomorrow.
            projected, bound_note = campaign_activation_bound(ctx, row, currency)

        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if wanted == "ACTIVE":
            warnings.append(
                "activating resumes delivery immediately for every ACTIVE ad "
                "set and ad underneath"
            )
        if wanted == "ACTIVE" and bound_note:
            warnings.append(f"projected delta: {bound_note}")
        return Proposal(
            entity_type="campaign",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=row.get("name") or "",
            field_name="status",
            before=before,
            after=wanted,
            checks=[],
            projected=projected,
            payload={"entity_id": str(row["id"])},
            status=before,
            effective_status=row.get("effective_status"),
            metrics=metrics,
            warnings=warnings,
            campaign_id=str(row["id"]),
            account_id=row.get("account_id"),
            is_activation=wanted == "ACTIVE" and before != "ACTIVE",
            note=(
                "Pausing cannot increase spend, so its projected delta is 0. "
                "Activating is bounded by the budget, not by past spend, and is "
                "counted against today's total rather than the per-change limit."
            ),
        )

    def activation_bound(self, ctx, row, currency):
        return campaign_activation_bound(ctx, row, currency)


class AdSetStatus(StatusKind):
    name = "ad_set_status"
    preview_title = "Preview pausing or activating an ad set"
    apply_title = "Apply an ad set status change (AFFECTS DELIVERY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = (("ad set", "status"),)
    entity_type = "ad set"
    module = adsets
    id_arg = "ad_set_id"
    level = "adset"

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> Proposal:
        """Show what pausing or activating an ad set would do. Writes nothing."""
        wanted = wanted_status(new_status)
        row = adsets.find(ctx.client, ad_set_id=ad_set_id)
        currency = ctx.currency_of(row)
        before = row.get("status") or ""
        metrics = ctx.metrics_7d(str(row["id"]), "adset")

        bound_note = ""
        if wanted == "PAUSED":
            projected = Decimal("0")
        else:
            projected, bound_note = ad_set_activation_bound(ctx, row, currency)

        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if bound_note:
            warnings.append(f"projected delta: {bound_note}")
        effective = row.get("effective_status")
        if wanted == "ACTIVE" and effective == "CAMPAIGN_PAUSED":
            warnings.append(
                "the parent CAMPAIGN is paused, so this changes nothing "
                "visible until that is activated too"
            )
        elif wanted == "ACTIVE":
            warnings.append("activating resumes delivery immediately")
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="status",
            before=before,
            after=wanted,
            checks=[],
            projected=projected,
            payload={"entity_id": str(row["id"])},
            status=before,
            effective_status=effective,
            metrics=metrics,
            warnings=warnings,
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            is_activation=wanted == "ACTIVE" and before != "ACTIVE",
            note="Pausing cannot increase spend, so its projected delta is 0.",
        )

    def activation_bound(self, ctx, row, currency):
        return ad_set_activation_bound(ctx, row, currency)


class AdStatus(StatusKind):
    name = "ad_status"
    preview_title = "Preview pausing or activating one ad"
    apply_title = "Apply an ad status change"
    apply_annotations = APPLY_REVERSIBLE
    ledger_keys = (("ad", "status"),)
    entity_type = "ad"
    module = ads
    id_arg = "ad_id"
    level = "ad"

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="From find_ads")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> Proposal:
        """Show what pausing or activating one ad would do. Writes nothing.

        The narrowest change available, and the usual fix for a worn-out
        creative: pausing one ad leaves the ad set's budget and learning intact
        and is completely reversible.
        """
        wanted = wanted_status(new_status)
        row = ads.find(ctx.client, ad_id=ad_id)
        before = row.get("status") or ""
        metrics = ctx.metrics_7d(str(row["id"]), "ad")
        # Pausing one ad cannot raise spend. Activating one moves nothing while
        # siblings already spend the ad set's budget -- but as the ad set's
        # ONLY active ad it restarts that ad set's whole spend.
        bound_note = ""
        if wanted == "PAUSED":
            projected = Decimal("0")
        else:
            projected, bound_note = ad_activation_bound(ctx, row, ctx.currency_of(row))

        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if bound_note:
            warnings.append(f"projected delta: {bound_note}")
        effective = row.get("effective_status")
        if wanted == "ACTIVE" and effective in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"):
            warnings.append(
                f"effective_status is {effective}, so a parent is paused and "
                f"this ad will not deliver until that is activated too"
            )
        elif wanted == "ACTIVE" and effective == "DISAPPROVED":
            warnings.append(
                "this ad was DISAPPROVED in review. Activating it does not "
                "appeal the decision and it will not deliver"
            )
        if wanted == "PAUSED":
            warnings.append(
                "pausing the last active ad in an ad set stops that ad set "
                "delivering entirely -- check find_ads for its siblings first"
            )
        return Proposal(
            entity_type="ad",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"ad set {row.get('adset_id')} > {row.get('name')}",
            field_name="status",
            before=before,
            after=wanted,
            checks=[],
            projected=projected,
            payload={"entity_id": str(row["id"])},
            status=before,
            effective_status=effective,
            metrics=metrics,
            warnings=warnings,
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            is_activation=wanted == "ACTIVE" and before != "ACTIVE" and projected > 0,
            note=(
                "Pausing projects 0. Activating projects 0 while other ACTIVE ads "
                "already spend the ad set's budget, and the ad set's budget when "
                "this would be its only active ad."
            ),
        )

    def activation_bound(self, ctx, row, currency):
        return ad_activation_bound(ctx, row, currency)


# ---------------------------------------------------------------------------
# Daily budgets
# ---------------------------------------------------------------------------


class _MoneyKind(ChangeKind):
    """A money field on one node: before/after rendered "250.00 CAD"."""

    field: str = ""
    module: Any = None
    id_arg: str = ""

    def find(self, ctx, entity_id: str) -> dict[str, Any]:
        return self.module.find(ctx.client, **{self.id_arg: str(entity_id)})

    def current(self, ctx, payload: dict[str, Any]) -> str:
        row = self.find(ctx, payload["entity_id"])
        return rendered(row, self.field, ctx.currency_of(row))

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        row = self.find(ctx, entry["entity_id"])
        return rendered(row, self.field, ctx.currency_of(row))

    def plan(self, ctx, entity_id: str, amount: Decimal):  # pragma: no cover
        raise NotImplementedError

    def build(self, ctx, payload: dict[str, Any]):
        return self.plan(ctx, payload["entity_id"], Decimal(payload["after_amount"]))

    def inverse(self, ctx, entry: dict[str, Any]):
        return self.plan(ctx, entry["entity_id"], stored_amount(entry))

    def revert_row_inverse(self, ctx, entry: dict[str, Any]) -> dict[str, Any] | None:
        # The revert replaces the original's AFTER amount; that is what a
        # revert of the revert puts back.
        try:
            return {"before_amount": str(amount_from_stored(entry["after"]))}
        except ValueError:
            return None


class CampaignDailyBudget(_MoneyKind):
    name = "campaign_daily_budget"
    preview_title = "Preview a campaign daily budget change"
    apply_title = "Apply a campaign daily budget change (SPENDS MONEY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = (("campaign", "daily_budget"),)
    field = "daily_budget"
    module = campaigns
    id_arg = "campaign_id"

    def plan(self, ctx, entity_id, amount):
        return campaigns.plan_set_daily_budget(ctx.client, campaign_id=entity_id, amount=amount)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "250.00"')
        ],
    ) -> Proposal:
        """Show what changing a CBO campaign's daily budget would do. Writes nothing.

        Only works on a campaign whose `budget_level` is `campaign`. On an
        ad-set-budget campaign the budget is one level down and this refuses,
        because setting one here would convert the campaign to campaign-budget
        optimization and redistribute spend across every ad set under it.

        The delta is EXACT, not an estimate: a daily budget is the cap itself
        moving, not a guess about behaviour.
        """
        after = parse_money(new_daily_budget, "new_daily_budget")
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        currency = ctx.currency_of(row)

        if campaigns.budget_level(row) != "campaign":
            raise ValueError(
                f"Campaign {row.get('name')!r} does not hold its own budget -- "
                f"its ad sets do. Use find_ad_sets to get an ad set id, then "
                f"preview_ad_set_daily_budget. Setting a budget here would "
                f"convert the campaign to campaign-budget optimization and "
                f"redistribute spend across every ad set under it."
            )
        if campaigns.budget_field(row) != "daily_budget":
            raise ValueError(
                f"Campaign {row.get('name')!r} is on a LIFETIME budget, not a "
                f"daily one. Use preview_campaign_lifetime_budget; switching "
                f"between the two is not offered."
            )

        before = money(row, "daily_budget", currency)
        metrics = ctx.metrics_7d(str(row["id"]), "campaign")
        ad_set_count = len(
            adsets.search(ctx.client, None, campaign_id=str(row["id"]), limit=200)
        )
        projected = project_budget_change(before, after)
        warnings = [
            f"this is a CAMPAIGN budget shared by {ad_set_count} ad set(s) -- "
            f"the change moves spend for all of them, not one"
        ] + budget_warnings(before, after, "campaign")
        return Proposal(
            entity_type="campaign",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"{row.get('name')} (campaign budget)",
            field_name="daily_budget",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=budget_checks(ctx, currency, before, after),
            projected=projected,
            payload={"entity_id": str(row["id"]), "after_amount": str(after)},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            metrics=metrics,
            warnings=warnings,
            campaign_id=str(row["id"]),
            account_id=row.get("account_id"),
            items=[],
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the campaign never reached the "
                "old cap, raising it may change nothing."
            ),
        ).with_inverse({"before_amount": None if before is None else str(before)})


class AdSetDailyBudget(_MoneyKind):
    name = "ad_set_daily_budget"
    preview_title = "Preview an ad set daily budget change"
    apply_title = "Apply an ad set daily budget change (SPENDS MONEY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = (("ad set", "daily_budget"),)
    field = "daily_budget"
    module = adsets
    id_arg = "ad_set_id"

    def plan(self, ctx, entity_id, amount):
        return adsets.plan_set_daily_budget(ctx.client, ad_set_id=entity_id, amount=amount)

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "50.00"')
        ],
    ) -> Proposal:
        """Show what changing an ad set's daily budget would do. Writes nothing.

        Refuses when the CAMPAIGN holds the budget, because then this ad set has
        none to change.
        """
        after = parse_money(new_daily_budget, "new_daily_budget")
        row = adsets.find(ctx.client, ad_set_id=ad_set_id)
        currency = ctx.currency_of(row)

        using = campaigns.budget_field(row)
        if using is None:
            raise ValueError(
                f"Ad set {row.get('name')!r} has no budget of its own -- its "
                f"CAMPAIGN holds it (Advantage campaign budget). Use "
                f"preview_campaign_daily_budget on campaign "
                f"{row.get('campaign_id')} instead."
            )
        if using != "daily_budget":
            raise ValueError(
                f"Ad set {row.get('name')!r} is on a LIFETIME budget, not a "
                f"daily one. Use preview_ad_set_lifetime_budget; switching "
                f"between the two is not offered."
            )

        before = money(row, "daily_budget", currency)
        metrics = ctx.metrics_7d(str(row["id"]), "adset")
        projected = project_budget_change(before, after)
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="daily_budget",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=budget_checks(ctx, currency, before, after),
            projected=projected,
            payload={"entity_id": str(row["id"]), "after_amount": str(after)},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            metrics=metrics,
            warnings=budget_warnings(before, after, "ad set"),
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the ad set never reached the "
                "old cap, raising it may change nothing."
            ),
        ).with_inverse({"before_amount": None if before is None else str(before)})


class AdSetBidAmount(_MoneyKind):
    name = "ad_set_bid_amount"
    preview_title = "Preview an ad set bid change"
    apply_title = "Apply an ad set bid change (SPENDS MONEY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = (("ad set", "bid_amount"),)
    field = "bid_amount"
    module = adsets
    id_arg = "ad_set_id"

    def plan(self, ctx, entity_id, amount):
        return adsets.plan_set_bid_amount(ctx.client, ad_set_id=entity_id, amount=amount)

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_bid_amount: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "3.50"')
        ],
    ) -> Proposal:
        """Show what changing an ad set's bid cap or cost cap would do.

        Writes nothing. Refuses under a bid strategy that does not use a bid --
        and that refusal is the main reason this tool exists. Under
        `LOWEST_COST_WITHOUT_CAP`, which is Meta's default and what most ad sets
        are on, Meta ACCEPTS a `bid_amount` write and ignores it. Without the
        check, a human would be told their bid moved when nothing happened.
        """
        after = parse_money(new_bid_amount, "new_bid_amount")
        row = adsets.find(ctx.client, ad_set_id=ad_set_id)
        currency = ctx.currency_of(row)
        strategy = adsets.effective_bid_strategy(ctx.client, row)

        usable = adsets.STRATEGIES_USING_BID_AMOUNT
        if strategy not in usable:
            raise ValueError(
                f"Ad set {row.get('name')!r} is on bid strategy {strategy}, "
                f"which does not use bid_amount. Meta would ACCEPT this write "
                f"and ignore it, so it would be reported as applied while "
                f"nothing changed. bid_amount is used under: "
                f"{', '.join(sorted(usable))}."
            )

        before = money(row, "bid_amount", currency)
        metrics = ctx.metrics_7d(str(row["id"]), "adset")
        projected = bid_change_projection(ctx, row, currency, before, after)
        ceiling_name, ceiling = (
            ("max_cost_cap", ctx.limits.max_cost_cap) if strategy == "COST_CAP"
            else ("max_bid_amount", ctx.limits.max_bid_amount)
        )
        checks = [
            check_currency(ctx.limits, currency),
            check_ceiling(ceiling_name, after, ceiling, currency),
            check_pct_increase(
                "max_bid_increase_pct", before, after, ctx.limits.max_bid_increase_pct
            ),
        ]
        meaning = (
            "a CAP: delivery will not bid above it"
            if strategy == "LOWEST_COST_WITH_BID_CAP"
            else "a TARGET: Meta aims for this as an average cost and may exceed "
            "it on an individual result"
        )
        warnings = [f"under {strategy} this amount is {meaning}"]
        if before and after < before:
            warnings.append(
                "lowering a bid cap usually reduces delivery volume as well as "
                "cost -- the ad set may simply stop winning auctions"
            )
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="bid_amount",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=checks,
            projected=projected,
            payload={"entity_id": str(row["id"]), "after_amount": str(after)},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            metrics=metrics,
            warnings=warnings,
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            tightens=before is not None and after < before,
            # A lower cap can only lower spend, whatever the click volume.
            reduces_spend=before is not None and after < before,
            note=(
                "The projected delta assumes the same click volume at the new "
                "bid. It is a bound to reason with, not a forecast -- and a cap "
                "is not a price, so raising one changes nothing unless the "
                "auction was already pressing against it."
            ),
        ).with_inverse({"before_amount": None if before is None else str(before)})
