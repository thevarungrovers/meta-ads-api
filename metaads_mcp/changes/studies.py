"""A/B tests: a split test between existing PAUSED ad sets or campaigns.

Creating the study spends nothing -- the cells only spend once they are
activated through the status kinds, where the projected delta is bounded
there. The study is never deleted: its inverse ends it early (end_time now).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.ads_create import CREATE_WEIGHT
from metaads_mcp.changes.base import APPLY_SPENDS, NOTHING, ChangeKind, Proposal
from metaads_mcp.changes.bidding import parse_time
from metaads_mcp.changes.common import as_obj, canonical
from metaads_mcp.changes.creative import creative_id_of
from metaads_mcp.changes.targeting import fingerprint
from metaads_mcp.guardrails import Check
from metaads_reporting.write import ads
from metaads_reporting.write.plan import FieldChange, PlannedChange

MAX_STUDY_DAYS = 90
PLACEMENT_KEYS = ("publisher_platforms", "facebook_positions", "instagram_positions",
                  "messenger_positions", "audience_network_positions")


def _ad_set_signature(ctx, ad_set_id: str) -> dict[str, str]:
    row = ctx.read.node(str(ad_set_id), ("id", "status", "targeting", "campaign_id"))
    targeting = as_obj(row.get("targeting")) or {}
    audience = {k: v for k, v in targeting.items() if k not in PLACEMENT_KEYS}
    placements = {k: targeting.get(k) for k in PLACEMENT_KEYS if targeting.get(k)}
    creatives = sorted(filter(None, (creative_id_of(a) for a in
                                     ads.search(ctx.client, None, ad_set_id=str(ad_set_id), limit=50))))
    return {"status": row.get("status") or "", "audience": fingerprint(audience),
            "placement": canonical(placements), "creative": canonical(creatives)}


class CreateAbTest(ChangeKind):
    name = "create_ab_test"
    adds_no_spend = True
    preview_title = "Preview creating an A/B (split) test between PAUSED ad sets or campaigns"
    apply_title = "Create the A/B test the preview described"
    apply_annotations = APPLY_SPENDS
    weight = CREATE_WEIGHT
    creates_named = ("ad study",)

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="Study BASE name; the owner/agent prefix is added (cell names are left as given)")],
        variable: Annotated[
            Literal["creative", "audience", "placement"],
            Field(description="The ONE thing the cells differ in"),
        ],
        cells: Annotated[
            list[dict[str, Any]],
            Field(description='2-5 cells: [{"name": "A", "ad_set_ids": ["..."], "treatment_percentage": 50}, ...] '
                              '(or "campaign_ids")'),
        ],
        start_time: Annotated[str, Field(description="ISO-8601 with zone, in the future")],
        end_time: Annotated[str, Field(description="ISO-8601 with zone, at most 90 days later")],
        description: Annotated[str | None, Field(description="What is being tested")] = None,
    ) -> Proposal:
        """Show a new split test. Writes nothing.

        The cells must be existing, PAUSED ad sets (or campaigns) that differ in
        exactly the declared variable -- the preview compares their specs and
        warns otherwise. Splits must add up to 100. The test only starts
        spending when a human activates the cells with the status tools. Revert
        ends it early; it is never deleted.
        """
        decision = ctx.name_for_create("ad study", name)
        name = decision.final
        if not 2 <= len(cells or []) <= 5:
            raise ValueError(f"A split test has 2-5 cells, got {len(cells or [])}.")
        start = parse_time(start_time, "start_time")
        end = parse_time(end_time, "end_time")
        if end <= start:
            raise ValueError("end_time must be after start_time.")
        if (end - start).days > MAX_STUDY_DAYS:
            raise ValueError(f"A study runs at most {MAX_STUDY_DAYS} days here.")
        total = sum(int(c.get("treatment_percentage") or 0) for c in cells)
        checks = [Check("cell_split", total == 100, f"treatment percentages add up to {total}")]
        out_cells, signatures, campaign_ids = [], [], []
        accounts: list[tuple[str, Any]] = []
        for index, cell in enumerate(cells):
            adset_ids = [str(a) for a in cell.get("ad_set_ids") or []]
            campaign_cell = [str(c) for c in cell.get("campaign_ids") or []]
            if bool(adset_ids) == bool(campaign_cell):
                raise ValueError(f"Cell {index + 1} needs ad_set_ids or campaign_ids, not both.")
            entry: dict[str, Any] = {"name": cell.get("name") or f"Cell {index + 1}",
                                     "treatment_percentage": int(cell.get("treatment_percentage") or 0)}
            if adset_ids:
                entry["adsets"] = adset_ids
                sig = [_ad_set_signature(ctx, a) for a in adset_ids]
                for a, s in zip(adset_ids, sig):
                    checks.append(Check(f"cell_paused[{a}]", s["status"] == "PAUSED",
                                        f"ad set {a} is {s['status']} (cells start PAUSED)"))
                    row = ctx.read.node(a, ("id", "campaign_id", "account_id"))
                    campaign_ids.append(str(row.get("campaign_id")))
                    accounts.append((f"ad set {a}", row.get("account_id")))
                signatures.append({k: canonical([s[k] for s in sig]) for k in ("audience", "placement", "creative")})
            else:
                entry["campaigns"] = campaign_cell
                for c in campaign_cell:
                    row = ctx.read.node(c, ("id", "status", "account_id"))
                    accounts.append((f"campaign {c}", row.get("account_id")))
                    checks.append(Check(f"cell_paused[{c}]", row.get("status") == "PAUSED",
                                        f"campaign {c} is {row.get('status')} (cells start PAUSED)"))
                    campaign_ids.append(c)
            out_cells.append(entry)
        warnings = []
        if len(signatures) == len(cells):
            differing = [k for k in ("audience", "placement", "creative")
                         if len({s[k] for s in signatures}) > 1]
            if differing != [variable]:
                warnings.append(
                    f"the cells differ in {differing or 'nothing'}, not only in {variable}: the "
                    "test will not isolate that variable"
                )
        create = {"name": name, "type": "SPLIT_TEST", "start_time": int(start.timestamp()),
                  "end_time": int(end.timestamp()), "cells": out_cells}
        if description:
            create["description"] = description
        for campaign in dict.fromkeys(campaign_ids):
            from metaads_mcp.guardrails import check_campaign_in_scope

            checks.append(check_campaign_in_scope(ctx.limits, campaign))
        # Every cell is scope-checked by ITS account, and must sit in the
        # account the study is created in: the engine's own check sees only
        # that one.
        from metaads_mcp.guardrails import check_account_in_scope

        study_account = ctx.target_account()
        for label, account in accounts:
            account = str(account or "").replace("act_", "")
            scoped = check_account_in_scope(ctx.limits, account or "(unknown)", ctx.default_account)
            scoped.name = f"account_in_scope[{label}]"
            checks.append(scoped)
            checks.append(Check(f"cell_account[{label}]", account == study_account,
                                f"{label} is in account {account or '(unknown)'}; the study "
                                f"is created in {study_account}"))
        proposal = Proposal(
            entity_type="ad study", entity_id="(new)", entity_name=name,
            path=f"account {ctx.default_account}", field_name="created", before=NOTHING,
            after=(f"SPLIT_TEST on {variable} | {start.isoformat()} to {end.isoformat()} | "
                   + " / ".join(f"{c['name']} {c['treatment_percentage']}%" for c in out_cells)),
            checks=checks, projected=Decimal("0"), payload={"create": create},
            account_id=ctx.target_account(),
            warnings=[*warnings, "the cells spend only once activated with the status tools"],
            note="Creating a study spends nothing; projected 0.",
        )
        proposal.name(decision)
        return proposal

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        create = payload["create"]
        return PlannedChange(action="create", entity="ad study", entity_type="adstudy", node_id="",
                             label=create["name"], payload=dict(create),
                             changes=[FieldChange("study", None, create["name"])], edge="ad_studies")

    def inverse_for(self, payload, result, created):
        return {"created_id": result.node_id}

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    def revert_reduces_spend(self, ctx, entry):
        # The undo pauses (or ends) what was created: it can only lower spend.
        return True

    def inverse(self, ctx, entry):
        study = (entry.get("inverse") or {}).get("created_id") or entry.get("entity_id")
        now = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 60
        return PlannedChange(action="update", entity="ad study", entity_type="adstudy",
                             node_id=str(study), label=f"study {study}", payload={"end_time": now},
                             changes=[FieldChange("end_time", None, "now (ended early)")])
