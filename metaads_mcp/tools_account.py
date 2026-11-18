"""Read tools for everything needed to decide: one entity in full, audiences,
targeting search, reach estimates, ad verification, and the account's assets.

Registered by :func:`metaads_mcp.tools_read.register`, with the same client and
the same annotations: every tool here is a GET (READ_ONLY), except the ones
that answer from the local ledger alone. List reads return ``count`` and
``truncated`` so a cut-off is never silent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.errors import refusals_reach_the_caller
from metaads_reporting.account_id import account_path
from metaads_reporting.insights import InsightsQuery

#: Fields read for one campaign in full.
CAMPAIGN_DETAIL = (
    "id", "name", "status", "effective_status", "objective", "buying_type",
    "bid_strategy", "daily_budget", "lifetime_budget", "budget_remaining",
    "spend_cap", "start_time", "stop_time", "special_ad_categories",
    "special_ad_category_country", "issues_info", "account_id",
    "created_time", "updated_time",
)

ADSET_DETAIL = (
    "id", "name", "status", "effective_status", "campaign_id", "account_id",
    "daily_budget", "lifetime_budget", "budget_remaining", "bid_amount",
    "bid_strategy", "bid_constraints", "billing_event", "optimization_goal",
    "targeting", "attribution_spec", "start_time", "end_time",
    "promoted_object", "destination_type", "learning_stage_info",
    "frequency_control_specs", "pacing_type", "adset_schedule",
    "is_dynamic_creative", "issues_info",
)

AD_DETAIL = (
    "id", "name", "status", "configured_status", "effective_status",
    "adset_id", "campaign_id", "account_id", "creative", "ad_review_feedback",
    "issues_info", "tracking_specs", "created_time", "updated_time",
)

#: Ad formats a preview may be asked for. See metaads_reporting.placement_specs.
AdFormat = Literal[
    "MOBILE_FEED_STANDARD", "DESKTOP_FEED_STANDARD", "INSTAGRAM_STANDARD",
    "INSTAGRAM_STORY", "INSTAGRAM_REELS", "INSTAGRAM_EXPLORE_CONTEXTUAL",
    "FACEBOOK_REELS_MOBILE", "FACEBOOK_STORY_MOBILE", "MARKETPLACE_MOBILE",
    "RIGHT_COLUMN_STANDARD", "MESSENGER_MOBILE_INBOX_MEDIA",
    "MESSENGER_MOBILE_STORY_MEDIA", "MOBILE_INTERSTITIAL",
]


def register(mcp, state, client, *, headroom, money, read_only, local_only) -> None:  # noqa: C901
    """Attach the decision-support reads to ``mcp``."""

    def listing(rows: list[dict[str, Any]], limit: int, render=None) -> dict[str, Any]:
        shown = rows[:limit]
        return {
            "rows": [render(r) for r in shown] if render else shown,
            "count": len(shown),
            "truncated": len(rows) > limit,
            "rate_limit": headroom(),
        }

    def edge(node: str, name: str, fields: tuple[str, ...], limit: int, **params) -> list[dict]:
        """One more than ``limit``, so ``truncated`` is a fact."""
        return client.edge(node, name, fields, limit=min(limit + 1, 500),
                           max_items=limit + 1, max_pages=10, **params)

    def account() -> str:
        return account_path(client.resolve_account_id())

    def read_ctx():
        """What metaads_mcp.verify needs: reads, limits, the target account."""
        return SimpleNamespace(
            read=client,
            limits=state.limits,
            target_account=lambda: client.resolve_account_id().replace("act_", ""),
        )

    def totals(node_id: str, level: str) -> dict[str, Any]:
        out = {}
        for preset in ("last_7d", "last_30d"):
            try:
                rows = client.insight_rows(InsightsQuery(
                    fields=("spend", "impressions", "clicks", "ctr", "cpc", "actions"),
                    level=level, date_preset=preset, time_increment=None, limit=5,
                ), node_id=str(node_id), max_items=1)
                out[preset] = rows[0] if rows else {"note": "no delivery"}
            except Exception as exc:  # noqa: BLE001 - context, not correctness
                out[preset] = {"unavailable": str(exc).splitlines()[0]}
        return out

    # -- one entity in full -------------------------------------------------

    @mcp.tool(title="Read one campaign in full", annotations=read_only)
    @refusals_reach_the_caller
    def get_campaign(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
    ) -> dict[str, Any]:
        """Objective, buying type, bid strategy, budget level and type, special
        ad categories, schedule, delivery issues, child counts, and the last 7
        and 30 days.

        `special_ad_categories` is a legal declaration (housing, employment,
        credit, social issues) that restricts what targeting is allowed under
        this campaign -- read it before proposing any targeting change.
        """
        from metaads_reporting.write import campaigns as campaign_writes

        currency = client.currency()
        row = client.node(str(campaign_id), CAMPAIGN_DETAIL)
        adsets = client.edge(str(campaign_id), "adsets", ("id",), limit=500, max_items=501)
        ads = client.edge(str(campaign_id), "ads", ("id",), limit=500, max_items=501)
        return {
            **{k: row.get(k) for k in CAMPAIGN_DETAIL if k not in (
                "daily_budget", "lifetime_budget", "budget_remaining", "spend_cap")},
            "budget_level": campaign_writes.budget_level(row),
            "budget_type": campaign_writes.budget_field(row),
            "daily_budget": money(row.get("daily_budget"), currency),
            "lifetime_budget": money(row.get("lifetime_budget"), currency),
            "budget_remaining": money(row.get("budget_remaining"), currency),
            "spend_cap": money(row.get("spend_cap"), currency),
            "currency": currency,
            "ad_set_count": len(adsets) if len(adsets) <= 500 else "500+",
            "ad_count": len(ads) if len(ads) <= 500 else "500+",
            "performance": totals(str(campaign_id), "campaign"),
            "rate_limit": headroom(),
        }

    @mcp.tool(title="Read one ad set in full, targeting included", annotations=read_only)
    @refusals_reach_the_caller
    def get_ad_set(
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
    ) -> dict[str, Any]:
        """The full targeting spec (with Meta's own plain-English
        `targeting_sentences`), optimization goal, billing event, bid strategy
        and constraints, attribution, schedule, placements, learning stage and
        promoted object.

        Read this before ANY targeting or placement change: Meta replaces
        targeting wholesale, so a change is always the current spec plus the
        edit, and the preview shows both.
        """
        currency = client.currency()
        row = client.node(str(ad_set_id), ADSET_DETAIL)
        try:
            sentences = client.get(f"{ad_set_id}/targetingsentencelines").get("targetingsentencelines") or []
        except Exception:  # noqa: BLE001 - a nice-to-have rendering
            sentences = []
        targeting = row.get("targeting") or {}
        from metaads_reporting.placement_specs import formats_for_targeting
        from metaads_reporting.write.adsets import effective_bid_strategy

        out = {k: row.get(k) for k in ADSET_DETAIL if k not in (
            "daily_budget", "lifetime_budget", "budget_remaining", "bid_amount")}
        out.update(
            daily_budget=money(row.get("daily_budget"), currency),
            lifetime_budget=money(row.get("lifetime_budget"), currency),
            budget_remaining=money(row.get("budget_remaining"), currency),
            bid_amount=money(row.get("bid_amount"), currency),
            effective_bid_strategy=effective_bid_strategy(_Shim(client), row),
            currency=currency,
            targeting_sentences=[
                f"{s.get('content', '')} {', '.join(map(str, s.get('children') or []))}".strip()
                for s in sentences if isinstance(s, dict)
            ],
            placements=(
                "Advantage+ placements (automatic)"
                if not targeting.get("publisher_platforms")
                else {k: v for k, v in targeting.items()
                      if k == "publisher_platforms" or k.endswith("_positions")}
            ),
            preview_formats=formats_for_targeting(targeting)[: state.limits.max_preview_formats],
            performance=totals(str(ad_set_id), "adset"),
            rate_limit=headroom(),
        )
        return out

    @mcp.tool(title="Read one ad in full, with preview links", annotations=read_only)
    @refusals_reach_the_caller
    def get_ad(
        ad_id: Annotated[str, Field(description="From find_ads")],
    ) -> dict[str, Any]:
        """The ad's creative (shape and copy), review feedback, delivery issues,
        tracking specs, and preview links for the placements its ad set uses.

        Preview links are signed and short-lived: open them (with a browser
        tool if the client has one) or hand them to the human. Never claim to
        have seen a rendering you did not open.
        """
        from metaads_mcp import verify
        from metaads_reporting.write import creatives

        row = client.node(str(ad_id), AD_DETAIL)
        creative_id = (row.get("creative") or {}).get("id")
        creative = None
        if creative_id:
            try:
                creative = creatives.read(_Shim(client), creative_id).describe()
            except Exception as exc:  # noqa: BLE001
                creative = {"unavailable": str(exc).splitlines()[0]}
        ctx = read_ctx()
        return {
            **{k: row.get(k) for k in AD_DETAIL},
            "creative": creative,
            "review": verify.review_state(row),
            "previews": verify.fetch_previews(
                ctx, verify.formats_for_ad_set(ctx, row.get("adset_id")), ad_id=str(ad_id)
            ),
            "performance": totals(str(ad_id), "ad"),
            "rate_limit": headroom(),
        }

    @mcp.tool(title="Verify an ad: review state, fields and previews", annotations=read_only)
    @refusals_reach_the_caller
    def verify_ad(
        ad_id: Annotated[str, Field(description="The ad to check (from find_ads or an apply result)")],
    ) -> dict[str, Any]:
        """Was it approved, and does it still look like what was approved?

        The after-apply verification, on demand: review state with Meta's
        `ad_review_feedback` reasons and `issues_info`, fresh preview links per
        placement, and -- when this server made the change -- a field-by-field
        diff against the values the human approved (otherwise the live state
        only). Review usually completes within 24 hours (Meta says up to 48).

        Updates the verification stored on the ledger entry; never the preview
        links, which are not stored.
        """
        from metaads_mcp import ledger, verify

        entry = None
        for candidate in reversed(ledger.entries()):
            inverse = candidate.get("inverse") or {}
            if not candidate.get("verification"):
                continue
            if str(ad_id) in (str(candidate.get("entity_id")), str(inverse.get("created_ad_id")),
                              str((candidate["verification"] or {}).get("ad_id"))):
                entry = candidate
                break
        approved = {}
        if entry is not None:
            approved = {f["field"]: f["approved"] for f in entry["verification"].get("fields") or []}
        result = verify.after_apply(read_ctx(), str(ad_id), approved)
        result["entry_id"] = entry["entry_id"] if entry else None
        result["compared_with"] = (
            "the values approved in that ledger entry" if entry
            else "nothing: this server did not make this ad's latest change, so only the live state is shown"
        )
        if entry is not None:
            ledger.set_verification(entry["entry_id"], verify.for_ledger(result))
        return result

    @mcp.tool(title="Preview links for an ad or a creative", annotations=read_only)
    @refusals_reach_the_caller
    def get_ad_previews(
        ad_id: Annotated[str | None, Field(description="An existing ad")] = None,
        creative_id: Annotated[str | None, Field(description="Or an existing creative")] = None,
        formats: Annotated[
            list[AdFormat] | None,
            Field(description="Placements to preview; default: the ad set's, most-spent first"),
        ] = None,
    ) -> dict[str, Any]:
        """Raw preview links for any existing ad or creative, for a review that
        involves no change.

        Each entry is {placement, ad_format, iframe_url, width, height}, or
        {unavailable: reason} where Meta refused that format for this creative.
        Links are signed and expire (about a day): refetch rather than reuse.
        """
        from metaads_mcp import verify

        if bool(ad_id) == bool(creative_id):
            raise ValueError("Give exactly one of ad_id or creative_id.")
        ctx = read_ctx()
        if formats:
            chosen = list(formats)
        elif ad_id:
            row = client.node(str(ad_id), ("id", "adset_id"))
            chosen = verify.formats_for_ad_set(ctx, row.get("adset_id"))
        else:
            chosen = verify.formats_for_ad_set(ctx, None)
        return {
            "previews": verify.fetch_previews(
                ctx, chosen, ad_id=ad_id, creative_id=None if ad_id else creative_id
            ),
            "note": "Signed, short-lived links. Open them; do not store them.",
        }

    register_assets(mcp, state, client, listing=listing, edge=edge, account=account,
                    headroom=headroom, money=money, read_only=read_only, local_only=local_only)


def register_assets(mcp, state, client, **helpers) -> None:
    """Audiences, targeting search, assets and diagnostics. See below."""


class _Shim:
    """The three read attributes the write package's helpers touch."""

    def __init__(self, client) -> None:
        self.read = client
        self._client = client

    def node(self, node_id, fields):
        return self._client.node(node_id, fields)

    def currency(self, account_id=None):
        return self._client.currency(account_id)

    def resolve_account_id(self, override=None):
        return self._client.resolve_account_id(override)


__all__ = ["register", "json"]
