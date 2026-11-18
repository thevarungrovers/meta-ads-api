"""Creating campaigns and ad sets, and deep-copying them. Always PAUSED.

A campaign carries ``special_ad_categories``, a legal declaration about
housing, employment, credit and social-issue advertising. Here it is a
REQUIRED argument with no default -- the agent must state it, ``[]`` included
-- and it is rendered verbatim in the preview the human approves.

Nothing created here spends: everything is PAUSED (copies with
``status_option=PAUSED``), so a budget counts toward the daily projected cap
only when something is activated, through the status kinds. A creation costs
3 applies. Undoing one PAUSES what was created; nothing is ever deleted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.ads_create import CREATE_WEIGHT
from metaads_mcp.changes.base import APPLY_SPENDS, NOTHING, ChangeKind, Proposal, parse_money
from metaads_mcp.changes.bidding import BidStrategy, BillingEvent, OptimizationGoal, parse_time
from metaads_mcp.changes.targeting import validate_spec
from metaads_mcp.guardrails import Check, check_ceiling, check_currency
from metaads_reporting.currency import format_money, to_minor_units
from metaads_reporting.write import ads, adsets, campaigns
from metaads_reporting.write.plan import FieldChange, PlannedChange

Objective = Literal[
    "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION", "OUTCOME_SALES",
]
SpecialAdCategory = Literal[
    "NONE", "HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS",
]


#: How many same-named siblings name_is_free reads before it gives up.
NAME_SCAN_CAP = 50


def name_is_free(ctx, module, name: str, **scope) -> Check:
    """WARN when a sibling already has this exact name. Meta does not require
    names to be unique, so this never blocks: it catches an accidental twin.
    A scan that could not finish (an error, or the cap reached) WARNs that the
    name could not be verified free -- it never passes silently."""
    try:
        rows = module.search(ctx.client, name, limit=NAME_SCAN_CAP, **scope)
    except Exception as exc:  # noqa: BLE001 - advisory
        return Check("name_is_free", True,
                     f"could not verify the name is free ({str(exc).splitlines()[0]})", warn=True)
    twins = [r for r in rows if (r.get("name") or "") == name]
    if twins:
        return Check("name_is_free", True,
                     f"{len(twins)} existing with this exact name ({', '.join(str(t.get('id')) for t in twins[:5])}); "
                     "Meta allows it, reports will not tell them apart", warn=True)
    if len(rows) >= NAME_SCAN_CAP:
        return Check("name_is_free", True,
                     f"could not verify the name is free: the scan stopped at {NAME_SCAN_CAP} "
                     "similar names", warn=True)
    return Check("name_is_free", True, "no sibling has this name")


class _Creates(ChangeKind):
    """Shared: weight 3, nothing before, revert pauses what was made."""

    apply_annotations = APPLY_SPENDS
    weight = CREATE_WEIGHT
    module: Any = None
    id_arg: str = ""

    def current(self, ctx, payload):
        return NOTHING

    def inverse_for(self, payload, result, created):
        children = [c.get("copied_id") for c in result.response.get("ad_object_ids") or []
                    if isinstance(c, dict)]
        return {"created_id": result.node_id, **({"children": children} if children else {})}

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    def revert_reduces_spend(self, ctx, entry):
        # The undo pauses (or ends) what was created: it can only lower spend.
        return True

    def inverse(self, ctx, entry):
        created = (entry.get("inverse") or {}).get("created_id") or entry.get("entity_id")
        if not created or created == "(new)":
            raise ValueError(f"Entry {entry['entry_id']} recorded no created id; pausing nothing.")
        return self.module.plan_set_status(ctx.client, status="PAUSED", **{self.id_arg: str(created)})


class CreateCampaign(_Creates):
    name = "create_campaign"
    preview_title = "Preview creating a PAUSED campaign"
    apply_title = "Create the PAUSED campaign the preview described"
    module = campaigns
    id_arg = "campaign_id"
    creates_named = ("campaign",)

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="Campaign BASE name; the owner/agent prefix is added (see the preview's naming block)")],
        objective: Annotated[Objective, Field(description="The ODAX objective")],
        special_ad_categories: Annotated[
            list[SpecialAdCategory],
            Field(description="REQUIRED legal declaration: [] or ['NONE'] if none applies; "
                              "HOUSING, EMPLOYMENT, FINANCIAL_PRODUCTS_SERVICES, ISSUES_ELECTIONS_POLITICS"),
        ],
        budget_level: Annotated[
            Literal["campaign", "adset"],
            Field(description="campaign: one budget here (CBO); adset: each ad set has its own"),
        ],
        daily_budget: Annotated[str | None, Field(description='CBO only: decimal string, e.g. "50.00"')] = None,
        bid_strategy: Annotated[BidStrategy | None, Field(description="CBO only; default lowest cost")] = None,
    ) -> Proposal:
        """Show a new campaign, created PAUSED. Writes nothing.

        `special_ad_categories` is a legal statement and has no default: say
        [] (or ["NONE"]) only if the ads are not about housing, employment,
        credit/financial products, or social issues, elections or politics. It
        is shown verbatim for the human to approve, and it restricts targeting
        for the campaign's whole life.

        Nothing spends until a human activates it. Next: preview_create_ad_set.
        """
        decision = ctx.name_for_create("campaign", name)
        name = decision.final
        cats = [c for c in special_ad_categories if c != "NONE"]
        # Created in the write client's account: that account's currency.
        currency = ctx.currency(ctx.target_account())
        payload_out: dict[str, Any] = {
            "name": name, "objective": objective, "status": "PAUSED",
            "buying_type": "AUCTION", "special_ad_categories": cats,
        }
        checks: list[Check] = [check_currency(ctx.limits, currency)]
        if budget_level == "campaign":
            if not daily_budget:
                raise ValueError("A campaign-budget (CBO) campaign needs daily_budget.")
            amount = parse_money(daily_budget, "daily_budget")
            checks.append(check_ceiling("max_daily_budget", amount, ctx.limits.max_daily_budget, currency))
            payload_out["daily_budget"] = to_minor_units(amount, currency)
            payload_out["bid_strategy"] = bid_strategy or "LOWEST_COST_WITHOUT_CAP"
            budget_text = f"campaign budget {format_money(amount, currency)}/day"
        else:
            if daily_budget or bid_strategy:
                raise ValueError("An ad-set-budget campaign takes no budget or bid strategy; "
                                 "set them on each ad set.")
            # Required to be explicit on recent API versions for ABO campaigns.
            # Graph form fields want "true"/"false", not Python's "False".
            payload_out["is_adset_budget_sharing_enabled"] = "false"
            budget_text = "budgets on the ad sets"
        if not decision.refused:
            checks.append(name_is_free(ctx, campaigns, name))
        proposal = Proposal(
            entity_type="campaign", entity_id="(new)", entity_name=name,
            path=name, field_name="created", before=NOTHING,
            after=(f"PAUSED {objective} campaign | {budget_text} | "
                   f"special_ad_categories: {list(special_ad_categories)!r}"),
            checks=checks, projected=Decimal("0"), payload={"create": payload_out},
            warnings=[
                f"special_ad_categories is declared as {list(special_ad_categories)!r}: a legal "
                "statement the human is approving, and it cannot be changed later without "
                "re-review",
                "created PAUSED: nothing spends until a human activates it",
            ],
            note="A PAUSED campaign spends nothing; its budget counts against the daily cap only when activated.",
        )
        proposal.name(decision)
        return proposal

    def build(self, ctx, payload):
        create = payload["create"]
        return PlannedChange(action="create", entity="campaign", entity_type="campaign",
                             node_id="", label=create["name"], payload=dict(create),
                             changes=[FieldChange("campaign", None, create["name"])],
                             edge="campaigns")


class CreateAdSet(_Creates):
    name = "create_ad_set"
    preview_title = "Preview creating a PAUSED ad set"
    apply_title = "Create the PAUSED ad set the preview described"
    module = adsets
    id_arg = "ad_set_id"
    creates_named = ("ad set",)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="The campaign it goes in")],
        name: Annotated[str, Field(description="Ad set BASE name; the owner/agent prefix is added")],
        optimization_goal: Annotated[OptimizationGoal, Field(description="What to optimize for")],
        targeting: Annotated[dict[str, Any], Field(description="A full targeting spec (geo_locations required); ids from search_targeting")],
        billing_event: Annotated[BillingEvent, Field(description="Usually IMPRESSIONS")] = "IMPRESSIONS",
        daily_budget: Annotated[str | None, Field(description="Ad-set-budget campaigns only")] = None,
        bid_strategy: Annotated[BidStrategy | None, Field(description="Ad-set-budget campaigns only")] = None,
        bid_amount: Annotated[str | None, Field(description="For BID_CAP / COST_CAP")] = None,
        promoted_object: Annotated[
            dict[str, Any] | None,
            Field(description='e.g. {"pixel_id": "...", "custom_event_type": "PURCHASE"} or {"page_id": "..."}'),
        ] = None,
        start_time: Annotated[str | None, Field(description="ISO-8601 with zone")] = None,
        end_time: Annotated[str | None, Field(description="ISO-8601 with zone")] = None,
    ) -> Proposal:
        """Show a new ad set, created PAUSED. Writes nothing.

        The targeting goes through the same validator as
        preview_ad_set_targeting (including the campaign's special ad
        categories), and the budget through the same ceilings as the budget
        kinds. On a campaign-budget campaign the budget is the campaign's and
        this refuses one here. Next: preview_create_creative and
        preview_create_ad.
        """
        campaign = campaigns.find(ctx.client, campaign_id=campaign_id)
        decision = ctx.name_for_create("ad set", name)
        name = decision.final
        cbo = campaigns.budget_level(campaign) == "campaign"
        currency = ctx.currency_of(campaign)
        create: dict[str, Any] = {
            "name": name, "campaign_id": str(campaign["id"]), "status": "PAUSED",
            "optimization_goal": optimization_goal, "billing_event": billing_event,
            "targeting": targeting,
        }
        full_campaign = ctx.read.node(str(campaign["id"]), ("id", "special_ad_categories"))
        checks = validate_spec(ctx, targeting, campaign=full_campaign)
        checks.append(check_currency(ctx.limits, currency))
        if cbo:
            if daily_budget or bid_strategy:
                raise ValueError(
                    f"Campaign {campaign.get('name')!r} holds the budget and bid strategy "
                    "(CBO); an ad set under it takes neither."
                )
        else:
            if not daily_budget:
                raise ValueError("An ad set under an ad-set-budget campaign needs daily_budget.")
            amount = parse_money(daily_budget, "daily_budget")
            checks.append(check_ceiling("max_daily_budget", amount, ctx.limits.max_daily_budget, currency))
            create["daily_budget"] = to_minor_units(amount, currency)
            create["bid_strategy"] = bid_strategy or "LOWEST_COST_WITHOUT_CAP"
        if bid_amount:
            bid = parse_money(bid_amount, "bid_amount")
            checks.append(check_ceiling("max_bid_amount", bid, ctx.limits.max_bid_amount, currency))
            create["bid_amount"] = to_minor_units(bid, currency)
        if promoted_object:
            create["promoted_object"] = promoted_object
        if start_time:
            create["start_time"] = parse_time(start_time, "start_time").isoformat()
        if end_time:
            create["end_time"] = parse_time(end_time, "end_time").isoformat()
        if not decision.refused:
            checks.append(name_is_free(ctx, adsets, name, campaign_id=str(campaign["id"])))
        proposal = Proposal(
            entity_type="ad set", entity_id="(new)", entity_name=name,
            path=f"campaign {campaign.get('id')} ({campaign.get('name')}) > {name}",
            field_name="created", before=NOTHING,
            after=(f"PAUSED ad set | {optimization_goal} | "
                   + (f"budget {daily_budget}/day" if daily_budget else "campaign budget")),
            checks=checks, projected=Decimal("0"), payload={"create": create},
            campaign_id=str(campaign["id"]), account_id=campaign.get("account_id"),
            warnings=["created PAUSED: nothing spends until a human activates it"],
        )
        proposal.name(decision)
        return proposal

    def build(self, ctx, payload):
        create = payload["create"]
        return PlannedChange(action="create", entity="ad set", entity_type="adset", node_id="",
                             label=create["name"], payload=dict(create),
                             changes=[FieldChange("ad set", None, create["name"])], edge="adsets")


#: Graph type, and readable type, of what a /copies response lists.
_COPY_TYPES = {"campaign": ("campaign", "campaign"), "adset": ("adset", "ad set"),
               "ad": ("ad", "ad")}


class _Duplicate(_Creates):
    """Meta's /copies, PAUSED -- then, with a naming identity configured, one
    rename per object it created, so every copy carries OUR prefix on its
    source's base (Meta would keep or decorate the source's names). Each
    rename is its own ledger row; together they count against
    max_entities_per_call. With no identity, Meta's own copy naming is kept.
    """

    child_edge = ""
    noun = ""
    graph_type = ""

    def _children(self, ctx, source_id: str) -> list[dict[str, Any]]:
        rows = ctx.read.edge(str(source_id), self.child_edge, ("id", "name", "status"),
                             limit=200, max_items=201)
        return rows

    def _tree(self, ctx, row, children) -> list[tuple[str, str, str]]:
        """(source id, graph type, source name) of everything the copy makes."""
        tree = [(str(row["id"]), self.graph_type, row.get("name") or "")]
        child_type = "adset" if self.child_edge == "adsets" else "ad"
        for child in children:
            tree.append((str(child["id"]), child_type, child.get("name") or ""))
            if child_type == "adset":
                for grandchild in ctx.read.edge(str(child["id"]), "ads", ("id", "name"),
                                                limit=200, max_items=201):
                    tree.append((str(grandchild["id"]), "ad", grandchild.get("name") or ""))
        return tree

    def _proposal(self, ctx, row, deep_copy: bool) -> Proposal:
        children = self._children(ctx, str(row["id"])) if deep_copy else []
        if len(children) > 200:
            raise ValueError(f"More than 200 {self.child_edge} under it; copy it in Ads Manager.")
        listing = [f"{c.get('id')} {c.get('name')}" for c in children]
        checks = [Check("children_listed", True,
                        f"{len(children)} {self.child_edge} will be copied: {listing[:20]}"
                        + (" ..." if len(listing) > 20 else ""))]
        decisions = []
        renames: list[dict[str, str]] = []
        if ctx.identity.configured or ctx.identity.problem:
            tree = self._tree(ctx, row, children)
            limit = ctx.limits.max_entities_per_call
            checks.append(Check(
                "copy_renames", len(tree) <= limit,
                f"{len(tree)} object(s) to name with {ctx.identity.prefix!r} (the copy and "
                f"everything under it), one rename each, against max_entities_per_call {limit}"
                + ("" if len(tree) <= limit else "; copy less (deep_copy=False) or in Ads Manager"),
            ))
            for source_id, graph_type, source_name in tree:
                decision = ctx.name_for_copy(_COPY_TYPES[graph_type][1], source_name)
                decisions.append(decision)
                renames.append({"source_id": source_id, "graph_type": graph_type,
                                "source_name": source_name, "final": decision.final})
        top_name = renames[0]["final"] if renames else f"copy of {row.get('name')}"
        proposal = Proposal(
            entity_type=self.noun, entity_id="(new)", entity_name=top_name,
            path=row.get("name") or "", field_name="created", before=NOTHING,
            after=f"PAUSED copy of {self.noun} {row['id']}"
            + (f" with {len(children)} {self.child_edge}" if deep_copy else " (no children)")
            + (f", named {top_name!r}" if renames else ""),
            checks=checks,
            projected=Decimal("0"),
            payload={"source_id": str(row["id"]), "deep_copy": bool(deep_copy), "renames": renames},
            campaign_id=str(row.get("campaign_id") or row["id"]),
            account_id=row.get("account_id"),
            warnings=["everything is copied PAUSED (status_option=PAUSED); nothing spends until activated"]
            + ([f"{len(renames)} rename(s) follow the copy, one ledger entry each: "
                + "; ".join(r["final"] for r in renames[:10]) + (" ..." if len(renames) > 10 else "")]
               if renames else []),
        )
        for decision in decisions:
            proposal.name(decision)
        if not renames:
            # No identity: Meta's own copy naming is kept, and said so.
            from metaads_reporting.naming import NO_IDENTITY_RULE

            proposal.naming.append({"requested": None, "final": None, "prefix": "",
                                    "base": None, "length": None, "max_length": None,
                                    "rule": f"{NO_IDENTITY_RULE}; Meta's copy naming is kept"})
        return proposal

    def build(self, ctx, payload):
        return PlannedChange(
            action="create", entity=self.noun, entity_type=self.graph_type,
            node_id=payload["source_id"], label=f"copy of {payload['source_id']}",
            payload={"status_option": "PAUSED", "deep_copy": "true" if payload["deep_copy"] else "false"},
            changes=[FieldChange("copy", None, payload["source_id"])], edge="copies",
        )

    def prepare_apply(self, ctx, plan, payload):
        """Re-count what the copy will make, before anything is sent.

        The preview counted the tree against max_entities_per_call; an ad
        added since would be copied (and need its own rename) without ever
        being counted. Over the limit now, the apply is refused: nothing is
        copied.
        """
        if payload.get("renames"):
            source_id = str(payload["source_id"])
            children = self._children(ctx, source_id) if payload.get("deep_copy") else []
            count = len(self._tree(ctx, {"id": source_id}, children))
            limit = ctx.limits.max_entities_per_call
            if count > limit:
                raise ValueError(
                    f"The copy would now make {count} object(s), each needing a rename, "
                    f"against max_entities_per_call {limit} (the preview counted "
                    f"{len(payload['renames'])}): something was added since. Nothing was "
                    "copied. Take a fresh preview, copy less (deep_copy=False), or copy "
                    "it in Ads Manager."
                )
        return plan

    def follow_ups(self, ctx, payload, result, created):
        """One name update per object the copy created, to its previewed name.

        An object Meta copied that the preview did not list is named the same
        way; if that name is refused, or renaming it would go over
        max_entities_per_call, it is skipped and reported (a ``None`` plan),
        never sent.
        """
        from metaads_mcp.changes.base import LedgerItem

        renames = {r["source_id"]: r for r in payload.get("renames") or []}
        if not renames:
            return []
        limit = ctx.limits.max_entities_per_call
        counted = len(renames)
        made = [(str(payload["source_id"]), result.node_id, self.graph_type)]
        for child in result.response.get("ad_object_ids") or []:
            if isinstance(child, dict) and child.get("copied_id"):
                made.append((str(child.get("source_id")), str(child["copied_id"]),
                             str(child.get("ad_object_type") or "")))
        out = []
        for source_id, copied_id, graph_type in made:
            if not copied_id:
                continue
            wanted = renames.get(source_id)
            graph_type = (wanted or {}).get("graph_type") or graph_type
            if graph_type not in _COPY_TYPES:
                continue
            live = None
            if wanted is None:
                # Not in the preview's tree (Meta copied more than it listed):
                # named the same way, from its source -- unless that name is
                # refused or the rename would exceed the limit.
                source = ctx.read.node(source_id, ("id", "name"))
                decision = ctx.name_for_copy(_COPY_TYPES[graph_type][1], source.get("name") or "")
                final = decision.final
                live = ctx.read.node(copied_id, ("id", "name")).get("name") or ""
                if live == final:
                    continue
                refusal = "; ".join(c.detail for c in decision.checks if not c.ok)
                if not refusal and counted >= limit:
                    refusal = (f"renaming it would make {counted + 1} renames against "
                               f"max_entities_per_call {limit}")
                if refusal:
                    out.append((None, LedgerItem(
                        entity_type=_COPY_TYPES[graph_type][1], entity_id=copied_id,
                        entity_name=live, field_name="name", before=live, after=live,
                        inverse={"refused": f"not renamed (the preview did not list it): {refusal}"})))
                    continue
                counted += 1
            else:
                final = wanted["final"]
            if live is None:
                live = ctx.read.node(copied_id, ("id", "name")).get("name") or ""
            if live == final:
                continue
            plan = PlannedChange(
                action="update", entity=_COPY_TYPES[graph_type][1], entity_type=graph_type,
                node_id=copied_id, label=f"{final} (id {copied_id})", payload={"name": final},
                changes=[FieldChange("name", live, final)])
            item = LedgerItem(
                entity_type=_COPY_TYPES[graph_type][1], entity_id=copied_id, entity_name=final,
                field_name="name", before=live, after=final,
                inverse={"kind": "low_risk_change", "previous": {"name": live}, "field": "name",
                         "graph_field": "name", "graph_type": graph_type})
            out.append((plan, item))
        return out


class DuplicateAdSet(_Duplicate):
    name = "duplicate_ad_set"
    preview_title = "Preview copying an ad set (and its ads), PAUSED"
    apply_title = "Create the PAUSED copy the preview described"
    module = adsets
    id_arg = "ad_set_id"
    child_edge = "ads"
    noun = "ad set"
    graph_type = "adset"
    creates_named = ("ad set", "ad")

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="The ad set to copy")],
        deep_copy: Annotated[bool, Field(description="Copy its ads too")] = True,
    ) -> Proposal:
        """Show a PAUSED copy of an ad set, with its ads when deep_copy.

        Uses Meta's /copies with status_option=PAUSED. The preview lists every
        ad that will be created. The usual first step of an A/B test: copy,
        change one variable, then preview_create_ab_test.
        """
        return self._proposal(ctx, adsets.find(ctx.client, ad_set_id=ad_set_id), deep_copy)


class DuplicateCampaign(_Duplicate):
    name = "duplicate_campaign"
    preview_title = "Preview copying a campaign (and its ad sets and ads), PAUSED"
    apply_title = "Create the PAUSED copy the preview described"
    module = campaigns
    id_arg = "campaign_id"
    child_edge = "adsets"
    noun = "campaign"
    graph_type = "campaign"
    creates_named = ("campaign", "ad set", "ad")

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="The campaign to copy")],
        deep_copy: Annotated[bool, Field(description="Copy its ad sets and ads too")] = True,
    ) -> Proposal:
        """Show a PAUSED copy of a campaign, with its ad sets and ads when deep_copy.

        Uses Meta's /copies with status_option=PAUSED; the copy keeps the
        source's special ad categories. The preview lists every ad set that
        will be created.
        """
        return self._proposal(ctx, campaigns.find(ctx.client, campaign_id=campaign_id), deep_copy)


CREATION_KINDS = (CreateCampaign, CreateAdSet, DuplicateAdSet, DuplicateCampaign)

__all__ = ["CREATION_KINDS", "ads"]
