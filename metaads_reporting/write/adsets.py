"""Ad set reads and writes.

The ad set is where most of a Meta account's real controls live: the budget
(unless the campaign holds it -- see :mod:`metaads_reporting.write.campaigns`),
the bid, the optimization goal, the schedule and the whole targeting spec.

``bid_amount`` carries its own trap, and it is the one most likely to produce a
change that reports success and does nothing. **The bid is only honoured under
a bid strategy that uses one.** Under ``LOWEST_COST_WITHOUT_CAP`` -- Meta's
default, and what most ad sets are on -- there is no bid to set, and a
``bid_amount`` write is accepted and ignored. So :func:`plan_set_bid_amount`
reads ``bid_strategy`` first and refuses rather than letting a write land in
the ledger as applied when nothing moved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..currency import format_money, from_minor_units, to_minor_units
from .campaigns import budget_field
from .client import MutatingMetaAdsClient, MutationError, check_status
from ..graph import GraphAPIError
from .lookup import Candidate, NotFound, edge_search, is_not_found, match_by_name
from .plan import FieldChange, PlannedChange

ENTITY = "ad set"
EDGE = "adsets"

#: What a search lists by default: everything that can still be changed.
#: ARCHIVED and DELETED are left out -- they cannot be changed, and including
#: them makes every name lookup ambiguous against things that no longer exist.
DEFAULT_STATUSES = ["ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "IN_PROCESS", "WITH_ISSUES"]

FIELDS = (
    "id",
    "name",
    # The account the object belongs to: the write scope check compares THIS,
    # not the server's default with itself.
    "account_id",
    "status",
    "effective_status",
    "campaign_id",
    "daily_budget",
    "lifetime_budget",
    "budget_remaining",
    "bid_amount",
    "bid_strategy",
    "billing_event",
    "optimization_goal",
    "start_time",
    "end_time",
    "destination_type",
)

#: Bid strategies under which ``bid_amount`` is actually used. Everything else
#: ignores it, silently.
#:
#:   LOWEST_COST_WITH_BID_CAP  bid_amount is a hard cap on each auction bid
#:   COST_CAP                  bid_amount is the average cost per result to hold
#:
#: NOT here, and the old table had both: LOWEST_COST_WITH_MIN_ROAS takes its
#: target in ``bid_constraints.roas_average_floor`` (a ratio, not money), and
#: TARGET_COST is deprecated. A bid_amount write under either is accepted and
#: ignored, which is exactly the trap this table exists to refuse.
STRATEGIES_USING_BID = frozenset({"LOWEST_COST_WITH_BID_CAP", "COST_CAP"})
STRATEGIES_USING_BID_AMOUNT = STRATEGIES_USING_BID

#: Uses bid_constraints.roas_average_floor; see the ad_set_roas_floor kind.
STRATEGY_MIN_ROAS = "LOWEST_COST_WITH_MIN_ROAS"


def effective_bid_strategy(client: MutatingMetaAdsClient, row: dict[str, Any]) -> str:
    """The bid strategy this ad set actually runs under.

    Under campaign budget optimization the strategy is set on the CAMPAIGN and
    an ad set may report none, so an empty answer is read from the parent
    rather than taken to mean "no strategy".
    """
    own = row.get("bid_strategy")
    if own:
        return own
    campaign_id = row.get("campaign_id")
    if campaign_id:
        try:
            parent = client.node(str(campaign_id), ("id", "bid_strategy"))
        except Exception:  # noqa: BLE001 - unreadable parent: say "not set"
            parent = {}
        if parent.get("bid_strategy"):
            return parent["bid_strategy"]
    return "(not set)"


def find(
    client: MutatingMetaAdsClient,
    *,
    ad_set_id: str | None = None,
    name: str | None = None,
    campaign_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one ad set by id or name. Raises if not exactly one."""
    if (ad_set_id is None) == (name is None):
        raise MutationError("Give exactly one of ad_set_id or name.")

    if ad_set_id is not None:
        try:
            return client.node(str(ad_set_id), FIELDS)
        except GraphAPIError as exc:
            if not is_not_found(exc):
                # A rate limit or an expired token is NOT "no such object":
                # reporting it as one sends the agent hunting for a typo.
                raise
            raise NotFound(
                f"No ad set {ad_set_id} this token can see. On Meta a missing "
                f"object and a missing permission are the same error."
            ) from exc

    rows = search(client, None, campaign_id=campaign_id, account_id=account_id)
    return match_by_name(rows, name or "", entity=ENTITY, to_candidate=as_candidate)


def as_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        id=str(row.get("id")),
        name=row.get("name") or "(unnamed)",
        status=row.get("status") or "",
        context=f"campaign {row.get('campaign_id')}",
    )


def search(
    client: MutatingMetaAdsClient,
    needle: str | None = None,
    *,
    campaign_id: str | None = None,
    account_id: str | None = None,
    limit: int = 200,
    max_pages: int = 10,
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Ad sets in the account or in one campaign, optionally narrowed by name.

    Scoping to a campaign matters more here than it looks: ad set names are
    unique only WITHIN a campaign, and "Broad" or "Lookalike 1%" appears in
    every campaign an account has.
    """
    from ..account_id import account_path

    parent = (
        str(campaign_id)
        if campaign_id
        else account_path(client.resolve_account_id(account_id))
    )
    rows, _ = edge_search(
        client, parent, EDGE, FIELDS,
        needle=needle, limit=limit, max_pages=max_pages,
        statuses=list(status_filter or DEFAULT_STATUSES),
    )
    return rows


def plan_set_status(
    client: MutatingMetaAdsClient,
    *,
    status: str,
    ad_set_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """Pause or activate an ad set."""
    wanted = check_status(status)
    row = find(client, ad_set_id=ad_set_id, name=name, account_id=account_id)
    before = row.get("status") or ""

    if before == wanted:
        raise MutationError(
            f"Ad set {row.get('name')!r} is already {wanted}; nothing to do."
        )

    warnings = []
    effective = row.get("effective_status")
    if wanted == "ACTIVE" and effective == "CAMPAIGN_PAUSED":
        warnings.append(
            "the parent CAMPAIGN is paused, so this ad set will not deliver "
            "until that is activated too -- this change alone does nothing "
            "visible"
        )
    elif wanted == "ACTIVE":
        warnings.append("activating resumes delivery immediately")

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="adset",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"status": wanted},
        changes=[FieldChange("status", before, wanted)],
        warnings=warnings,
    )


def plan_set_daily_budget(
    client: MutatingMetaAdsClient,
    *,
    amount: Decimal | str | float,
    ad_set_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
    override_guardrail: bool = False,
) -> PlannedChange:
    """Change an ad set's daily budget.

    Refuses when the CAMPAIGN holds the budget, because then this ad set has
    none to change and the write would either be rejected or convert the
    campaign off campaign-budget optimization.
    """
    row = find(client, ad_set_id=ad_set_id, name=name, account_id=account_id)
    # The AD SET's account, not the default one: a token reaches several.
    currency = client.currency(row.get("account_id") or account_id)
    client.check_daily_budget(float(amount), override=override_guardrail)

    using = budget_field(row)
    if using is None:
        raise MutationError(
            f"Ad set {row.get('name')!r} has no budget of its own -- its "
            f"CAMPAIGN holds it (Advantage campaign budget). Change the "
            f"campaign's daily_budget instead; setting one here would take the "
            f"campaign off campaign-budget optimization."
        )
    if using != "daily_budget":
        raise MutationError(
            f"Ad set {row.get('name')!r} is on a LIFETIME budget, not a daily "
            f"one. Switching between them also requires changing the schedule, "
            f"which this tool does not do."
        )

    before = from_minor_units(row.get("daily_budget"), currency)
    after = Decimal(str(amount))
    if before == after:
        raise MutationError(
            f"Ad set {row.get('name')!r} is already at "
            f"{format_money(after, currency)}; nothing to do."
        )

    warnings = []
    if before and after < before:
        warnings.append(
            "lowering a daily budget can stop delivery part-way through today "
            "if the ad set has already spent more than the new cap"
        )
    if before and after > before * 2:
        warnings.append(
            "more than doubling a budget re-enters the learning phase on most "
            "optimization goals, and performance is unstable until it exits"
        )

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="adset",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"daily_budget": to_minor_units(after, currency)},
        changes=[
            FieldChange(
                "daily_budget",
                format_money(before, currency),
                format_money(after, currency),
            )
        ],
        warnings=warnings,
    )


def plan_set_bid_amount(
    client: MutatingMetaAdsClient,
    *,
    amount: Decimal | str | float,
    ad_set_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """Change an ad set's bid cap or cost target.

    Refuses under a bid strategy that does not use one. That refusal is the
    point of this function: Meta ACCEPTS the write under
    ``LOWEST_COST_WITHOUT_CAP`` and ignores it, so without the check the ledger
    would record a change that never happened and a human would be told their
    bid moved.
    """
    row = find(client, ad_set_id=ad_set_id, name=name, account_id=account_id)
    # The AD SET's account, not the default one: a token reaches several.
    currency = client.currency(row.get("account_id") or account_id)
    strategy = effective_bid_strategy(client, row)

    if strategy not in STRATEGIES_USING_BID_AMOUNT:
        raise MutationError(
            f"Ad set {row.get('name')!r} is on bid strategy {strategy}, which "
            f"does not use bid_amount. Meta ACCEPTS the write and ignores it, "
            f"so this would be recorded as applied while nothing changed. "
            f"bid_amount is used under: "
            f"{', '.join(sorted(STRATEGIES_USING_BID_AMOUNT))}."
        )

    before = from_minor_units(row.get("bid_amount"), currency)
    after = Decimal(str(amount))
    if before == after:
        raise MutationError(
            f"Ad set {row.get('name')!r} is already at "
            f"{format_money(after, currency)}; nothing to do."
        )

    meaning = (
        "a CAP: delivery will not bid above it"
        if strategy == "LOWEST_COST_WITH_BID_CAP"
        else "a TARGET: Meta aims for this average cost, and may exceed it on "
        "an individual result"
    )
    warnings = [f"under {strategy} this amount is {meaning}"]
    if before and after < before:
        warnings.append(
            "lowering a bid cap usually reduces delivery volume as well as "
            "cost -- the ad set may simply stop winning auctions"
        )

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="adset",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"bid_amount": to_minor_units(after, currency)},
        changes=[
            FieldChange(
                "bid_amount",
                format_money(before, currency),
                format_money(after, currency),
            )
        ],
        warnings=warnings,
    )
