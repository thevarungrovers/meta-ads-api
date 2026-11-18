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

        row = client.node(str(campaign_id), CAMPAIGN_DETAIL)
        # The campaign's OWN account's currency, not the default account's.
        currency = client.currency(str(row["account_id"]) if row.get("account_id") else None)
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
        row = client.node(str(ad_set_id), ADSET_DETAIL)
        currency = client.currency(str(row["account_id"]) if row.get("account_id") else None)
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

    helpers = dict(listing=listing, edge=edge, account=account, headroom=headroom,
                   money=money, read_only=read_only, local_only=local_only)
    register_assets(mcp, state, client, **helpers)
    register_more(mcp, state, client, **helpers)


TargetingType = Literal[
    "interest", "interest_suggestion", "behaviors", "demographics", "life_events",
    "industries", "income", "family_statuses", "geo", "locale",
    "education_school", "work_employer",
]

#: search_targeting type -> Meta /search params.
_SEARCH = {
    "interest": {"type": "adinterest"},
    "interest_suggestion": {"type": "adinterestsuggestion"},
    "behaviors": {"type": "adTargetingCategory", "class": "behaviors"},
    "demographics": {"type": "adTargetingCategory", "class": "demographics"},
    "life_events": {"type": "adTargetingCategory", "class": "life_events"},
    "industries": {"type": "adTargetingCategory", "class": "industries"},
    "income": {"type": "adTargetingCategory", "class": "income"},
    "family_statuses": {"type": "adTargetingCategory", "class": "family_statuses"},
    "geo": {"type": "adgeolocation"},
    "locale": {"type": "adlocale"},
    "education_school": {"type": "adeducationschool"},
    "work_employer": {"type": "adworkemployer"},
}


def audience_usage(client, *, max_pages: int = 10) -> tuple[dict[str, list[dict]], bool]:
    """audience id -> the ad sets that include or exclude it, and whether the
    scan reached the end of the account."""
    rows = client.edge(
        account_path(client.resolve_account_id()), "adsets",
        ("id", "name", "targeting", "effective_status"),
        limit=200, max_items=200 * max_pages, max_pages=max_pages,
        effective_status=json.dumps(
            ["ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "IN_PROCESS", "WITH_ISSUES"]
        ),
    )
    used: dict[str, list[dict]] = {}
    for row in rows:
        targeting = row.get("targeting") or {}
        for mode, key in (("include", "custom_audiences"), ("exclude", "excluded_custom_audiences")):
            for audience in targeting.get(key) or []:
                aid = str(audience.get("id") if isinstance(audience, dict) else audience)
                used.setdefault(aid, []).append(
                    {"ad_set_id": str(row.get("id")), "name": row.get("name"), "mode": mode,
                     "effective_status": row.get("effective_status")}
                )
    return used, len(rows) < 200 * max_pages


def register_assets(mcp, state, client, *, listing, edge, account, headroom, money,
                    read_only, local_only) -> None:  # noqa: C901
    """Audiences, targeting search, reach, assets and diagnostics."""
    from metaads_reporting import reach
    from metaads_reporting.audience_status import approx_size, usability
    from metaads_reporting.write.audiences import FIELDS as AUDIENCE_FIELDS

    state.audience_usage = lambda: audience_usage(client)

    @mcp.tool(title="Read one audience in full, and who uses it", annotations=read_only)
    @refusals_reach_the_caller
    def get_audience(
        audience_id: Annotated[str, Field(description="From list_audiences")],
    ) -> dict[str, Any]:
        """One custom or lookalike audience: its rule or source, lookalike
        country and ratio, approximate size, status and whether it is usable
        yet, and every ad set that includes or excludes it.

        A rule-based audience takes hours to populate after creation; `usable`
        says whether Meta can deliver to it now.
        """
        row = client.node(str(audience_id), AUDIENCE_FIELDS)
        used, complete = audience_usage(client)
        return {
            **row,
            "approx_size": approx_size(row),
            **{("usable" if k == "usable" else f"usable_{k}"): v for k, v in usability(row).items()},
            "used_by": used.get(str(audience_id), []),
            "used_by_complete": complete,
            "rate_limit": headroom(),
        }

    @mcp.tool(title="Search targeting options: interests, places, behaviours", annotations=read_only)
    @refusals_reach_the_caller
    def search_targeting(
        type: Annotated[TargetingType, Field(description="What to search")],
        q: Annotated[str | None, Field(description="Search text (an interest name for interest_suggestion)")] = None,
        location_types: Annotated[
            list[Literal["country", "region", "city", "zip", "geo_market", "electoral_district"]] | None,
            Field(description="For type=geo: which kinds of place"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """Ids ready to paste into a targeting spec, with Meta's audience-size
        bounds and path. Interests, behaviours and demographics go in
        `flexible_spec`; places in `geo_locations` (by `key`); locales in
        `locales`.

        Category types (behaviors, demographics, life_events, industries,
        income, family_statuses) list the whole class; `q` then filters it by
        name here.
        """
        params = dict(_SEARCH[type])
        if type == "interest_suggestion":
            if not q:
                raise ValueError("interest_suggestion needs q: an interest name to expand.")
            params["interest_list"] = json.dumps([q])
        elif params.get("type") != "adTargetingCategory":
            if not q:
                raise ValueError(f"type={type} needs q.")
            params["q"] = q
        if type == "geo":
            params["location_types"] = json.dumps(location_types or ["country", "region", "city"])
        params["limit"] = min(limit + 1, 500)
        rows = client.get("search", **params).get("data") or []
        if q and params.get("type") == "adTargetingCategory":
            folded = q.casefold()
            rows = [r for r in rows if folded in str(r.get("name", "")).casefold()]
        return listing(rows, limit, lambda r: {
            "id": r.get("id") or r.get("key"),
            "key": r.get("key"),
            "name": r.get("name"),
            "type": r.get("type") or type,
            "path": r.get("path"),
            "audience_size_lower_bound": r.get("audience_size_lower_bound"),
            "audience_size_upper_bound": r.get("audience_size_upper_bound"),
            "country_code": r.get("country_code"),
        })

    @mcp.tool(title="Estimate the reach of a targeting spec", annotations=read_only)
    @refusals_reach_the_caller
    def estimate_reach(
        targeting: Annotated[dict[str, Any], Field(description="A Meta targeting spec")],
        optimization_goal: Annotated[
            str, Field(description="The ad set's optimization goal, e.g. REACH or OFFSITE_CONVERSIONS")
        ] = "REACH",
    ) -> dict[str, Any]:
        """Monthly active people the spec can reach: Meta's rounded lower and
        upper bounds, from act_/delivery_estimate. Custom audiences may be part
        of the spec. `ready: false` means Meta is still computing -- ask again
        shortly. These are estimates, not counts.
        """
        out = reach.estimate(client, targeting, optimization_goal)
        out["rate_limit"] = headroom()
        return out


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


__all__ = ["register"]


#: Rule execution types that change money. A rule is an unattended writer that
#: bypasses every guardrail here, so an ENABLED one of these is flagged.
MONEY_RULE_EXECUTIONS = frozenset({"CHANGE_BUDGET", "CHANGE_CAMPAIGN_BUDGET", "CHANGE_BID"})


def register_more(mcp, state, client, *, listing, edge, account, headroom, money,
                  read_only, local_only) -> None:  # noqa: C901
    """Media, labels, pixels, identities, forms, rules, studies, catalogs, history."""
    import datetime as dt

    @mcp.tool(title="List the account's ad images", annotations=read_only)
    @refusals_reach_the_caller
    def list_ad_images(limit: Annotated[int, Field(ge=1, le=500)] = 50) -> dict[str, Any]:
        """Image hashes in the account's library, with width and height, for
        reuse in a creative and for the placement shape checks."""
        rows = edge(account(), "adimages", ("hash", "name", "width", "height", "created_time"), limit)
        return listing(rows, limit)

    @mcp.tool(title="List the account's ad videos", annotations=read_only)
    @refusals_reach_the_caller
    def list_ad_videos(limit: Annotated[int, Field(ge=1, le=500)] = 50) -> dict[str, Any]:
        """Video ids with length (seconds) and processing status. A video must
        be `ready` before an ad can use it; uploading new video is not offered
        here -- reuse an existing id."""
        rows = edge(account(), "advideos", ("id", "title", "length", "status", "created_time"), limit)
        return listing(rows, limit, lambda r: {
            **r, "ready": (r.get("status") or {}).get("video_status") == "ready"
            if isinstance(r.get("status"), dict) else None,
        })

    @mcp.tool(title="List ad labels", annotations=read_only)
    @refusals_reach_the_caller
    def list_labels(limit: Annotated[int, Field(ge=1, le=500)] = 100) -> dict[str, Any]:
        """The account's ad labels (act_/adlabels), for organising and filtering."""
        return listing(edge(account(), "adlabels", ("id", "name", "created_time"), limit), limit)

    @mcp.tool(title="List custom conversions", annotations=read_only)
    @refusals_reach_the_caller
    def list_custom_conversions(limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict[str, Any]:
        """Custom conversions: their event type, rule, pixel and when they last fired."""
        rows = edge(account(), "customconversions", (
            "id", "name", "custom_event_type", "rule", "pixel", "last_fired_time",
            "is_archived"), limit)
        return listing(rows, limit)

    @mcp.tool(title="List the account's pixels", annotations=read_only)
    @refusals_reach_the_caller
    def list_pixels(limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict[str, Any]:
        """Pixels (datasets) the account uses, with when each last fired. A pixel
        that has not fired recently cannot feed a website audience or a
        conversion goal."""
        rows = edge(account(), "adspixels", ("id", "name", "last_fired_time", "is_unavailable",
                                             "creation_time"), limit)
        return listing(rows, limit)

    @mcp.tool(title="Check one pixel's health", annotations=read_only)
    @refusals_reach_the_caller
    def get_pixel_health(
        pixel_id: Annotated[str, Field(description="From list_pixels")],
    ) -> dict[str, Any]:
        """When the pixel last fired, event counts by event name, and the share
        of events from the Conversions API versus the browser, where Meta
        reports it. Read-only diagnostics: nothing here configures a pixel or
        sends an event.

        The event names are the ones a website audience rule can use.
        """
        node = client.node(str(pixel_id), ("id", "name", "last_fired_time", "is_unavailable"))
        out: dict[str, Any] = {**node}
        for aggregation in ("event", "event_source"):
            try:
                data = client.get(f"{pixel_id}/stats", aggregation=aggregation).get("data") or []
            except Exception as exc:  # noqa: BLE001 - one aggregation may be refused
                out[f"by_{aggregation}"] = {"unavailable": str(exc).splitlines()[0]}
                continue
            counts: dict[str, int] = {}
            for bucket in data:
                for entry in bucket.get("data") or []:
                    key = str(entry.get("value"))
                    try:
                        counts[key] = counts.get(key, 0) + int(entry.get("count") or 0)
                    except (TypeError, ValueError):
                        continue
            out[f"by_{aggregation}"] = counts
        events = out.get("by_event") if isinstance(out.get("by_event"), dict) else {}
        out["events_seen"] = sorted(events)
        out["rate_limit"] = headroom()
        return out

    @mcp.tool(title="List the Pages, Instagram accounts and apps the account can use",
              annotations=read_only)
    @refusals_reach_the_caller
    def list_connected_identities() -> dict[str, Any]:
        """Facebook Pages and Instagram accounts the ad account can advertise
        as, and its advertisable apps. Engagement audiences and a creative's
        identity take their ids from here."""
        out: dict[str, Any] = {}
        for key, name, fields in (
            ("pages", "promote_pages", ("id", "name")),
            ("instagram_accounts", "instagram_accounts", ("id", "username")),
            ("apps", "advertisable_applications", ("id", "name")),
        ):
            try:
                out[key] = edge(account(), name, fields, 100)[:100]
            except Exception as exc:  # noqa: BLE001 - one edge may need a permission
                out[key] = {"unavailable": str(exc).splitlines()[0]}
        out["rate_limit"] = headroom()
        return out

    @mcp.tool(title="List a Page's lead forms", annotations=read_only)
    @refusals_reach_the_caller
    def list_lead_forms(
        page_id: Annotated[str, Field(description="From list_connected_identities")],
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Form metadata, status and lead counts. Never the leads themselves:
        downloading submissions (personal data) is out of scope.

        Needs the Page assigned to the System User with lead access; without
        it Meta answers PERMISSION. Creating a form stays in Ads Manager: it
        carries a privacy policy and legal text a person answers for.
        """
        rows = edge(str(page_id), "leadgen_forms",
                    ("id", "name", "status", "leads_count", "locale", "created_time"), limit)
        return listing(rows, limit)

    @mcp.tool(title="List automated rules", annotations=read_only)
    @refusals_reach_the_caller
    def list_ad_rules(limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict[str, Any]:
        """The account's automated rules: name, status, what they evaluate and
        what they do. An ENABLED rule that changes budgets or bids is flagged:
        it acts unattended, outside every guardrail and cap on this server."""
        rows = edge(account(), "adrules_library", (
            "id", "name", "status", "evaluation_spec", "execution_spec", "schedule_spec",
            "created_time", "updated_time"), limit)

        def render(r):
            execution = (r.get("execution_spec") or {}).get("execution_type")
            return {**r, "changes_money": execution in MONEY_RULE_EXECUTIONS,
                    "warning": (
                        "ENABLED and changes money outside this server's guardrails"
                        if execution in MONEY_RULE_EXECUTIONS and r.get("status") == "ENABLED"
                        else None)}

        return listing(rows, limit, render)

    @mcp.tool(title="List A/B tests and lift studies", annotations=read_only)
    @refusals_reach_the_caller
    def list_ab_tests(limit: Annotated[int, Field(ge=1, le=100)] = 25) -> dict[str, Any]:
        """Split tests and lift studies on the account (act_/ad_studies): type,
        schedule and description. get_ab_test reads one with its cells."""
        rows = edge(account(), "ad_studies", (
            "id", "name", "type", "start_time", "end_time", "description",
            "created_time"), limit)
        return listing(rows, limit)

    @mcp.tool(title="Read one A/B test with its cells", annotations=read_only)
    @refusals_reach_the_caller
    def get_ab_test(
        study_id: Annotated[str, Field(description="From list_ab_tests")],
    ) -> dict[str, Any]:
        """One study: type, schedule, and each cell's split and the ad sets or
        campaigns in it. Results are read in Ads Manager or with run_report on
        the cells' ad sets; Meta exposes few study results over the API."""
        row = client.node(str(study_id), ("id", "name", "type", "start_time", "end_time",
                                          "description", "objectives"))
        try:
            cells = client.edge(str(study_id), "cells", (
                "id", "name", "treatment_percentage", "adsets", "campaigns"), limit=50,
                max_items=50)
        except Exception as exc:  # noqa: BLE001
            cells = [{"unavailable": str(exc).splitlines()[0]}]
        return {**row, "cells": cells, "rate_limit": headroom()}

    @mcp.tool(title="List product catalogs", annotations=read_only)
    @refusals_reach_the_caller
    def list_catalogs(limit: Annotated[int, Field(ge=1, le=100)] = 25) -> dict[str, Any]:
        """Product catalogs the business owns, for catalog-sales campaigns.
        Needs META_ADS_BUSINESS_ID. Read-only: feeds are not edited here."""
        business = client.settings.business_id
        if not business:
            raise ValueError(
                "Catalogs belong to a business, not an ad account: set "
                "META_ADS_BUSINESS_ID in .env to list them."
            )
        rows = edge(str(business), "owned_product_catalogs",
                    ("id", "name", "product_count", "vertical"), limit)
        return listing(rows, limit)

    @mcp.tool(title="List a catalog's product sets", annotations=read_only)
    @refusals_reach_the_caller
    def list_product_sets(
        catalog_id: Annotated[str, Field(description="From list_catalogs")],
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Product sets in a catalog, with their product counts and filters."""
        rows = edge(str(catalog_id), "product_sets", ("id", "name", "product_count", "filter"), limit)
        return listing(rows, limit)

    @mcp.tool(title="What changed in the account recently, and who changed it",
              annotations=read_only)
    @refusals_reach_the_caller
    def list_recent_changes(
        days: Annotated[int, Field(description="How far back", ge=1, le=90)] = 7,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """The account's activity log (act_/activities): every change made in
        Ads Manager, by the API or by a rule, with who made it and when.

        The usual explanation for a preview refused as stale: somebody changed
        the entity in Ads Manager between preview and apply.
        """
        until = dt.datetime.now(dt.timezone.utc)
        since = until - dt.timedelta(days=days)
        rows = edge(account(), "activities", (
            "event_time", "event_type", "translated_event_type", "object_id",
            "object_name", "object_type", "actor_name", "application_name",
            "extra_data"), limit,
            since=int(since.timestamp()), until=int(until.timestamp()))
        return listing(rows, limit)

    @mcp.tool(title="Search Meta's Ad Library", annotations=read_only)
    @refusals_reach_the_caller
    def search_ad_library(
        q: Annotated[str, Field(description="Search terms")],
        country: Annotated[str, Field(description="Two-letter country code, e.g. CA")],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Ads in Meta's public Ad Library (ads_archive).

        LIMITATION: outside the EU the Ad Library API returns only political and
        issue ads, and it needs a separately identity-verified token -- for
        competitors' ordinary ads in North America it will usually return
        nothing or a PERMISSION error. The Ad Library website shows more than
        the API does.
        """
        code = (country or "").strip().upper()
        if len(code) != 2:
            raise ValueError(f"country must be a two-letter code, got {country!r}.")
        data = client.get(
            "ads_archive",
            search_terms=q,
            ad_reached_countries=json.dumps([code]),
            ad_type="ALL",
            fields="id,page_name,ad_creative_bodies,ad_delivery_start_time,ad_snapshot_url",
            limit=limit + 1,
        ).get("data") or []
        return listing(data, limit)
