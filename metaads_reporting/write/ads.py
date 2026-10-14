"""Ad reads and writes.

The smallest unit, and the one most worth pausing: on Meta a single worn-out
creative inside an otherwise healthy ad set is the common failure, and pausing
it is both the usual fix and completely reversible.

Only ``status`` and ``name`` are writable here. Changing what an ad SAYS means
swapping its creative, which on Meta is not an edit at all -- a creative is
immutable once attached, so a copy change is a new ad. That is a create, and
creates are not exposed to an agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import MutatingMetaAdsClient, MutationError, check_status
from .lookup import Candidate, NotFound, match_by_name
from .plan import FieldChange, PlannedChange

ENTITY = "ad"
EDGE = "ads"

FIELDS = (
    "id",
    "name",
    "status",
    "effective_status",
    "adset_id",
    "campaign_id",
    "created_time",
    "updated_time",
    # Needed to reach what the ad SAYS. The Graph API returns only the fields
    # named, so leaving this off makes every ad look like it has no creative.
    "creative",
)


def find(
    client: MutatingMetaAdsClient,
    *,
    ad_id: str | None = None,
    name: str | None = None,
    ad_set_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one ad by id or name. Raises if not exactly one."""
    if (ad_id is None) == (name is None):
        raise MutationError("Give exactly one of ad_id or name.")

    if ad_id is not None:
        try:
            return client.node(str(ad_id), FIELDS)
        except Exception as exc:
            raise NotFound(
                f"No ad {ad_id} this token can see. On Meta a missing object "
                f"and a missing permission are the same error."
            ) from exc

    rows = search(client, None, ad_set_id=ad_set_id, account_id=account_id)
    return match_by_name(rows, name or "", entity=ENTITY, to_candidate=as_candidate)


def as_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        id=str(row.get("id")),
        name=row.get("name") or "(unnamed)",
        status=row.get("status") or "",
        context=f"ad set {row.get('adset_id')}",
    )


def search(
    client: MutatingMetaAdsClient,
    needle: str | None = None,
    *,
    ad_set_id: str | None = None,
    campaign_id: str | None = None,
    account_id: str | None = None,
    limit: int = 200,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Ads in the account, one campaign or one ad set, narrowed by name.

    Ad names are the least unique things in a Meta account -- a bulk duplicate
    leaves a dozen ads called "Creative A - Copy" across different ad sets --
    so scoping to a parent is usually necessary rather than merely tidy.
    """
    from ..account_id import account_path

    if ad_set_id:
        parent = str(ad_set_id)
    elif campaign_id:
        parent = str(campaign_id)
    else:
        parent = account_path(client.resolve_account_id(account_id))

    rows = client.read.edge(
        parent,
        EDGE,
        FIELDS,
        limit=limit,
        # `limit` here is what the CALLER wants back, so it is the
        # intentional cap too. Passing it only as the page size walks the
        # whole account and trips the runaway guard on a large one.
        max_items=limit,
        max_pages=max_pages,
        effective_status=(
            '["ACTIVE","PAUSED","CAMPAIGN_PAUSED","ADSET_PAUSED",'
            '"PENDING_REVIEW","DISAPPROVED","WITH_ISSUES"]'
        ),
    )
    if needle:
        target = needle.casefold()
        rows = [r for r in rows if target in (r.get("name") or "").casefold()]
    return rows


def plan_set_status(
    client: MutatingMetaAdsClient,
    *,
    status: str,
    ad_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """Pause or activate one ad."""
    wanted = check_status(status)
    row = find(client, ad_id=ad_id, name=name, account_id=account_id)
    before = row.get("status") or ""

    if before == wanted:
        raise MutationError(
            f"Ad {row.get('name')!r} is already {wanted}; nothing to do."
        )

    warnings = []
    effective = row.get("effective_status")
    if wanted == "ACTIVE" and effective in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"):
        warnings.append(
            f"effective_status is {effective}, so a parent is paused and this "
            f"ad will not deliver until that is activated too"
        )
    elif wanted == "ACTIVE" and effective == "DISAPPROVED":
        warnings.append(
            "this ad was DISAPPROVED in review. Activating it does not appeal "
            "the decision and it will not deliver"
        )
    elif wanted == "ACTIVE":
        warnings.append("activating resumes delivery immediately")

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="ad",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"status": wanted},
        changes=[FieldChange("status", before, wanted)],
        warnings=warnings,
    )


def plan_rename(
    client: MutatingMetaAdsClient,
    *,
    new_name: str,
    ad_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    row = find(client, ad_id=ad_id, name=name, account_id=account_id)
    if row.get("name") == new_name:
        raise MutationError(f"Ad is already named {new_name!r}.")
    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="ad",
        node_id=str(row["id"]),
        label=f"id {row['id']}",
        payload={"name": new_name},
        changes=[FieldChange("name", row.get("name"), new_name)],
    )


# ---------------------------------------------------------------------------
# Creative: duplicating an ad with new copy, and repointing a live one
# ---------------------------------------------------------------------------
#
# A Meta creative is IMMUTABLE once attached, so there is no "edit the
# headline" call. Both plans below build a NEW creative and differ only in what
# they point at it:
#
#   plan_duplicate_ad   a new ad, PAUSED, beside the original. Nothing live
#                       changes. The original keeps its history and its social
#                       proof.
#   plan_update_copy    the SAME ad, repointed. Keeps the ad id and its
#                       reporting line, and usually discards the likes,
#                       comments and shares on the old post.
#
# Separate functions rather than one with a mode flag, because the risk differs
# and the MCP layer turns each into its own tool name -- which is what a
# permission rule keys on.

from . import creatives as _creatives  # noqa: E402
from . import media as _media  # noqa: E402
from .plan import PlannedChange as _PlannedChange  # noqa: E402

CREATIVE_EDGE = "adcreatives"


def _creative_of(client: MutatingMetaAdsClient, ad: dict[str, Any]):
    """The creative behind an ad, read and classified."""
    creative_id = (ad.get("creative") or {}).get("id")
    if not creative_id:
        raise MutationError(
            f"Ad {ad.get('id')} has no creative to read. A dynamic or catalog "
            f"ad may not expose one."
        )
    return _creatives.read(client, creative_id)


def _resolve_media(
    client: MutatingMetaAdsClient,
    *,
    image_path: str | None,
    video_id: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Check the media a caller wants to use. Uploads nothing.

    Returns (what to splice into the creative, notes for the preview). The
    image is only INSPECTED here -- the upload is a write and belongs at apply
    time, so a preview describes the file rather than inventing a hash for it.
    """
    if image_path and video_id:
        raise MutationError("Give an image or a video, not both.")
    if image_path:
        image = _media.inspect_image(image_path)
        return {"_pending_image": image}, image.warnings
    if video_id:
        video = _media.check_video_usable(client, video_id)
        return {"video_id": str(video_id)}, [
            f"reusing existing video {video_id}"
            + (f" ({video.get('title')})" if video.get("title") else "")
        ]
    return {}, []


def _creative_plan(
    creative,
    edits: dict[str, Any],
    media_spec: dict[str, Any],
    *,
    name: str,
) -> _PlannedChange:
    """The create-a-creative half, shared by both paths."""
    # allow_empty: a media-only swap changes nothing textual and is still a
    # real change. The two plan_* functions above already refuse a call that
    # changes neither text nor media.
    payload = _creatives.plan_payload(creative, edits, name=name, allow_empty=True)

    # Media is spliced into the shape the creative already has, so a video ad
    # keeps being a video ad and an image ad an image ad.
    if media_spec:
        payload = _splice_media(creative, payload, media_spec)

    return _PlannedChange(
        action="create",
        entity="creative",
        entity_type="adcreative",
        node_id="",
        label=name,
        payload=payload,
        edge=CREATIVE_EDGE,
    )


def _splice_media(creative, payload: dict[str, Any], media_spec: dict[str, Any]):
    """Put a new image or video into the creative payload, by shape."""
    pending = media_spec.get("_pending_image")
    video_id = media_spec.get("video_id")

    if creative.shape == _creatives.ASSET_FEED:
        feed = dict(payload.get("asset_feed_spec") or {})
        if pending is not None:
            existing = feed.get("images") or [{}]
            # The hash is filled in at apply time; adlabels are preserved so
            # the customization rules that reference them keep working.
            feed["images"] = [
                {**dict(entry), "hash": "<pending upload>"} for entry in existing
            ]
        if video_id:
            feed["videos"] = [{"video_id": video_id}]
            feed.pop("images", None)
        payload["asset_feed_spec"] = feed
        return payload

    spec = dict(payload.get("object_story_spec") or {})
    block = dict(spec.get(creative.shape) or {})
    if pending is not None:
        block["image_hash"] = "<pending upload>"
    if video_id:
        if creative.shape != _creatives.VIDEO:
            raise MutationError(
                f"This is a {creative.shape} creative; giving it a video_id "
                f"would change what kind of ad it is. Duplicate a video ad "
                f"instead, or build the new ad in Ads Manager."
            )
        block["video_id"] = video_id
    spec[creative.shape] = block
    payload["object_story_spec"] = spec
    return payload


def _fill_uploaded_image(payload: dict[str, Any], image_hash: str) -> dict[str, Any]:
    """Replace the placeholder with the hash the upload returned."""
    import json as _json

    text = _json.dumps(payload)
    return _json.loads(text.replace("<pending upload>", image_hash))


def plan_duplicate_ad(
    client: MutatingMetaAdsClient,
    *,
    ad_id: str,
    new_name: str | None = None,
    edits: dict[str, Any] | None = None,
    image_path: str | None = None,
    video_id: str | None = None,
    account_id: str | None = None,
) -> _PlannedChange:
    """A NEW ad beside an existing one, with edited copy and/or new media.

    Always PAUSED. An ad created ACTIVE begins spending the moment it is
    approved, before anyone has looked at it, so going live stays a separate
    deliberate act.

    Nothing about the original changes -- it keeps delivering, keeps its
    history and keeps its social proof. That is what makes this the safe half
    of the pair.
    """
    source = find(client, ad_id=ad_id, account_id=account_id)
    creative = _creative_of(client, source)
    edits = edits or {}
    media_spec, media_notes = _resolve_media(
        client, image_path=image_path, video_id=video_id
    )
    if not edits and not media_spec:
        raise MutationError(
            "Nothing to change. Give at least one of primary_text, headline, "
            "description, link, image_path or video_id -- a duplicate with no "
            "edits is just a second identical ad."
        )

    name = new_name or f"{source.get('name')} (copy)"
    creative_plan = _creative_plan(
        creative, edits, media_spec, name=f"{name} creative"
    )

    before = creative.current_text()
    changes = [
        FieldChange(label, before.get(label), value) for label, value in edits.items()
    ]
    if media_spec:
        changes.append(
            FieldChange(
                "media",
                "existing",
                media_spec.get("video_id") or f"upload {Path(image_path).name}",
            )
        )

    warnings = list(media_notes)
    warnings.append("the new ad is created PAUSED; activating it is a separate step")
    warnings.append(
        f"the original ad {source.get('id')} is untouched and keeps delivering"
    )
    if creative.shape == _creatives.ASSET_FEED:
        warnings.append(
            f"this is a multi-locale creative ({len(before.get('headline') or [])} "
            f"locales); a single value is applied to all of them"
        )

    return _PlannedChange(
        action="create",
        entity=ENTITY,
        entity_type="ad",
        node_id="",
        label=name,
        payload={
            "name": name,
            "adset_id": str(source.get("adset_id")),
            "status": "PAUSED",
        },
        changes=changes,
        warnings=warnings,
        edge=EDGE,
        requires=creative_plan,
    )


def plan_update_copy(
    client: MutatingMetaAdsClient,
    *,
    ad_id: str,
    edits: dict[str, Any] | None = None,
    image_path: str | None = None,
    video_id: str | None = None,
    account_id: str | None = None,
) -> _PlannedChange:
    """Repoint an EXISTING ad at a new creative. Keeps the ad id.

    The cost is social proof. Unless the ad runs on an existing page post, a
    new creative means a new post, and the likes, comments and shares
    accumulated on the old one do not come with it. The plan carries the
    current counts so the trade is made against a number rather than a feeling.
    """
    source = find(client, ad_id=ad_id, account_id=account_id)
    creative = _creative_of(client, source)
    edits = edits or {}
    media_spec, media_notes = _resolve_media(
        client, image_path=image_path, video_id=video_id
    )
    if not edits and not media_spec:
        raise MutationError(
            "Nothing to change. Give at least one of primary_text, headline, "
            "description, link, image_path or video_id."
        )

    creative_plan = _creative_plan(
        creative, edits, media_spec, name=f"{source.get('name')} creative (updated)"
    )

    before = creative.current_text()
    changes = [
        FieldChange(label, before.get(label), value) for label, value in edits.items()
    ]
    if media_spec:
        changes.append(
            FieldChange(
                "media",
                "existing",
                media_spec.get("video_id") or f"upload {Path(image_path).name}",
            )
        )

    warnings = list(media_notes)
    proof = _creatives.social_proof(client, creative)
    engagement = sum(
        int(proof.get(k) or 0) for k in ("likes", "comments", "shares")
    )
    if engagement:
        warnings.append(
            f"THIS DISCARDS SOCIAL PROOF: the current post has "
            f"{proof.get('likes') or 0} likes, {proof.get('comments') or 0} "
            f"comments and {proof.get('shares') or 0} shares. A new creative "
            f"means a new post, and none of it carries over. Duplicating the "
            f"ad instead keeps it."
        )
    if (source.get("effective_status") or source.get("status")) == "ACTIVE":
        warnings.append(
            "this ad is live; the new copy goes into review and delivery can "
            "pause until it is approved"
        )

    return _PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="ad",
        node_id=str(source["id"]),
        label=f"{source.get('name')} (id {source['id']})",
        payload={},  # `creative` is spliced in by execute_chain once it exists
        changes=changes,
        warnings=warnings,
        requires=creative_plan,
    )
