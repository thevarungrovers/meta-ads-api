"""Ad verification: what a change to an ad will look like, and what it became.

Part of the change FRAMEWORK, not of any one tool. A kind that creates or
changes an ad or a creative declares ``verifies_ad = True`` and the engine
runs this for it -- no kind can skip it.

BEFORE APPLY (in the preview)
  * The placements the ad set actually uses (its targeting, or all of them under
    Advantage+ placements), ranked by recent spend, capped at
    ``max_preview_formats``.
  * Preview links per placement: ``/{creative_id}/previews`` for an existing
    creative, ``act_/generatepreviews`` for one that does not exist yet. Both
    GET. An image that is only uploaded at apply has no preview yet, and says so.
  * Checks: text truncation, image ratio and width, video length per
    placement; a CTA where the objective needs one; and the destination link,
    followed through its redirects (see metaads_reporting.linkcheck). A FAIL
    blocks the apply, a WARN does not.

AFTER APPLY (in the apply result, and in the ledger)
  * The ad and its creative re-read, and diffed field by field against what the
    preview approved. A mismatch does NOT auto-revert: it returns
    ``passed: false`` and says what differs.
  * Review state: right after apply an ad is usually pending review, so
    ``verify_ad`` repeats this later.
  * Fresh preview links from ``/{ad_id}/previews``.

PREVIEW LINKS ARE RETURNED, NOT STORED. They are signed and short-lived, and
anyone holding one sees an unpublished ad. The ledger keeps the placement and
the time, never the URL (:func:`for_ledger`). The server cannot "see" a
rendering -- the page is drawn by JavaScript -- so the human opens the links,
or an agent with a browser tool does; nobody claims to have looked at one
they did not open.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from typing import Any

from metaads_mcp.guardrails import Check
from metaads_reporting import linkcheck, placement_specs
from metaads_reporting.account_id import account_path
from metaads_reporting.write import media

#: Objectives where an ad without a call to action cannot do its job.
CTA_REQUIRED_OBJECTIVES = frozenset(
    {"OUTCOME_LEADS", "OUTCOME_SALES", "LEAD_GENERATION", "CONVERSIONS", "PRODUCT_CATALOG_SALES"}
)

#: Fields compared after apply, in Ads Manager's words.
VERIFIED_FIELDS = (
    "primary_text",
    "headline",
    "description",
    "link",
    "call_to_action",
    "url_tags",
    "image_hash",
    "video_id",
    "page_id",
    "instagram_user_id",
)

AD_FIELDS = (
    "id",
    "name",
    "status",
    "configured_status",
    "effective_status",
    "adset_id",
    "campaign_id",
    "creative",
    "ad_review_feedback",
    "issues_info",
)

CREATIVE_FIELDS = (
    "id",
    "object_story_spec",
    "asset_feed_spec",
    "url_tags",
    "call_to_action_type",
    "image_hash",
    "video_id",
    "actor_id",
    "instagram_user_id",
)

_IFRAME = re.compile(r"<iframe[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _json(value: Any) -> Any:
    """A Graph value that may arrive JSON-encoded (as a create sent it)."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# What a creative says, flattened
# ---------------------------------------------------------------------------


def approved_from_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    """A creative payload (or a creative read back) as Ads Manager's fields.

    >>> approved_from_spec({"object_story_spec": {"page_id": "P", "link_data": {
    ...     "message": "body", "name": "head", "link": "https://example.com",
    ...     "call_to_action": {"type": "SHOP_NOW"}, "image_hash": "h"}}})["headline"]
    'head'
    """
    spec = dict(spec or {})
    story = _json(spec.get("object_story_spec")) or {}
    feed = _json(spec.get("asset_feed_spec")) or {}
    out: dict[str, Any] = {key: None for key in VERIFIED_FIELDS}
    out["url_tags"] = spec.get("url_tags") or None
    out["page_id"] = story.get("page_id")
    out["instagram_user_id"] = (
        story.get("instagram_user_id")
        or story.get("instagram_actor_id")
        or spec.get("instagram_user_id")
    )
    if "link_data" in story:
        block = story["link_data"] or {}
        out.update(
            primary_text=block.get("message"),
            headline=block.get("name"),
            description=block.get("description"),
            link=block.get("link"),
            call_to_action=(block.get("call_to_action") or {}).get("type"),
            image_hash=block.get("image_hash"),
        )
    elif "video_data" in story:
        block = story["video_data"] or {}
        cta = block.get("call_to_action") or {}
        out.update(
            primary_text=block.get("message"),
            headline=block.get("title"),
            description=block.get("link_description"),
            link=(cta.get("value") or {}).get("link"),
            call_to_action=cta.get("type"),
            video_id=block.get("video_id"),
            image_hash=block.get("image_hash"),
        )
    if feed:
        out.update(
            primary_text=[e.get("text") for e in feed.get("bodies") or []] or out["primary_text"],
            headline=[e.get("text") for e in feed.get("titles") or []] or out["headline"],
            description=[e.get("text") for e in feed.get("descriptions") or []]
            or out["description"],
            link=[e.get("website_url") for e in feed.get("link_urls") or []] or out["link"],
            call_to_action=(feed.get("call_to_action_types") or [None])[0]
            or out["call_to_action"],
            image_hash=[e.get("hash") for e in feed.get("images") or []] or out["image_hash"],
            video_id=[e.get("video_id") for e in feed.get("videos") or []] or out["video_id"],
        )
    if spec.get("call_to_action_type") and not out["call_to_action"]:
        out["call_to_action"] = spec["call_to_action_type"]
    return out


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


# ---------------------------------------------------------------------------
# Placements and preview links
# ---------------------------------------------------------------------------


def formats_for_ad_set(ctx, adset_id: str | None) -> list[str]:
    """The ad_formats worth previewing for this ad set, most-spent first."""
    if not adset_id:
        return list(placement_specs.DEFAULT_FORMAT_ORDER)[: _cap(ctx)]
    try:
        row = ctx.read.node(str(adset_id), ("id", "targeting"))
        targeting = _json(row.get("targeting")) or {}
    except Exception:  # noqa: BLE001 - previews are context; fall back to defaults
        targeting = {}
    formats = placement_specs.formats_for_targeting(targeting)
    spent = _spend_by_format(ctx, adset_id)
    if spent:
        formats.sort(key=lambda f: -spent.get(f, -1))
    return formats[: _cap(ctx)]


def _cap(ctx) -> int:
    return int(getattr(ctx.limits, "max_preview_formats", 5) or 5)


def _spend_by_format(ctx, adset_id: str) -> dict[str, float]:
    from metaads_reporting.insights import InsightsQuery

    try:
        rows = ctx.read.insights(
            InsightsQuery(
                fields=("spend",),
                level="adset",
                date_preset="last_30d",
                breakdowns=("publisher_platform", "platform_position"),
                limit=100,
            ),
            node_id=str(adset_id),
            max_pages=2,
        )
    except Exception:  # noqa: BLE001
        return {}
    spent: dict[str, float] = {}
    for row in rows:
        ad_format = placement_specs.format_for_breakdown(
            row.get("publisher_platform"), row.get("platform_position")
        )
        if ad_format:
            try:
                spent[ad_format] = spent.get(ad_format, 0.0) + float(row.get("spend") or 0)
            except (TypeError, ValueError):
                continue
    return spent


def parse_preview(body: str | None) -> dict[str, Any] | None:
    """The iframe a preview's ``body`` holds: its src, width and height.

    >>> parse_preview('<iframe src="https://business.facebook.com/ads/api/preview_iframe.php?d=1&amp;t=2" width="540" height="690"></iframe>')
    {'iframe_url': 'https://business.facebook.com/ads/api/preview_iframe.php?d=1&t=2', 'width': 540, 'height': 690}
    """
    if not body:
        return None
    match = _IFRAME.search(body)
    if not match:
        return None
    attrs = dict(_ATTR.findall(match.group(0)))
    if "src" not in attrs:
        return None

    def number(key):
        try:
            return int(attrs.get(key) or 0) or None
        except ValueError:
            return None

    return {
        "iframe_url": html.unescape(attrs["src"]),
        "width": number("width"),
        "height": number("height"),
    }


def fetch_previews(
    ctx,
    formats: list[str],
    *,
    creative_id: str | None = None,
    spec: dict[str, Any] | None = None,
    ad_id: str | None = None,
) -> list[dict[str, Any]]:
    """One preview link per ad_format, or a reason it has none."""
    out: list[dict[str, Any]] = []
    for ad_format in formats:
        label = placement_specs.SPECS.get(ad_format)
        entry: dict[str, Any] = {
            "placement": label.label if label else ad_format,
            "ad_format": ad_format,
        }
        if spec is not None and "<pending upload>" in json.dumps(spec, default=str):
            entry["unavailable"] = "image not uploaded until apply"
            out.append(entry)
            continue
        try:
            if ad_id:
                answer = ctx.read.get(f"{ad_id}/previews", ad_format=ad_format)
            elif creative_id:
                answer = ctx.read.get(f"{creative_id}/previews", ad_format=ad_format)
            elif spec is not None:
                answer = ctx.read.get(
                    f"{account_path(ctx.target_account())}/generatepreviews",
                    creative=json.dumps(spec),
                    ad_format=ad_format,
                )
            else:
                entry["unavailable"] = "nothing to preview"
                out.append(entry)
                continue
        except Exception as exc:  # noqa: BLE001 - one refused format is not fatal
            entry["unavailable"] = f"Meta refused this format: {str(exc).splitlines()[0]}"
            out.append(entry)
            continue
        data = (answer or {}).get("data") or []
        parsed = parse_preview((data[0] or {}).get("body") if data else None)
        if parsed:
            entry.update(parsed)
        else:
            entry["unavailable"] = "Meta returned no preview for this format"
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def text_checks(fields: dict[str, Any], formats: list[str]) -> list[Check]:
    checks: list[Check] = []
    for label, attr in (
        ("primary_text", "primary_text_visible"),
        ("headline", "headline_max"),
        ("description", "description_max"),
    ):
        texts = _texts(fields.get(label))
        if not texts:
            continue
        cut = []
        for ad_format in formats:
            spec = placement_specs.spec_for(ad_format)
            limit = getattr(spec, attr, None) if spec else None
            if limit and any(len(t) > limit for t in texts):
                cut.append(f"{spec.label} ({limit})")
        longest = max(len(t) for t in texts)
        if cut:
            checks.append(Check(
                f"ad_text_{label}", True,
                f"{longest} characters; cut off in {', '.join(cut)}", warn=True,
            ))
        else:
            checks.append(Check(f"ad_text_{label}", True, f"{longest} characters fit every placement"))
    return checks


def ratio_checks(dimensions: tuple[int, int] | None, formats: list[str], source: str) -> list[Check]:
    if not dimensions:
        return [Check("ad_image_shape", True, f"dimensions of {source} unknown; not checked",
                      skipped=True)]
    width, height = dimensions
    ratio = width / height if height else 0
    checks: list[Check] = []
    too_small = []
    cropped = []
    soft = []
    for ad_format in formats:
        spec = placement_specs.spec_for(ad_format)
        if spec is None:
            continue
        if width < spec.hard_min_width:
            too_small.append(f"{spec.label} (minimum {spec.hard_min_width}px)")
        elif width < spec.recommended_min_width:
            soft.append(spec.label)
        if not (spec.min_ratio - 0.01 <= ratio <= spec.max_ratio + 0.01):
            cropped.append(spec.label)
    shape = f"{width}x{height} ({ratio:.2f}:1)"
    if too_small:
        checks.append(Check("ad_image_width", False,
                            f"{shape} is below Meta's minimum in {', '.join(too_small)}"))
    elif soft:
        checks.append(Check("ad_image_width", True,
                            f"{shape} is under the recommended width in {', '.join(soft)}",
                            warn=True))
    if cropped:
        checks.append(Check("ad_image_shape", True,
                            f"{shape} will be cropped or letterboxed in {', '.join(cropped)}",
                            warn=True))
    elif not too_small:
        checks.append(Check("ad_image_shape", True, f"{shape} fits every placement previewed"))
    return checks


def video_checks(length: float | None, formats: list[str]) -> list[Check]:
    if length is None:
        return []
    over = [
        f"{spec.label} ({spec.video_max_seconds}s)"
        for spec in (placement_specs.spec_for(f) for f in formats)
        if spec and spec.video_max_seconds and length > spec.video_max_seconds
    ]
    if over:
        return [Check("ad_video_length", True,
                      f"{length:.0f}s is longer than {', '.join(over)} plays", warn=True)]
    return [Check("ad_video_length", True, f"{length:.0f}s fits every placement previewed")]


def cta_check(cta: Any, objective: str | None) -> Check:
    present = bool(cta[0] if isinstance(cta, list) and cta else cta) and cta != "NO_BUTTON"
    if present:
        return Check("ad_call_to_action", True, f"{cta}")
    if objective in CTA_REQUIRED_OBJECTIVES:
        return Check("ad_call_to_action", False,
                     f"no call to action on a {objective} campaign; the ad has nothing to click")
    return Check("ad_call_to_action", True, "no call to action", warn=True)


#: Answers that come from a bot wall or a rate limit rather than the page.
BOT_WALL_STATUSES = (401, 403, 429)


def link_check(link: Any, url_tags: str | None) -> Check:
    links = _texts(link)
    if not links:
        return Check("ad_destination", True, "no destination link on this creative", skipped=True)
    target = linkcheck.with_url_tags(links[0], url_tags)
    result = linkcheck.check_link(target)
    detail = f"{target} -> {result.final_url or target}"
    if result.refused:
        return Check("ad_destination", False, f"{detail}: refused to check ({result.refused})")
    if result.error:
        return Check("ad_destination", True, f"{detail}: could not check ({result.error})", warn=True)
    if result.status in BOT_WALL_STATUSES:
        # A bot wall or rate limit answers the checker, not the page's
        # visitors: say so, but do not block on it.
        return Check("ad_destination", True,
                     f"{detail}: HTTP {result.status} to the checker -- open it in a browser",
                     warn=True)
    if result.status is not None and result.status >= 400:
        return Check("ad_destination", False, f"{detail}: HTTP {result.status}")
    if result.changed_host:
        return Check("ad_destination", True,
                     f"{detail}: HTTP {result.status}, redirects to another host", warn=True)
    return Check("ad_destination", True, f"{detail}: HTTP {result.status}")


def _objective(ctx, adset_id: str | None) -> str | None:
    if not adset_id:
        return None
    try:
        adset = ctx.read.node(str(adset_id), ("id", "campaign_id"))
        campaign = ctx.read.node(str(adset.get("campaign_id")), ("id", "objective"))
        return campaign.get("objective")
    except Exception:  # noqa: BLE001
        return None


def _image_dimensions(ctx, spec_fields: dict[str, Any], image_path: str | None):
    if image_path:
        try:
            return media.local_image_dimensions(image_path), "the uploaded file"
        except OSError:
            return None, "the file"
    hashes = [h for h in _texts(spec_fields.get("image_hash")) if h != "<pending upload>"]
    if not hashes:
        return None, "the image"
    try:
        answer = ctx.read.get(
            f"{account_path(ctx.target_account())}/adimages",
            hashes=json.dumps(hashes[:1]),
            fields="hash,width,height",
        )
        row = ((answer or {}).get("data") or [{}])[0]
        if row.get("width") and row.get("height"):
            return (int(row["width"]), int(row["height"])), f"image {hashes[0]}"
    except Exception:  # noqa: BLE001
        pass
    return None, f"image {hashes[0]}"


def _video_length(ctx, video_id: Any) -> float | None:
    ids = _texts(video_id)
    if not ids:
        return None
    try:
        row = ctx.read.node(ids[0], ("id", "length"))
        return float(row.get("length")) if row.get("length") is not None else None
    except Exception:  # noqa: BLE001
        return None


def before_apply(ctx, target: dict[str, Any]) -> tuple[list[Check], list[dict[str, Any]]]:
    """Checks and preview links for an ad change, before anyone approves it.

    ``target``: ``adset_id``, and one of ``spec`` (a creative payload) or
    ``creative_id``; optionally ``image_path`` and ``video_id``.
    """
    adset_id = target.get("adset_id")
    formats = formats_for_ad_set(ctx, adset_id)
    spec = target.get("spec")
    creative_id = target.get("creative_id")
    if spec is None and creative_id:
        try:
            spec = ctx.read.node(str(creative_id), CREATIVE_FIELDS)
        except Exception:  # noqa: BLE001
            spec = {}
    fields = approved_from_spec(spec)
    if target.get("video_id"):
        fields["video_id"] = target["video_id"]

    checks: list[Check] = []
    checks.extend(text_checks(fields, formats))
    if fields.get("video_id"):
        checks.extend(video_checks(_video_length(ctx, fields["video_id"]), formats))
    else:
        dims, source = _image_dimensions(ctx, fields, target.get("image_path"))
        checks.extend(ratio_checks(dims, formats, source))
    checks.append(cta_check(fields.get("call_to_action"), _objective(ctx, adset_id)))
    checks.append(link_check(fields.get("link"), fields.get("url_tags")))

    previews = fetch_previews(
        ctx, formats,
        creative_id=None if target.get("spec") is not None else creative_id,
        spec=target.get("spec"),
    )
    return checks, previews


# ---------------------------------------------------------------------------
# After apply
# ---------------------------------------------------------------------------


def review_state(ad: dict[str, Any]) -> dict[str, Any]:
    """Where the ad is in Meta's review, from its effective_status."""
    effective = ad.get("effective_status") or ad.get("status")
    feedback = ad.get("ad_review_feedback") or None
    issues = ad.get("issues_info") or None
    if effective in ("PENDING_REVIEW", "IN_PROCESS"):
        state = "pending"
    elif effective == "DISAPPROVED" or feedback:
        state = "disapproved"
    elif effective == "WITH_ISSUES" or issues:
        state = "with_issues"
    else:
        state = "not_flagged"
    return {
        "state": state,
        "effective_status": effective,
        "ad_review_feedback": feedback,
        "issues_info": issues,
    }


def diff_fields(approved: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name in VERIFIED_FIELDS:
        want = approved.get(name)
        if want in (None, [], ""):
            continue
        got = live.get(name)
        rows.append({"field": name, "approved": want, "live": got, "ok": want == got})
    return rows


def after_apply(ctx, ad_id: str | None, approved: dict[str, Any]) -> dict[str, Any]:
    """Re-read the ad and its creative, diff them, and fetch fresh previews."""
    if not ad_id:
        return {"passed": False, "error": "no ad id to verify", "checked_at": _now()}
    try:
        ad = ctx.read.node(str(ad_id), AD_FIELDS)
    except Exception as exc:  # noqa: BLE001 - a failed re-read is a finding, not a crash
        return {
            "ad_id": str(ad_id),
            "passed": False,
            "error": f"could not re-read the ad: {str(exc).splitlines()[0]}",
            "checked_at": _now(),
        }
    creative = _json(ad.get("creative")) or {}
    creative_id = str(creative.get("id") or creative.get("creative_id") or "") or None
    live: dict[str, Any] = {}
    if creative_id:
        try:
            live = approved_from_spec(ctx.read.node(creative_id, CREATIVE_FIELDS))
        except Exception:  # noqa: BLE001
            live = {}
    fields = diff_fields(approved or {}, live)
    review = review_state(ad)
    formats = formats_for_ad_set(ctx, ad.get("adset_id"))
    previews = fetch_previews(ctx, formats, ad_id=str(ad_id))
    passed = all(f["ok"] for f in fields) and review["state"] not in ("disapproved", "with_issues")
    differs = [f["field"] for f in fields if not f["ok"]]
    return {
        "ad_id": str(ad_id),
        "creative_id": creative_id,
        "fields": fields,
        "review": review,
        "previews": previews,
        "passed": passed,
        "checked_at": _now(),
        "note": (
            f"live differs from what was approved in: {', '.join(differs)}. Nothing "
            "was reverted; decide with the human."
            if differs
            else "review is pending: call verify_ad later (Meta says up to 48h)"
            if review["state"] == "pending"
            else None
        ),
    }


def after_create_creative(ctx, creative_id: str | None, approved: dict[str, Any],
                          adset_id: str | None = None) -> dict[str, Any]:
    """The after-apply check for a creative no ad uses yet: re-read and diff it,
    and fetch its previews. There is no review state until an ad uses it."""
    if not creative_id:
        return {"passed": False, "error": "no creative id to verify", "checked_at": _now()}
    try:
        live = approved_from_spec(ctx.read.node(str(creative_id), CREATIVE_FIELDS))
    except Exception as exc:  # noqa: BLE001
        return {"creative_id": str(creative_id), "passed": False,
                "error": f"could not re-read the creative: {str(exc).splitlines()[0]}",
                "checked_at": _now()}
    fields = diff_fields(approved or {}, live)
    previews = fetch_previews(ctx, formats_for_ad_set(ctx, adset_id), creative_id=str(creative_id))
    return {
        "creative_id": str(creative_id),
        "fields": fields,
        "review": {"state": "not_reviewed", "note": "a creative is reviewed once an ad uses it"},
        "previews": previews,
        "passed": all(f["ok"] for f in fields),
        "checked_at": _now(),
    }


def for_ledger(verification: dict[str, Any] | None) -> dict[str, Any] | None:
    """The verification without the signed preview URLs.

    Anyone with a preview link sees an unpublished ad, and the links expire:
    keeping them would store a credential-like string that is useless a day
    later. Placement and time are kept.
    """
    if verification is None:
        return None
    out = dict(verification)
    out["previews"] = [
        {k: v for k, v in p.items() if k != "iframe_url"}
        for p in verification.get("previews") or []
    ]
    return out
