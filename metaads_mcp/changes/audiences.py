"""Creating audiences. Never deleting one: the undo is not using it.

One tool pair, ``create_audience(kind, ...)``. Every kind is built from data
Meta ALREADY holds -- pixel events, Page/Instagram engagement, video views,
lead-form interactions, app events, an existing audience for a lookalike, or a
plain targeting spec for a saved audience. None uploads personal data:
customer-list audiences stay in Ads Manager (see write/audiences.py).

The rule shapes follow Meta's rule-based custom audience format: an
``inclusions`` block of rules, each with ``event_sources``, a retention and an
event filter. The preview sends them with validate_only, which is the backstop
for an event name Meta does not recognise.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.base import APPLY_REVERSIBLE, NOTHING, ChangeKind, Proposal
from metaads_mcp.guardrails import Check
from metaads_reporting.account_id import account_path
from metaads_reporting.write import audiences

AudienceKind = Literal[
    "saved", "lookalike", "website", "engagement_page", "engagement_ig",
    "video_viewers", "lead_form", "app_activity",
]

PAGE_EVENTS = ("page_engaged", "page_visited", "page_liked", "page_messaged",
               "page_cta_clicked", "page_saved")
IG_EVENTS = ("ig_business_profile_all", "ig_business_profile_engaged",
             "ig_business_profile_visit", "ig_user_messaged_business",
             "ig_business_profile_ad_saved")
#: Watch thresholds -> Meta's video event names.
VIDEO_EVENTS = {
    "3s": "video_watched",
    "thruplay": "thruplay_watched",
    "25%": "video_view_25_percent",
    "50%": "video_view_50_percent",
    "75%": "video_view_75_percent",
    "95%": "video_completed",
}
LEAD_EVENTS = {
    "opened": "lead_generation_opened",
    "submitted": "lead_generation_submitted",
    "opened_not_submitted": "lead_generation_dropoff",
}
MAX_VIDEOS = 50
DAY = 86400


def _rule(sources: list[dict[str, str]], retention_days: int, filters: list[dict]) -> dict:
    return {
        "inclusions": {
            "operator": "or",
            "rules": [{
                "event_sources": sources,
                "retention_seconds": int(retention_days) * DAY,
                "filter": {"operator": "and", "filters": filters},
            }],
        }
    }


def _retention(kind: str, days: int | None) -> int:
    low, high = audiences.RETENTION_DAYS[kind]
    days = int(days or high // 6 or 30)
    if not low <= days <= high:
        raise ValueError(f"retention_days must be {low}-{high} for this audience, got {days}.")
    return days


class CreateAudience(ChangeKind):
    name = "create_audience"
    preview_title = "Preview creating an audience"
    apply_title = "Create the audience the preview described"
    apply_annotations = APPLY_REVERSIBLE
    revertible = False
    creates_named = ("audience",)

    # -- identities and sources the account can use ---------------------

    def _ids(self, ctx, edge: str) -> set[str]:
        rows = ctx.read.edge(account_path(ctx.read.resolve_account_id()), edge, ("id",),
                             limit=200, max_items=500)
        return {str(r.get("id")) for r in rows}

    def _plan(self, ctx, payload: dict[str, Any]):  # noqa: C901 - one branch per kind
        kind = payload["kind"]
        name = payload["name"]
        if kind == "saved":
            return audiences.plan_create_saved(ctx.client, name=name,
                                               targeting=payload.get("targeting") or {})
        if kind == "lookalike":
            if not payload.get("origin_audience_id") or not payload.get("country"):
                raise ValueError(
                    "A lookalike needs origin_audience_id and country. Use "
                    "list_audiences to find a usable source."
                )
            return audiences.plan_create_lookalike(
                ctx.client, name=name, origin_audience_id=payload["origin_audience_id"],
                country=payload["country"], ratio=payload["ratio"],
            )
        if kind == "website":
            pixel = payload.get("pixel_id")
            if not pixel:
                raise ValueError("A website audience needs pixel_id (from list_pixels).")
            days = _retention("WEBSITE", payload.get("retention_days"))
            filters = [{"field": "event", "operator": "eq",
                        "value": payload.get("event") or "PageView"}]
            if payload.get("url_contains"):
                filters.append({"field": "url", "operator": "i_contains",
                                "value": payload["url_contains"]})
            return audiences.plan_create_rule_audience(
                ctx.client, name=name, subtype="WEBSITE", retention_days=days,
                rule=_rule([{"id": str(pixel), "type": "pixel"}], days, filters),
                summary=f"pixel {pixel}: {payload.get('event') or 'PageView'}"
                + (f" on URLs containing {payload['url_contains']!r}" if payload.get("url_contains") else "")
                + f", last {days} days",
            )
        if kind in ("engagement_page", "engagement_ig"):
            source = payload.get("page_id") if kind == "engagement_page" else payload.get("ig_account_id")
            if not source:
                raise ValueError(f"{kind} needs {'page_id' if kind == 'engagement_page' else 'ig_account_id'} "
                                 "(from list_connected_identities).")
            events = PAGE_EVENTS if kind == "engagement_page" else IG_EVENTS
            event = payload.get("engagement_event") or events[0]
            if event not in events:
                raise ValueError(f"engagement_event must be one of {', '.join(events)}.")
            days = _retention("ENGAGEMENT", payload.get("retention_days"))
            source_type = "page" if kind == "engagement_page" else "ig_business"
            return audiences.plan_create_rule_audience(
                ctx.client, name=name, subtype="ENGAGEMENT",
                rule=_rule([{"id": str(source), "type": source_type}], days,
                           [{"field": "event", "operator": "eq", "value": event}]),
                summary=f"{source_type} {source}: {event}, last {days} days",
            )
        if kind == "video_viewers":
            videos = [str(v) for v in payload.get("video_ids") or []]
            if not videos:
                raise ValueError("video_viewers needs video_ids (from list_ad_videos).")
            if len(videos) > MAX_VIDEOS:
                raise ValueError(f"At most {MAX_VIDEOS} videos per audience, got {len(videos)}.")
            threshold = payload.get("watch_threshold") or "3s"
            if threshold not in VIDEO_EVENTS:
                raise ValueError(f"watch_threshold must be one of {', '.join(VIDEO_EVENTS)}.")
            days = _retention("ENGAGEMENT", payload.get("retention_days"))
            return audiences.plan_create_rule_audience(
                ctx.client, name=name, subtype="ENGAGEMENT",
                rule=_rule([{"id": v, "type": "video"} for v in videos], days,
                           [{"field": "event", "operator": "eq", "value": VIDEO_EVENTS[threshold]}]),
                summary=f"{len(videos)} video(s), watched {threshold}, last {days} days",
            )
        if kind == "lead_form":
            form = payload.get("lead_form_id")
            if not form:
                raise ValueError("lead_form needs lead_form_id (from list_lead_forms).")
            event = payload.get("form_event") or "opened"
            if event not in LEAD_EVENTS:
                raise ValueError(f"form_event must be one of {', '.join(LEAD_EVENTS)}.")
            days = _retention("LEAD", payload.get("retention_days"))
            return audiences.plan_create_rule_audience(
                ctx.client, name=name, subtype="ENGAGEMENT",
                rule=_rule([{"id": str(form), "type": "lead"}], days,
                           [{"field": "event", "operator": "eq", "value": LEAD_EVENTS[event]}]),
                summary=f"lead form {form}: {event}, last {days} days",
            )
        if kind == "app_activity":
            app = payload.get("app_id")
            if not app or not payload.get("app_event"):
                raise ValueError("app_activity needs app_id and app_event.")
            days = _retention("APP", payload.get("retention_days"))
            return audiences.plan_create_rule_audience(
                ctx.client, name=name, subtype="APP", retention_days=days,
                rule=_rule([{"id": str(app), "type": "app"}], days,
                           [{"field": "event", "operator": "eq", "value": payload["app_event"]}]),
                summary=f"app {app}: {payload['app_event']}, last {days} days",
            )
        raise ValueError(
            f"kind {kind!r} is not available. A customer-list audience is not "
            f"available here at all: it would upload personal data to Meta."
        )

    def _checks(self, ctx, payload: dict[str, Any]) -> tuple[list[Check], list[str]]:
        kind = payload["kind"]
        checks: list[Check] = []
        warnings: list[str] = []
        try:
            if kind == "website":
                owned = self._ids(ctx, "adspixels")
                pixel = str(payload["pixel_id"])
                checks.append(Check("pixel_in_account", pixel in owned,
                                    f"pixel {pixel} {'belongs' if pixel in owned else 'does NOT belong'} to the account"))
                event = payload.get("event") or "PageView"
                if pixel in owned:
                    stats = ctx.read.get(f"{pixel}/stats", aggregation="event").get("data") or []
                    seen = {str(e.get("value")) for b in stats for e in (b.get("data") or [])}
                    if event not in seen:
                        warnings.append(
                            f"{event} has not fired on pixel {pixel} recently; the audience "
                            "will be empty until it does"
                        )
            elif kind in ("engagement_page", "engagement_ig"):
                edge = "promote_pages" if kind == "engagement_page" else "instagram_accounts"
                source = str(payload.get("page_id") or payload.get("ig_account_id"))
                owned = self._ids(ctx, edge)
                checks.append(Check("identity_connected", source in owned,
                                    f"{source} {'is' if source in owned else 'is NOT'} connected to the account"))
            elif kind == "app_activity":
                owned = self._ids(ctx, "advertisable_applications")
                app = str(payload.get("app_id"))
                checks.append(Check("app_connected", app in owned,
                                    f"app {app} {'is' if app in owned else 'is NOT'} advertisable by the account"))
            elif kind == "video_viewers":
                for video in payload.get("video_ids") or []:
                    ctx.read.node(str(video), ("id",))
                checks.append(Check("videos_readable", True,
                                    f"{len(payload.get('video_ids') or [])} video(s) readable by this token"))
            elif kind == "lead_form":
                form = ctx.read.node(str(payload["lead_form_id"]), ("id", "status"))
                status = form.get("status")
                checks.append(Check("lead_form_usable", status in (None, "ACTIVE"),
                                    f"form status {status or 'unknown'}"))
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a FAIL
            checks.append(Check(f"{kind}_source", False, str(exc).splitlines()[0]))
        return checks, warnings

    def propose(
        self,
        ctx,
        kind: Annotated[AudienceKind, Field(description="What the audience is built from")],
        name: Annotated[str, Field(description="BASE name for the new audience; the owner/agent prefix is added")],
        targeting: Annotated[
            dict[str, Any] | None, Field(description="saved: Meta's targeting spec")
        ] = None,
        origin_audience_id: Annotated[
            str | None, Field(description="lookalike: a usable source, from list_audiences")
        ] = None,
        country: Annotated[str | None, Field(description="lookalike: two-letter code, e.g. CA")] = None,
        ratio: Annotated[
            float, Field(description="lookalike: 0.01 to 0.10 of the country", ge=0.01, le=0.10)
        ] = 0.01,
        pixel_id: Annotated[str | None, Field(description="website: from list_pixels")] = None,
        event: Annotated[
            str | None, Field(description="website: PageView, ViewContent, AddToCart, Purchase or a custom event")
        ] = None,
        url_contains: Annotated[str | None, Field(description="website: only URLs containing this")] = None,
        page_id: Annotated[str | None, Field(description="engagement_page: from list_connected_identities")] = None,
        ig_account_id: Annotated[str | None, Field(description="engagement_ig: from list_connected_identities")] = None,
        engagement_event: Annotated[
            str | None, Field(description="engagement: page_engaged, page_visited, ig_business_profile_engaged ...")
        ] = None,
        video_ids: Annotated[list[str] | None, Field(description="video_viewers: up to 50 video ids")] = None,
        watch_threshold: Annotated[
            Literal["3s", "thruplay", "25%", "50%", "75%", "95%"] | None,
            Field(description="video_viewers: how much they watched"),
        ] = None,
        lead_form_id: Annotated[str | None, Field(description="lead_form: from list_lead_forms")] = None,
        form_event: Annotated[
            Literal["opened", "submitted", "opened_not_submitted"] | None,
            Field(description="lead_form: what they did"),
        ] = None,
        app_id: Annotated[str | None, Field(description="app_activity: a connected app")] = None,
        app_event: Annotated[str | None, Field(description="app_activity: the app event")] = None,
        retention_days: Annotated[
            int | None, Field(description="How long someone stays in: website 1-180, engagement 1-365", ge=1, le=365)
        ] = None,
    ) -> Proposal:
        """Show a new audience. Writes nothing. Audiences are never deleted here.

        Kinds: `saved` (a targeting spec), `lookalike` (1-10% of a country,
        from a usable source of at least 100 people), `website` (pixel
        events), `engagement_page` / `engagement_ig` (people who engaged with a
        connected Page or Instagram account), `video_viewers`, `lead_form`
        (opened or submitted a form), `app_activity`.

        **Customer-list audiences are not available here and never will be.**
        Building one uploads hashed customer emails and phone numbers to Meta;
        that is personal data leaving the company, and it belongs in Ads
        Manager where a person accepts the data terms. The attempt is refused
        structurally, not just undocumented.

        A rule-based audience populates over hours: create it, then wait for
        get_audience to report it usable before attaching it with
        preview_ad_set_targeting. An unused audience changes nothing;
        list_audiences(unused_only=True) shows them.
        """
        decision = ctx.name_for_create("audience", name)
        name = decision.final
        payload = {
            "kind": (kind or "").strip().lower(),
            "name": name,
            "targeting": targeting,
            "origin_audience_id": origin_audience_id,
            "country": country,
            "ratio": ratio,
            "pixel_id": pixel_id,
            "event": event,
            "url_contains": url_contains,
            "page_id": page_id,
            "ig_account_id": ig_account_id,
            "engagement_event": engagement_event,
            "video_ids": video_ids,
            "watch_threshold": watch_threshold,
            "lead_form_id": lead_form_id,
            "form_event": form_event,
            "app_id": app_id,
            "app_event": app_event,
            "retention_days": retention_days,
        }
        plan = self._plan(ctx, payload)
        checks, warnings = self._checks(ctx, payload)
        if payload["kind"] == "saved":
            # The same validator an ad set's targeting goes through.
            from metaads_mcp.changes.targeting import validate_spec

            checks.extend(validate_spec(ctx, targeting or {}, campaign=None, require_geo=False))
        proposal = Proposal(
            entity_type=plan.entity,
            entity_id="(new)",
            entity_name=name,
            path=f"account {ctx.default_account}",
            field_name="audience",
            before=NOTHING,
            after=" | ".join(f"{c.field}: {c.after}" for c in plan.changes),
            checks=checks,
            projected=Decimal("0"),
            payload=payload,
            warnings=[*plan.warnings, *warnings],
            note=(
                "Creating an audience changes no delivery on its own -- it is "
                "available for an ad set to use. Projected delta is 0. Its size "
                "cannot be estimated before it exists; get_audience reports it."
            ),
        )
        proposal.name(decision)
        return proposal

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return NOTHING

    def build(self, ctx, payload: dict[str, Any]):
        return self._plan(ctx, payload)

    def inverse(self, ctx, entry: dict[str, Any]):
        raise ValueError(
            f"Entry {entry['entry_id']} created an audience, and audiences are "
            "never deleted here (remove no). An audience no ad set uses changes "
            "nothing; if one is attached, detach it with preview_ad_set_targeting. "
            "list_audiences(unused_only=True) shows the ones nothing uses."
        )


__all__ = ["CreateAudience"]
