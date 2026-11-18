"""Lifetime budgets, bid strategies, the ROAS floor, schedules, dayparting and
the frequency cap.

Every one of these changes how much an ad set or campaign spends or when, so
each is a typed pair with money guardrails, and each keeps the previous raw
values in the ledger so revert_change puts back exactly what was there.

Two Meta traps shape the refusals here, the same trap as bid_amount:

* a field Meta ACCEPTS and IGNORES -- a frequency cap on a conversions ad set,
  a bid under a strategy that does not use one -- is refused, because it would
  be recorded as applied while nothing changed;
* dayparting is accepted only with a LIFETIME budget and day_parting pacing;
  on a daily budget the preview refuses and says why, and does not offer to
  switch the budget type (that redistributes the whole flight).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.base import APPLY_SPENDS, Proposal, parse_money
from metaads_mcp.changes.common import (
    NodeFieldKind,
    _remaining_days,
    as_obj,
    canonical,
    daily_bound,
    money,
    previous_values,
    rendered,
)
from metaads_mcp.guardrails import Check, check_ceiling, check_currency, check_pct_increase
from metaads_mcp.previews import pct_change, project_bid_change
from metaads_reporting.currency import format_money, to_minor_units
from metaads_reporting.write import adsets, campaigns

BidStrategy = Literal[
    "LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP", "LOWEST_COST_WITH_MIN_ROAS",
]

#: Meta stores the ROAS floor as an integer: the ratio x 10000.
ROAS_SCALE = Decimal(10000)

#: Optimization goals under which Meta honours frequency_control_specs.
FREQUENCY_CAP_GOALS = frozenset({"REACH", "THRUPLAY", "AD_RECALL_LIFT"})

ADSET_READ = (
    "status", "effective_status", "campaign_id", "account_id", "daily_budget",
    "lifetime_budget", "budget_remaining", "bid_amount", "bid_strategy",
    "bid_constraints", "start_time", "end_time", "optimization_goal",
    "adset_schedule", "pacing_type", "frequency_control_specs",
)
CAMPAIGN_READ = (
    "status", "effective_status", "account_id", "daily_budget", "lifetime_budget",
    "budget_remaining", "bid_strategy", "start_time", "stop_time",
)


def _floor(row: dict[str, Any]) -> Decimal | None:
    raw = (as_obj(row.get("bid_constraints")) or {}).get("roas_average_floor")
    return None if raw in (None, "") else (Decimal(str(raw)) / ROAS_SCALE).quantize(Decimal("0.01"))


def _headroom(ctx, row: dict[str, Any], currency: str, level: str, end_field: str) -> Decimal:
    """Unused daily budget: the honest bound for anything that loosens a cap."""
    bound, _ = daily_bound(row, currency, end_field)
    if bound is None:
        return Decimal("0")
    spent = ctx.spend_7d(ctx.metrics_7d(str(row["id"]), level)) / 7
    return max(bound - spent, Decimal("0"))


def parse_time(raw: str, name: str) -> dt.datetime:
    """An ISO-8601 time WITH a zone, in the future."""
    try:
        value = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601 with a zone, e.g. 2026-12-31T23:59:00-05:00; got {raw!r}.") from exc
    if value.tzinfo is None:
        raise ValueError(f"{name} needs a time zone offset, e.g. -05:00; {raw!r} has none.")
    if value <= dt.datetime.now(dt.timezone.utc):
        raise ValueError(f"{name} {raw} is in the past.")
    return value


def _iso(raw: Any) -> str:
    """A Meta time in one spelling (Meta writes -0500, Python +00:00 ...)."""
    if not raw:
        return "(not set)"
    try:
        value = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        text = str(raw)
        if len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit():
            try:
                value = dt.datetime.fromisoformat(text[:-2] + ":" + text[-2:])
            except ValueError:
                return text
        else:
            return text
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _started(row: dict[str, Any], field: str = "start_time") -> bool:
    raw = row.get(field)
    if not raw:
        return True
    try:
        start = dt.datetime.fromisoformat(str(raw).replace("+0000", "+00:00"))
    except ValueError:
        return True
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    return start <= dt.datetime.now(dt.timezone.utc)


class _AdSetKind(NodeFieldKind):
    graph_type = "adset"
    entity_type = "ad set"
    read_fields = ADSET_READ
    apply_annotations = APPLY_SPENDS

    def _proposal(self, ctx, row, *, before, after, fields, checks, projected,
                  warnings=(), note=None, tightens=False, is_activation=False) -> Proposal:
        # `after` is what a re-read will render once the change lands -- the
        # string revert_change and reconcile_ledger compare against -- so it is
        # rendered from the row with the new fields, never hand-written.
        after = self.render(ctx, {**row, **fields})
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name=self.name.removeprefix("ad_set_"),
            before=before,
            after=after,
            checks=list(checks),
            projected=projected,
            payload={"entity_id": str(row["id"]), "name": row.get("name") or "", "fields": fields},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=list(warnings),
            note=note,
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            tightens=tightens,
            is_activation=is_activation,
        ).with_inverse({"previous": previous_values(row, fields)})


class _CampaignKind(NodeFieldKind):
    graph_type = "campaign"
    entity_type = "campaign"
    read_fields = CAMPAIGN_READ
    apply_annotations = APPLY_SPENDS

    def _proposal(self, ctx, row, *, before, after, fields, checks, projected,
                  warnings=(), note=None, tightens=False, is_activation=False) -> Proposal:
        after = self.render(ctx, {**row, **fields})
        return Proposal(
            entity_type="campaign",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=row.get("name") or "",
            field_name=self.name.removeprefix("campaign_"),
            before=before,
            after=after,
            checks=list(checks),
            projected=projected,
            payload={"entity_id": str(row["id"]), "name": row.get("name") or "", "fields": fields},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=list(warnings),
            note=note,
            campaign_id=str(row["id"]),
            account_id=row.get("account_id"),
            tightens=tightens,
            is_activation=is_activation,
        ).with_inverse({"previous": previous_values(row, fields)})


# ---------------------------------------------------------------------------
# Lifetime budgets
# ---------------------------------------------------------------------------


def _lifetime(kind, ctx, row, new_amount: str, end_field: str, what: str) -> Proposal:
    currency = ctx.currency_of(row)
    after = parse_money(new_amount, "new_lifetime_budget")
    if campaigns.budget_field(row) != "lifetime_budget":
        raise ValueError(
            f"{what.capitalize()} {row.get('name')!r} is not on a LIFETIME budget "
            f"({campaigns.budget_field(row) or 'no budget of its own'}). Use the daily "
            "budget tool; switching budget type is not offered."
        )
    before = money(row, "lifetime_budget", currency)
    days = _remaining_days(row, end_field) or Decimal("1")
    projected = ((after - (before or Decimal("0"))) / days).quantize(Decimal("0.01"))
    return kind._proposal(
        ctx, row,
        before=format_money(before, currency),
        after=format_money(after, currency),
        fields={"lifetime_budget": to_minor_units(after, currency)},
        checks=[
            check_currency(ctx.limits, currency),
            check_pct_increase("max_budget_change_pct", before, after,
                               ctx.limits.max_budget_change_pct),
        ],
        projected=projected,
        warnings=[f"spread over the {days:.1f} day(s) left, this is {projected:+.2f}/day"],
        note=f"{pct_change(before, after) or 'n/a'} change to the whole flight's budget.",
    )


class AdSetLifetimeBudget(_AdSetKind):
    name = "ad_set_lifetime_budget"
    preview_title = "Preview an ad set lifetime budget change"
    apply_title = "Apply an ad set lifetime budget change (SPENDS MONEY)"
    ledger_keys = (("ad set", "lifetime_budget"),)

    def render(self, ctx, row):
        return rendered(row, "lifetime_budget", ctx.currency_of(row))

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_lifetime_budget: Annotated[str, Field(description='Decimal string, e.g. "1500.00"')],
    ) -> Proposal:
        """Change an ad set's LIFETIME budget. Writes nothing.

        The projected delta is the change spread over the days the ad set has
        left (end_time). Refuses on a daily-budget ad set.
        """
        return _lifetime(self, ctx, self.read_row(ctx, ad_set_id), new_lifetime_budget,
                         "end_time", "ad set")


class CampaignLifetimeBudget(_CampaignKind):
    name = "campaign_lifetime_budget"
    preview_title = "Preview a campaign lifetime budget change"
    apply_title = "Apply a campaign lifetime budget change (SPENDS MONEY)"
    ledger_keys = (("campaign", "lifetime_budget"),)

    def render(self, ctx, row):
        return rendered(row, "lifetime_budget", ctx.currency_of(row))

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_lifetime_budget: Annotated[str, Field(description='Decimal string, e.g. "5000.00"')],
    ) -> Proposal:
        """Change a CBO campaign's LIFETIME budget. Writes nothing.

        The projected delta is the change spread over the days left
        (stop_time). Refuses on a daily-budget or ad-set-budget campaign.
        """
        return _lifetime(self, ctx, self.read_row(ctx, campaign_id), new_lifetime_budget,
                         "stop_time", "campaign")


# ---------------------------------------------------------------------------
# Bid strategy and the ROAS floor
# ---------------------------------------------------------------------------


def _strategy_render(ctx, row, strategy_from=None) -> str:
    currency = ctx.currency_of(row)
    strategy = strategy_from or row.get("bid_strategy") or "(not set)"
    parts = [strategy]
    if row.get("bid_amount"):
        parts.append(f"bid {rendered(row, 'bid_amount', currency)}")
    floor = _floor(row)
    if floor is not None:
        parts.append(f"ROAS floor {floor}")
    return " | ".join(parts)


class AdSetBidStrategy(_AdSetKind):
    name = "ad_set_bid_strategy"
    preview_title = "Preview changing an ad set's bid strategy"
    apply_title = "Apply an ad set bid strategy change (SPENDS MONEY)"
    ledger_keys = (("ad set", "bid_strategy"),)

    def render(self, ctx, row):
        return _strategy_render(ctx, row)

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        strategy: Annotated[BidStrategy, Field(description="The new bid strategy")],
        bid_amount: Annotated[
            str | None, Field(description='For BID_CAP / COST_CAP: decimal string, e.g. "4.00"')
        ] = None,
        roas_floor: Annotated[
            str | None, Field(description='For MIN_ROAS: the return to hold, e.g. "2.5" (250%)')
        ] = None,
    ) -> Proposal:
        """Change how an ad set bids. Writes nothing.

        LOWEST_COST_WITHOUT_CAP spends the budget at the lowest cost it can;
        LOWEST_COST_WITH_BID_CAP caps every bid at bid_amount; COST_CAP holds
        the AVERAGE cost per result near bid_amount; LOWEST_COST_WITH_MIN_ROAS
        holds a minimum return (roas_floor). Any strategy change sends the ad
        set back into learning.

        Under campaign budget optimization the strategy is set on the campaign;
        this refuses and points at preview_campaign_bid_strategy.
        """
        row = self.read_row(ctx, ad_set_id)
        currency = ctx.currency_of(row)
        if campaigns.budget_field(row) is None:
            raise ValueError(
                f"Ad set {row.get('name')!r} has no budget of its own: its campaign uses "
                "campaign budget optimization, where the bid strategy is set on the "
                f"CAMPAIGN. Use preview_campaign_bid_strategy on {row.get('campaign_id')}."
            )
        fields: dict[str, Any] = {"bid_strategy": strategy}
        checks: list[Check] = [check_currency(ctx.limits, currency)]
        before_bid = money(row, "bid_amount", currency)
        projected = Decimal("0")
        after_bid = None
        if strategy in ("LOWEST_COST_WITH_BID_CAP", "COST_CAP"):
            if not bid_amount:
                raise ValueError(f"{strategy} needs bid_amount.")
            after_bid = parse_money(bid_amount, "bid_amount")
            fields["bid_amount"] = to_minor_units(after_bid, currency)
            ceiling = ctx.limits.max_bid_amount if strategy == "LOWEST_COST_WITH_BID_CAP" else ctx.limits.max_cost_cap
            checks.append(check_ceiling(
                "max_bid_amount" if strategy == "LOWEST_COST_WITH_BID_CAP" else "max_cost_cap",
                after_bid, ceiling, currency))
            checks.append(check_pct_increase("max_bid_increase_pct", before_bid, after_bid,
                                             ctx.limits.max_bid_increase_pct))
            projected = project_bid_change(
                before_bid, after_bid, ctx.clicks_7d(ctx.metrics_7d(str(row["id"]), "adset")))
        elif strategy == "LOWEST_COST_WITH_MIN_ROAS":
            if not roas_floor:
                raise ValueError("LOWEST_COST_WITH_MIN_ROAS needs roas_floor.")
            floor = parse_money(roas_floor, "roas_floor")
            fields["bid_constraints"] = {"roas_average_floor": int(floor * ROAS_SCALE)}
            checks.append(Check("min_roas_floor", floor >= ctx.limits.min_roas_floor,
                                f"{floor} against minimum {ctx.limits.min_roas_floor}"))
        elif (row.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP") != "LOWEST_COST_WITHOUT_CAP":
            # Removing a cap or a floor lets the ad set spend its whole budget.
            projected = _headroom(ctx, row, currency, "adset", "end_time")
        after = " | ".join(
            [strategy]
            + ([f"bid {format_money(after_bid, currency)}"] if after_bid is not None else [])
            + ([f"ROAS floor {Decimal(roas_floor):.2f}"] if fields.get("bid_constraints") else [])
        )
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after=after, fields=fields,
            checks=checks, projected=projected,
            # A cap, a cost target or a floor restricts what the ad set wins:
            # undoing it lets the ad set spend its unused budget.
            tightens=strategy != "LOWEST_COST_WITHOUT_CAP",
            warnings=["changing the bid strategy sends the ad set back into the learning phase"],
            note="Removing a cap is bounded by the unused daily budget; adding one cannot raise spend.",
        )


class CampaignBidStrategy(_CampaignKind):
    name = "campaign_bid_strategy"
    preview_title = "Preview changing a CBO campaign's bid strategy"
    apply_title = "Apply a campaign bid strategy change (SPENDS MONEY)"
    ledger_keys = (("campaign", "bid_strategy"),)

    def render(self, ctx, row):
        return row.get("bid_strategy") or "(not set)"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        strategy: Annotated[BidStrategy, Field(description="The new bid strategy")],
    ) -> Proposal:
        """Change the bid strategy of a campaign that holds its own budget (CBO).

        Writes nothing. A cap or cost-cap strategy also needs a bid on each ad
        set; Meta's validate_only in this preview says whether they have one.
        Every ad set goes back into learning. Refuses on an ad-set-budget
        campaign: there the strategy is per ad set.
        """
        row = self.read_row(ctx, campaign_id)
        currency = ctx.currency_of(row)
        if campaigns.budget_level(row) != "campaign":
            raise ValueError(
                f"Campaign {row.get('name')!r} does not hold its budget; its ad sets "
                "carry their own bid strategies. Use preview_ad_set_bid_strategy."
            )
        projected = Decimal("0")
        if strategy == "LOWEST_COST_WITHOUT_CAP" and (row.get("bid_strategy") or strategy) != strategy:
            projected = _headroom(ctx, row, currency, "campaign", "stop_time")
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after=strategy,
            fields={"bid_strategy": strategy}, checks=[check_currency(ctx.limits, currency)],
            projected=projected,
            tightens=strategy != "LOWEST_COST_WITHOUT_CAP",
            warnings=["every ad set in the campaign goes back into the learning phase"],
        )


class AdSetRoasFloor(_AdSetKind):
    name = "ad_set_roas_floor"
    preview_title = "Preview changing an ad set's minimum ROAS"
    apply_title = "Apply a minimum ROAS change (SPENDS MONEY)"
    ledger_keys = (("ad set", "roas_floor"),)

    def render(self, ctx, row):
        floor = _floor(row)
        return "(not set)" if floor is None else f"ROAS floor {floor}"

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_roas_floor: Annotated[str, Field(description='The return to hold, e.g. "2.5" (250%)')],
    ) -> Proposal:
        """Change the minimum return a LOWEST_COST_WITH_MIN_ROAS ad set holds.

        Writes nothing. Lowering the floor lets the ad set win more auctions and
        spend more of its budget; raising it narrows delivery. Refuses under any
        other strategy, where Meta would accept the floor and ignore it.
        """
        row = self.read_row(ctx, ad_set_id)
        strategy = adsets.effective_bid_strategy(ctx.client, row)
        if strategy != adsets.STRATEGY_MIN_ROAS:
            raise ValueError(
                f"Ad set {row.get('name')!r} is on {strategy}; a ROAS floor is only used "
                f"under {adsets.STRATEGY_MIN_ROAS}. Meta would accept it and ignore it."
            )
        after = parse_money(new_roas_floor, "new_roas_floor").quantize(Decimal("0.01"))
        before = _floor(row)
        projected = Decimal("0")
        if before is not None and after < before:
            projected = _headroom(ctx, row, ctx.currency_of(row), "adset", "end_time")
        change = Decimal("0") if not before else abs((after - before) / before * 100)
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after=f"ROAS floor {after}",
            fields={"bid_constraints": {"roas_average_floor": int(after * ROAS_SCALE)}},
            checks=[
                Check("min_roas_floor", after >= ctx.limits.min_roas_floor,
                      f"{after} against minimum {ctx.limits.min_roas_floor}"),
                Check("max_bid_increase_pct", change <= Decimal(str(ctx.limits.max_bid_increase_pct)),
                      f"{change:.1f}% move against +/-{ctx.limits.max_bid_increase_pct:.0f}%"),
            ],
            projected=projected,
            tightens=before is not None and after > before,
            warnings=["a ROAS floor change re-enters learning on most ad sets"],
            note="Lowering the floor is bounded by the unused daily budget.",
        )


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _schedule(kind, ctx, row, end_value: str | None, start_value: str | None,
              end_field: str) -> Proposal:
    fields: dict[str, Any] = {}
    warnings = []
    if end_value:
        end = parse_time(end_value, end_field)
        fields[end_field] = end.isoformat()
    if start_value:
        if _started(row):
            raise ValueError(
                f"{row.get('name')!r} has already started, so its start_time cannot move. "
                f"Change {end_field} only."
            )
        fields["start_time"] = parse_time(start_value, "start_time").isoformat()
    if not fields:
        raise ValueError(f"Give {end_field} and/or start_time.")
    currency = ctx.currency_of(row)
    projected = Decimal("0")
    if row.get("lifetime_budget"):
        # The same lifetime pot over a different number of days: shortening a
        # flight raises the daily pace.
        pot = money(row, "budget_remaining", currency) or money(row, "lifetime_budget", currency)
        old_days = _remaining_days(row, end_field) or Decimal("1")
        new_days = _remaining_days({end_field: fields.get(end_field, row.get(end_field))},
                                   end_field) or Decimal("1")
        projected = (pot / new_days - pot / old_days).quantize(Decimal("0.01"))
        warnings.append(f"the lifetime budget is re-paced over {new_days:.1f} day(s)")
    extends = False
    if not row.get("lifetime_budget") and _adds_days(row, fields, end_field):
        # A DAILY budget spends that budget on every day the flight runs: the
        # added days are spend that would not have happened. Counted like an
        # activation -- the unused daily headroom, exempt from the per-change
        # limit and counted in the daily and session totals.
        from metaads_mcp.changes.common import unused_headroom

        level = "ad set" if end_field == "end_time" else "campaign"
        projected = unused_headroom(ctx, {"entity_type": level, "entity_id": row["id"]})
        extends = projected > 0
        warnings.append(
            f"this adds days to a daily-budget flight: up to {projected:.2f}/day more "
            "(the unused daily budget) on each added day"
        )
    after = " | ".join(f"{k} {v}" for k, v in sorted(fields.items()))
    return kind._proposal(
        ctx, row, before=kind.render(ctx, row), after=after, fields=fields,
        checks=[], projected=projected, warnings=warnings, is_activation=extends,
        note=("On a daily budget, added days spend the daily budget again; on a lifetime "
              "budget the schedule re-paces it."),
    )


def _adds_days(row: dict[str, Any], fields: dict[str, Any], end_field: str) -> bool:
    """Whether the new schedule runs on days the old one did not."""
    def when(raw):
        if not raw:
            return None
        value = dt.datetime.fromisoformat(_iso(raw))
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)

    try:
        old_end, new_end = when(row.get(end_field)), when(fields.get(end_field))
        old_start, new_start = when(row.get("start_time")), when(fields.get("start_time"))
    except ValueError:
        return True  # unreadable: assume it does, and count it
    if new_end is not None and (old_end is None or new_end > old_end):
        # No old end means it already runs forever: a new end SHORTENS it.
        return old_end is not None
    return new_start is not None and old_start is not None and new_start < old_start


class AdSetSchedule(_AdSetKind):
    name = "ad_set_schedule"
    preview_title = "Preview changing an ad set's schedule"
    apply_title = "Apply an ad set schedule change (AFFECTS DELIVERY)"
    ledger_keys = (("ad set", "schedule"),)

    def render(self, ctx, row):
        return f"start {_iso(row.get('start_time'))} | end {_iso(row.get('end_time'))}"

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        end_time: Annotated[str | None, Field(description="ISO-8601 with zone, e.g. 2026-12-31T23:59:00-05:00")] = None,
        start_time: Annotated[str | None, Field(description="Only before the ad set has started")] = None,
    ) -> Proposal:
        """Change when an ad set ends (and, before launch, when it starts).

        Writes nothing. On a lifetime budget the remaining budget is re-paced
        over the new number of days, and the projected delta says by how much.
        """
        return _schedule(self, ctx, self.read_row(ctx, ad_set_id), end_time, start_time, "end_time")


class CampaignSchedule(_CampaignKind):
    name = "campaign_schedule"
    preview_title = "Preview changing a campaign's schedule"
    apply_title = "Apply a campaign schedule change (AFFECTS DELIVERY)"
    ledger_keys = (("campaign", "schedule"),)

    def render(self, ctx, row):
        return f"start {_iso(row.get('start_time'))} | stop {_iso(row.get('stop_time'))}"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        stop_time: Annotated[str | None, Field(description="ISO-8601 with zone")] = None,
        start_time: Annotated[str | None, Field(description="Only before the campaign has started")] = None,
    ) -> Proposal:
        """Change when a campaign stops (and, before launch, when it starts).

        Writes nothing. A lifetime campaign budget is re-paced over the new
        number of days.
        """
        return _schedule(self, ctx, self.read_row(ctx, campaign_id), stop_time, start_time, "stop_time")


# ---------------------------------------------------------------------------
# Optional: dayparting and the frequency cap
# ---------------------------------------------------------------------------


class AdSetDayparting(_AdSetKind):
    name = "ad_set_dayparting"
    preview_title = "Preview an ad set's dayparting (hours and days it runs)"
    apply_title = "Apply ad set dayparting (AFFECTS DELIVERY)"
    ledger_keys = (("ad set", "dayparting"),)

    def render(self, ctx, row):
        return canonical(row.get("adset_schedule"))

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        schedule: Annotated[
            list[dict[str, Any]],
            Field(description='Blocks of {"days": [0-6, 0=Sunday], "start_minute": 0-1440, "end_minute": 0-1440}, whole hours'),
        ],
    ) -> Proposal:
        """Run an ad set only on certain days and hours (in the account's zone).

        Writes nothing. Meta accepts dayparting ONLY on a lifetime budget with
        day_parting pacing; on a daily budget this refuses and does not offer
        the budget switch (that re-plans the whole flight).
        """
        row = self.read_row(ctx, ad_set_id)
        if not row.get("lifetime_budget"):
            raise ValueError(
                f"Ad set {row.get('name')!r} is on a daily budget. Meta only accepts an "
                "ad schedule with a LIFETIME budget and day_parting pacing; switching "
                "budget type is not offered here."
            )
        blocks = []
        for block in schedule or []:
            days = sorted({int(d) for d in block.get("days") or []})
            start, end = int(block.get("start_minute", -1)), int(block.get("end_minute", -1))
            if not days or any(d < 0 or d > 6 for d in days):
                raise ValueError(f"days must be 0-6 (0 = Sunday), got {block.get('days')}.")
            if not (0 <= start < end <= 1440) or start % 60 or end % 60:
                raise ValueError(f"start/end must be whole hours within the day, got {start}-{end}.")
            blocks.append({"days": days, "start_minute": start, "end_minute": end})
        if not blocks:
            raise ValueError("Give at least one schedule block.")
        fields = {"adset_schedule": blocks, "pacing_type": ["day_parting"]}
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after=canonical(blocks), fields=fields,
            checks=[], projected=Decimal("0"), tightens=True,
            warnings=["the lifetime budget is spent only inside these hours, so their pace rises"],
            note="Restricting hours cannot raise the lifetime total; projected 0.",
        )


class AdSetFrequencyCap(_AdSetKind):
    name = "ad_set_frequency_cap"
    preview_title = "Preview an ad set's frequency cap"
    apply_title = "Apply an ad set frequency cap"
    ledger_keys = (("ad set", "frequency_cap"),)

    def render(self, ctx, row):
        return canonical(row.get("frequency_control_specs"))

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        max_frequency: Annotated[int, Field(description="Impressions per person", ge=1, le=30)],
        interval_days: Annotated[int, Field(description="Over this many days", ge=1, le=90)],
    ) -> Proposal:
        """Cap how often one person sees the ad set's ads.

        Writes nothing. Meta honours a frequency cap only under REACH,
        THRUPLAY and AD_RECALL_LIFT optimization; under any other goal it
        accepts the field and ignores it, so this refuses there.
        """
        row = self.read_row(ctx, ad_set_id)
        goal = row.get("optimization_goal")
        if goal not in FREQUENCY_CAP_GOALS:
            raise ValueError(
                f"Ad set {row.get('name')!r} optimizes for {goal}. Meta honours a frequency "
                f"cap only under {', '.join(sorted(FREQUENCY_CAP_GOALS))}; here it would be "
                "accepted and ignored."
            )
        specs = [{"event": "IMPRESSIONS", "interval_days": interval_days,
                  "max_frequency": max_frequency}]
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after=canonical(specs),
            fields={"frequency_control_specs": specs}, checks=[], projected=Decimal("0"),
            tightens=True,
            note="A cap can only reduce delivery; projected 0. Undoing it is not free.",
        )


BIDDING_KINDS = (
    AdSetLifetimeBudget, CampaignLifetimeBudget, AdSetBidStrategy, CampaignBidStrategy,
    AdSetRoasFloor, AdSetSchedule, CampaignSchedule, AdSetDayparting, AdSetFrequencyCap,
)


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

OptimizationGoal = Literal[
    "OFFSITE_CONVERSIONS", "LINK_CLICKS", "LANDING_PAGE_VIEWS", "IMPRESSIONS", "REACH",
    "THRUPLAY", "LEAD_GENERATION", "QUALITY_LEAD", "VALUE", "POST_ENGAGEMENT",
    "APP_INSTALLS", "AD_RECALL_LIFT", "CONVERSATIONS",
]
BillingEvent = Literal["IMPRESSIONS", "LINK_CLICKS", "THRUPLAY"]


class AdSetOptimizationGoal(_AdSetKind):
    name = "ad_set_optimization_goal"
    preview_title = "Preview changing what an ad set optimizes for"
    apply_title = "Apply an optimization goal change (RESETS LEARNING)"
    ledger_keys = (("ad set", "optimization_goal"),)
    read_fields = ADSET_READ + ("promoted_object",)

    def render(self, ctx, row):
        return f"{row.get('optimization_goal') or '(not set)'} | billed on {row.get('billing_event') or '(not set)'}"

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        optimization_goal: Annotated[OptimizationGoal, Field(description="What Meta optimizes delivery for")],
        billing_event: Annotated[
            BillingEvent | None, Field(description="Only when the goal needs a different one (e.g. THRUPLAY)")
        ] = None,
    ) -> Proposal:
        """Change what an ad set optimizes for (and, where Meta requires it, its
        billing event). Writes nothing.

        The ad set re-enters the learning phase from scratch. The preview shows
        the promoted object (pixel and event, page, app) the new goal will use;
        Meta's validate_only says whether that goal is valid for it and for the
        campaign's objective.
        """
        row = self.read_row(ctx, ad_set_id)
        fields: dict[str, Any] = {"optimization_goal": optimization_goal}
        if billing_event:
            fields["billing_event"] = billing_event
        if row.get("optimization_goal") == optimization_goal and not billing_event:
            raise ValueError(f"The ad set already optimizes for {optimization_goal}.")
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after="", fields=fields, checks=[],
            projected=Decimal("0"),
            warnings=[
                "the learning phase restarts from scratch: expect unstable cost per result for days",
                f"promoted object: {canonical(row.get('promoted_object'))}",
            ],
            note="The budget does not move; projected 0.",
        )


class AdSetAttributionSpec(_AdSetKind):
    name = "ad_set_attribution_spec"
    preview_title = "Preview changing an ad set's attribution window"
    apply_title = "Apply an attribution window change"
    ledger_keys = (("ad set", "attribution_spec"),)
    read_fields = ADSET_READ + ("attribution_spec",)

    def render(self, ctx, row):
        return canonical(row.get("attribution_spec"))

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        click_days: Annotated[Literal[1, 7], Field(description="Click-through window, days")] = 7,
        view_days: Annotated[Literal[0, 1], Field(description="View-through window, days (0 = none)")] = 1,
    ) -> Proposal:
        """Change the attribution window an ad set optimizes and reports on.

        Writes nothing. A shorter window credits fewer conversions, so reported
        results drop without anything changing in the real world -- say so to
        the human. Delivery re-learns.
        """
        row = self.read_row(ctx, ad_set_id)
        spec = [{"event_type": "CLICK_THROUGH", "window_days": int(click_days)}]
        if view_days:
            spec.append({"event_type": "VIEW_THROUGH", "window_days": int(view_days)})
        return self._proposal(
            ctx, row, before=self.render(ctx, row), after="", fields={"attribution_spec": spec},
            checks=[], projected=Decimal("0"),
            warnings=["reported conversions change with the window, not with performance"],
        )


BIDDING_KINDS = BIDDING_KINDS + (AdSetOptimizationGoal, AdSetAttributionSpec)
