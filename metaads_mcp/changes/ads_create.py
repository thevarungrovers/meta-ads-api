"""Creating ads and creatives, and pausing or activating several ads at once.

Everything created here is created PAUSED (an ad) or unattached (a creative),
so creation itself spends nothing; going live is a separate status change the
human approves. A creation costs 3 applies against the session and daily caps,
so an agent cannot build out an account in a loop.

Undoing a create is PAUSING what was created -- never deleting it. An
unattached creative cannot be paused and is not deleted either: it costs
nothing and shows nowhere.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.base import APPLY_SPENDS, NOTHING, ChangeKind, LedgerItem, Proposal
from metaads_mcp.changes.common import ad_activation_bound, wanted_status
from metaads_mcp.changes.context import PENDING_UPLOAD
from metaads_mcp.changes.delivery import AdStatus
from metaads_mcp.guardrails import Check
from metaads_reporting.account_id import account_path
from metaads_reporting.write import ads, adsets, media
from metaads_reporting.write.plan import FieldChange, PlannedChange

CallToAction = Literal[
    "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "SUBSCRIBE", "ORDER_NOW", "GET_OFFER",
    "BOOK_TRAVEL", "CONTACT_US", "DOWNLOAD", "APPLY_NOW", "GET_QUOTE", "SEE_MENU",
    "WATCH_MORE", "MESSAGE_PAGE", "NO_BUTTON",
]

#: A creation costs this many applies against the session and daily caps.
CREATE_WEIGHT = 3


class CreateAd(ChangeKind):
    name = "create_ad"
    adds_no_spend = True
    preview_title = "Preview creating a PAUSED ad from an existing creative"
    apply_title = "Create the PAUSED ad the preview described"
    apply_annotations = APPLY_SPENDS
    weight = CREATE_WEIGHT
    verifies_ad = True
    creates_named = ("ad",)

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="The ad set it goes in, from find_ad_sets")],
        creative_id: Annotated[str, Field(description="An existing creative (create_creative, or get_ad's)")],
        name: Annotated[str, Field(description="BASE name for the new ad; the owner/agent prefix is added")],
    ) -> Proposal:
        """Show a new ad, created PAUSED, using a creative that already exists.

        The cheapest create: nothing is uploaded and nothing goes live. The
        preview fetches the creative's previews on the ad set's placements and
        runs the ad checks. Activating it is a separate preview_ad_status.
        """
        from metaads_mcp import verify

        adset = adsets.find(ctx.client, ad_set_id=ad_set_id)
        # What the human approves is the creative AS PREVIEWED: kept in the
        # payload, so the after-apply diff compares the live ad against it
        # rather than against a fresh read of the same creative.
        approved = verify.approved_from_spec(ctx.client.node(str(creative_id), verify.CREATIVE_FIELDS))
        decision = ctx.name_for_create("ad", name)
        name = decision.final
        proposal = Proposal(
            entity_type="ad", entity_id="(new)", entity_name=name,
            path=f"ad set {adset.get('id')} ({adset.get('name')}) > {name}",
            field_name="created", before=NOTHING,
            after=f"PAUSED ad using creative {creative_id}",
            checks=[], projected=Decimal("0"),
            payload={"adset_id": str(adset["id"]), "creative_id": str(creative_id), "name": name,
                     "approved": approved},
            campaign_id=str(adset.get("campaign_id")), account_id=adset.get("account_id"),
            warnings=["the ad is created PAUSED; activating it is a separate, previewed step"] + (
                [] if ctx.client.node(str(creative_id), ("url_tags",)).get("url_tags") else
                [f"creative {creative_id} has no url_tags: clicks from this ad carry no URL "
                 "parameters (preview_url_tags can add them)"]),
            note="A PAUSED ad spends nothing; projected 0.",
            verify={"adset_id": str(adset["id"]), "creative_id": str(creative_id)},
        )
        proposal.name(decision)
        return proposal

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        return PlannedChange(
            action="create", entity="ad", entity_type="ad", node_id="", label=payload["name"],
            payload={"name": payload["name"], "adset_id": payload["adset_id"],
                     "creative": {"creative_id": payload["creative_id"]}, "status": "PAUSED"},
            changes=[FieldChange("ad", None, payload["name"])], edge="ads",
        )

    def approved_for(self, ctx, payload, plan):
        return payload["approved"]

    def verify_target(self, payload, result, created):
        return result.node_id or None

    def inverse_for(self, payload, result, created):
        return {"created_ad_id": result.node_id}

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    def revert_reduces_spend(self, ctx, entry):
        # The undo pauses the created ad: it can only lower spend.
        return True

    def inverse(self, ctx, entry):
        created = (entry.get("inverse") or {}).get("created_ad_id") or entry.get("entity_id")
        return ads.plan_set_status(ctx.client, ad_id=str(created), status="PAUSED")


def default_identity(ctx) -> tuple[str | None, str | None, str | None]:
    """The Page, Instagram account and url_tags most of the account's creatives use."""
    rows = ctx.read.edge(account_path(ctx.read.resolve_account_id()), "adcreatives",
                         ("id", "object_story_spec", "url_tags"), limit=25, max_items=25)
    pages, igs, tags = Counter(), Counter(), Counter()
    for row in rows:
        spec = row.get("object_story_spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        if spec.get("page_id"):
            pages[str(spec["page_id"])] += 1
        ig = spec.get("instagram_user_id") or spec.get("instagram_actor_id")
        if ig:
            igs[str(ig)] += 1
        if row.get("url_tags"):
            tags[row["url_tags"]] += 1
    return tuple(c.most_common(1)[0][0] if c else None for c in (pages, igs, tags))


def campaign_url_tags(ctx, campaign_id: str) -> str | None:
    """The url_tags most of a campaign's ads carry, or None.

    Read per campaign because utm_campaign usually names the campaign: the
    account's most common tags would send this ad's clicks to another
    campaign's line in analytics.
    """
    rows = ctx.read.edge(str(campaign_id), "ads", ("id", "creative{url_tags}"),
                         limit=25, max_items=25)
    tags = Counter((r.get("creative") or {}).get("url_tags") for r in rows)
    tags.pop(None, None)
    return tags.most_common(1)[0][0] if tags else None


def resolve_url_tags(ctx, url_tags: str | None, campaign_id: str | None,
                     account_default: str | None) -> tuple[str | None, list[str]]:
    """The url_tags a new creative gets, and what the human should be told.

    None means "the default": the campaign's own, else the account's, each
    said out loud. An empty string is an explicit "no URL parameters".
    """
    from metaads_mcp.changes.creative_options import check_url_tags

    if url_tags is not None:
        if not url_tags.strip():
            return None, ["url_tags left empty on purpose: clicks from this ad carry no URL parameters"]
        return check_url_tags(url_tags), []
    if campaign_id:
        tags = campaign_url_tags(ctx, campaign_id)
        if tags:
            return tags, [f"url_tags copied from the ads in campaign {campaign_id}: {tags}"]
    if account_default:
        why = (f"no ad in campaign {campaign_id} carries any" if campaign_id
               else "no ad_set_id was given, so the campaign is unknown")
        return account_default, [
            f"url_tags copied from the account's most common ({why}): {account_default} -- "
            "check that utm_campaign names the right campaign, or pass url_tags"]
    return None, ["no url_tags: none given and none on the account's creatives, so clicks "
                  "from this ad carry no URL parameters"]


class CreateCreative(ChangeKind):
    name = "create_creative"
    adds_no_spend = True
    preview_title = "Preview creating a new ad creative (unattached)"
    apply_title = "Create the creative the preview described"
    apply_annotations = APPLY_SPENDS
    weight = CREATE_WEIGHT
    verifies_ad = True
    revertible = False
    creates_named = ("creative",)

    def _spec(self, ctx, p: dict[str, Any]) -> dict[str, Any]:
        media_count = sum(bool(p.get(k)) for k in ("image_path", "image_hash", "video_id"))
        if media_count != 1:
            raise ValueError("Give exactly one of image_path, image_hash or video_id.")
        if not p.get("link"):
            raise ValueError("A link ad needs a destination link.")
        cta: dict[str, Any] = {"type": p.get("call_to_action") or "LEARN_MORE"}
        if p.get("video_id"):
            media.check_video_usable(ctx.client, p["video_id"])
            cta["value"] = {"link": p["link"]}
            block = {"video_id": p["video_id"], "message": p.get("primary_text") or "",
                     "title": p.get("headline") or "", "link_description": p.get("description") or "",
                     "call_to_action": cta}
            if p.get("thumbnail_hash"):
                block["image_hash"] = p["thumbnail_hash"]
            story_key = "video_data"
        else:
            block = {"link": p["link"], "message": p.get("primary_text") or "",
                     "name": p.get("headline") or "", "description": p.get("description") or "",
                     "call_to_action": cta,
                     "image_hash": p.get("image_hash") or PENDING_UPLOAD}
            story_key = "link_data"
        block = {k: v for k, v in block.items() if v not in ("", None)}
        story: dict[str, Any] = {"page_id": p["page_id"], story_key: block}
        if p.get("instagram_user_id"):
            story["instagram_user_id"] = p["instagram_user_id"]
        spec: dict[str, Any] = {"name": p["name"], "object_story_spec": story}
        if p.get("url_tags"):
            spec["url_tags"] = p["url_tags"]
        return spec

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="BASE name for the creative (internal; the owner/agent prefix is added). The headline is NOT this")],
        link: Annotated[str, Field(description="Destination URL")],
        primary_text: Annotated[str | None, Field(description="Body text")] = None,
        headline: Annotated[str | None, Field(description="Headline")] = None,
        description: Annotated[str | None, Field(description="Description")] = None,
        call_to_action: Annotated[CallToAction, Field(description="The button")] = "LEARN_MORE",
        image_path: Annotated[str | None, Field(description="Local image, uploaded at APPLY")] = None,
        image_hash: Annotated[str | None, Field(description="Or an image already in the library (list_ad_images)")] = None,
        video_id: Annotated[str | None, Field(description="Or an existing, ready video (list_ad_videos)")] = None,
        thumbnail_hash: Annotated[str | None, Field(description="video: a thumbnail image hash")] = None,
        url_tags: Annotated[str | None, Field(description="UTM parameters, key=value&...; default: the ones this campaign's ads use (pass ad_set_id), else the account's most common; \"\" for none")] = None,
        page_id: Annotated[str | None, Field(description="Page to publish as; default: the one most creatives use")] = None,
        instagram_user_id: Annotated[str | None, Field(description="Instagram account; default: the one most creatives use")] = None,
        ad_set_id: Annotated[str | None, Field(description="Preview on this ad set's placements (optional)")] = None,
    ) -> Proposal:
        """Show a new single-image or video creative. Writes nothing.

        Created UNATTACHED: it spends nothing and shows nowhere until
        preview_create_ad (or preview_replace_ad_creative) uses it. The image
        is uploaded only at apply. Page and Instagram identity default to the
        ones the account's existing creatives use. Previews and the ad checks
        (truncation, image shape, video length, CTA, destination) run here.
        """
        from metaads_mcp.changes.creative_options import name_macro_warnings

        if image_path:
            media.inspect_image(image_path)
        # Only the creative's top-level, internal `name` is stamped; the
        # headline viewers see (link_data.name) is the agent's text, untouched.
        decision = ctx.name_for_create("creative", name)
        name = decision.final
        default_page, default_ig, default_tags = default_identity(ctx)
        campaign_id = None
        if ad_set_id:
            campaign_id = str(adsets.find(ctx.client, ad_set_id=ad_set_id).get("campaign_id"))
        url_tags, tag_warnings = resolve_url_tags(ctx, url_tags, campaign_id, default_tags)
        payload = {
            "name": name, "link": link, "primary_text": primary_text, "headline": headline,
            "description": description, "call_to_action": call_to_action,
            "image_path": image_path, "image_hash": image_hash, "video_id": video_id,
            "thumbnail_hash": thumbnail_hash, "url_tags": url_tags,
            "page_id": page_id or default_page,
            "instagram_user_id": instagram_user_id or default_ig, "adset_id": ad_set_id,
        }
        if not payload["page_id"]:
            raise ValueError("No page_id given and none found on the account's creatives; "
                             "pass one from list_connected_identities.")
        spec = self._spec(ctx, payload)
        proposal = Proposal(
            entity_type="creative", entity_id="(new)", entity_name=name,
            path=f"account {ctx.default_account}", field_name="created",
            before=NOTHING,
            after=" | ".join(f"{k}: {v!r}" for k, v in (
                ("primary_text", primary_text), ("headline", headline),
                ("description", description), ("link", link), ("cta", call_to_action),
                ("media", image_path or image_hash or video_id), ("page", payload["page_id"]),
                ("instagram", payload["instagram_user_id"]), ("url_tags", url_tags)) if v),
            checks=[], projected=Decimal("0"), payload=payload, campaign_id=campaign_id,
            warnings=["created unattached: nothing changes until an ad uses it", *tag_warnings],
            verify={"adset_id": ad_set_id, "spec": spec, "image_path": image_path,
                    "video_id": video_id},
        )
        proposal.name(decision)
        if url_tags:
            proposal.warnings.extend(name_macro_warnings(url_tags))
        return proposal

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        spec = self._spec(ctx, payload)
        return PlannedChange(action="create", entity="creative", entity_type="adcreative",
                             node_id="", label=payload["name"], payload=spec,
                             changes=[FieldChange("creative", None, payload["name"])],
                             edge=ads.CREATIVE_EDGE)

    def prepare_apply(self, ctx, plan, payload):
        if payload.get("image_path"):
            image = media.inspect_image(payload["image_path"])
            image_hash = media.upload_image(ctx.client, image, apply=True)
            plan.payload = ads._fill_uploaded_image(plan.payload, image_hash)
        return plan

    def approved_for(self, ctx, payload, plan):
        from metaads_mcp import verify

        return verify.approved_from_spec(plan.payload)

    def verify_creative_target(self, payload, result, created):
        return result.node_id or None

    def inverse(self, ctx, entry):
        raise ValueError(
            f"Entry {entry['entry_id']} created a creative. An unattached creative "
            "cannot be paused, costs nothing and shows nowhere, and nothing is "
            "deleted here. If an ad uses it, pause that ad."
        )


class MultiAdStatus(AdStatus):
    name = "multi_ad_status"
    preview_title = "Preview pausing or activating several ads in one ad set"
    apply_title = "Apply a status change to several ads (AFFECTS DELIVERY)"
    apply_annotations = APPLY_SPENDS
    ledger_keys = ()

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="The ad set they are in")],
        ad_ids: Annotated[list[str], Field(description="Ads in that ad set, from find_ads")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> Proposal:
        """Pause or activate several ads in ONE ad set as a batch. Writes nothing.

        Capped by max_entities_per_call. Each ad is its own write and its own
        ledger row, sharing a batch_id: revert_change takes the batch_id to
        undo them all, or one entry_id to undo one. Meta's batch is not atomic,
        and neither is this: a failure part-way says which ads changed.
        """
        wanted = wanted_status(new_status)
        if not ad_ids:
            raise ValueError("Give at least one ad id.")
        rows = []
        for ad_id in dict.fromkeys(str(a) for a in ad_ids):
            row = ads.find(ctx.client, ad_id=ad_id)
            if str(row.get("adset_id")) != str(ad_set_id):
                raise ValueError(f"Ad {ad_id} is in ad set {row.get('adset_id')}, not {ad_set_id}.")
            rows.append(row)
        changing = [r for r in rows if (r.get("status") or "") != wanted]
        if not changing:
            raise ValueError(f"Every one of those ads is already {wanted}.")
        projected, note = Decimal("0"), ""
        if wanted == "ACTIVE":
            siblings = [
                a for a in ads.search(ctx.client, None, ad_set_id=str(ad_set_id), limit=200)
                if (a.get("status") or "") == "ACTIVE" and str(a.get("id")) not in {str(r["id"]) for r in rows}
            ]
            if not siblings:
                projected, note = ad_activation_bound(ctx, {**changing[0], "id": "(batch)"}, ctx.currency_of(changing[0]))
        items = [LedgerItem(entity_type="ad", entity_id=str(r["id"]), entity_name=r.get("name") or "",
                            field_name="status", before=r.get("status") or "", after=wanted)
                 for r in changing]
        first = changing[0]
        return Proposal(
            entity_type="ad", entity_id=str(first["id"]),
            entity_name=f"{len(changing)} ads in ad set {ad_set_id}",
            path=f"ad set {ad_set_id}", field_name="status",
            before=" | ".join(f"{r['id']}: {r.get('status')}" for r in changing),
            after=wanted, checks=[], projected=projected,
            payload={"ad_set_id": str(ad_set_id), "ad_ids": [str(r["id"]) for r in changing],
                     "statuses": {str(r["id"]): r.get("status") or "" for r in changing}},
            items=items, batch=True, campaign_id=str(first.get("campaign_id")),
            account_id=first.get("account_id"),
            is_activation=wanted == "ACTIVE" and projected > 0,
            warnings=([f"projected delta: {note}"] if note else []) + (
                [f"left out, already {wanted}: {', '.join(str(r['id']) for r in rows if r not in changing)}"]
                if len(changing) < len(rows) else []
            ),
        )

    def current(self, ctx, payload):
        statuses = {a: ads.find(ctx.client, ad_id=a).get("status") or "" for a in payload["ad_ids"]}
        return " | ".join(f"{a}: {statuses[a]}" for a in payload["ad_ids"])

    def build(self, ctx, payload):
        return [ads.plan_set_status(ctx.client, ad_id=a, status=payload["after"])
                for a in payload["ad_ids"]]


ADS_KINDS = (CreateAd, CreateCreative, MultiAdStatus)
