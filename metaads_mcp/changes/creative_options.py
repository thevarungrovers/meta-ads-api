"""Placements, and the creative options that live on an immutable creative.

``ad_set_placements`` edits the ad set's targeting (where its ads run) and
previews every ad in the ad set on the NEW placement set before approval, so a
square image landing in Reels is seen before it happens.

``url_tags``, ``advantage_creative_options`` and ``attach_lead_form`` all
change something that lives on the CREATIVE, which Meta never edits in place:
each builds a new creative from the ad's current one with that one thing
changed, and repoints the ad at it -- the same path, the same ad verification
and the same revert (back to the previous creative) as update_ad_copy.
``replace_ad_creative`` points an ad at a creative that already exists.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.base import APPLY_SPENDS, Proposal
from metaads_mcp.changes.common import NodeFieldKind, as_obj, canonical
from metaads_mcp.changes.creative import UpdateAdCopy, creative_id_of, describe
from metaads_mcp.changes.targeting import fingerprint, placement_widening, with_dimensions
from metaads_mcp.guardrails import Check
from metaads_reporting.write import ads, creatives
from metaads_reporting.write.plan import FieldChange, PlannedChange

POSITION_KEYS = ("facebook_positions", "instagram_positions", "messenger_positions",
                 "audience_network_positions")
PLACEMENT_KEYS = ("publisher_platforms", *POSITION_KEYS)
#: Ads previewed per ad set on a placement change.
MAX_ADS_PREVIEWED = 3

AdvantageFeature = Literal[
    "image_touchups", "image_brightness_and_contrast", "image_enhancement",
    "image_uncrop", "image_templates", "text_optimizations", "text_generation",
    "enhance_cta", "description_automation", "music", "3d_animation",
    "video_auto_crop", "adapt_to_placement", "inline_comment",
    "site_extensions", "product_extensions",
]

_TAG = re.compile(r"^[A-Za-z0-9_.\-~%]+=([^&=?#\s]|\{\{[a-z_.]+\}\})*$")


def check_url_tags(raw: str) -> str:
    """A query string with no leading '?': key=value pairs, {{macros}} allowed.

    >>> check_url_tags("utm_source=meta&utm_campaign={{campaign.name}}")
    'utm_source=meta&utm_campaign={{campaign.name}}'
    """
    text = (raw or "").strip()
    if text.startswith("?"):
        raise ValueError("url_tags has no leading '?': give key=value&key=value.")
    if not text:
        raise ValueError("url_tags is empty.")
    for pair in text.split("&"):
        if not _TAG.match(pair):
            raise ValueError(f"{pair!r} is not a key=value pair (macros like {{{{campaign.name}}}} are fine).")
    return text


def _placements(targeting: dict[str, Any]) -> dict[str, Any]:
    return {k: targeting[k] for k in PLACEMENT_KEYS if targeting.get(k)}


class AdSetPlacements(NodeFieldKind):
    name = "ad_set_placements"
    preview_title = "Preview where an ad set's ads run (placements)"
    apply_title = "Apply an ad set placement change (CHANGES WHERE ADS RUN)"
    apply_annotations = APPLY_SPENDS
    graph_type = "adset"
    entity_type = "ad set"
    read_fields = ("status", "effective_status", "campaign_id", "account_id", "targeting")
    ledger_keys = (("ad set", "placements"),)

    def render(self, ctx, row):
        targeting = as_obj(row.get("targeting")) or {}
        placed = _placements(targeting)
        where = canonical(placed) if placed else "Advantage+ placements (automatic)"
        return f"{where} || {fingerprint(targeting)}"

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        automatic: Annotated[bool, Field(description="True: Advantage+ placements (Meta chooses everywhere)")] = False,
        publisher_platforms: Annotated[
            list[Literal["facebook", "instagram", "messenger", "audience_network"]] | None,
            Field(description="Manual placements: the platforms"),
        ] = None,
        facebook_positions: Annotated[list[str] | None, Field(description="e.g. feed, story, facebook_reels, marketplace")] = None,
        instagram_positions: Annotated[list[str] | None, Field(description="e.g. stream, story, reels, explore")] = None,
        messenger_positions: Annotated[list[str] | None, Field(description="e.g. messenger_home, story")] = None,
        audience_network_positions: Annotated[list[str] | None, Field(description="e.g. classic")] = None,
    ) -> Proposal:
        """Change where an ad set's ads appear. Writes nothing.

        `automatic=True` is Advantage+ placements: Meta places ads everywhere
        it predicts they perform. Otherwise give publisher_platforms and,
        optionally, the positions on each. The preview fetches preview links
        for every ad in the ad set (up to three) on the NEW placement set and
        checks each ad's image shape against it -- a square image in Stories or
        Reels is cropped -- so look at them before approving.
        """
        from metaads_mcp import verify
        from metaads_reporting.placement_specs import formats_for_targeting

        row = self.read_row(ctx, ad_set_id)
        targeting = as_obj(row.get("targeting")) or {}
        new = {k: v for k, v in targeting.items() if k not in PLACEMENT_KEYS}
        if not automatic:
            if not publisher_platforms:
                raise ValueError("Give publisher_platforms, or automatic=True for Advantage+ placements.")
            new["publisher_platforms"] = list(publisher_platforms)
            for key, value in (("facebook_positions", facebook_positions),
                               ("instagram_positions", instagram_positions),
                               ("messenger_positions", messenger_positions),
                               ("audience_network_positions", audience_network_positions)):
                if value:
                    platform = key.removesuffix("_positions")
                    if platform not in publisher_platforms:
                        raise ValueError(f"{key} given but {platform} is not in publisher_platforms.")
                    new[key] = list(value)
        before = self.render(ctx, row)
        after = self.render(ctx, {**row, "targeting": new})
        if before == after:
            raise ValueError("The placements are already exactly that.")

        formats = formats_for_targeting(new)[: ctx.limits.max_preview_formats]
        previews: list[dict[str, Any]] = []
        checks: list[Check] = []
        ad_rows = ads.search(ctx.client, None, ad_set_id=str(row["id"]), limit=MAX_ADS_PREVIEWED)
        for ad in ad_rows[:MAX_ADS_PREVIEWED]:
            cid = creative_id_of(ad)
            for entry in verify.fetch_previews(ctx, formats, creative_id=cid):
                previews.append({"ad_id": str(ad.get("id")), **entry})
            if cid:
                try:
                    spec = ctx.read.node(cid, verify.CREATIVE_FIELDS)
                    dims, source = verify._image_dimensions(ctx, verify.approved_from_spec(spec), None)
                    for check in verify.ratio_checks(dims, formats, source):
                        check.name = f"{check.name}[ad {ad.get('id')}]"
                        checks.append(check)
                except Exception:  # noqa: BLE001 - previews are context
                    pass
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="placements",
            before=before.split(" || ")[0],
            after=after.split(" || ")[0],
            checks=checks,
            projected=Decimal("0"),
            payload={"entity_id": str(row["id"]), "name": row.get("name") or "",
                     "fields": {"targeting": new}, "full_before": before, "full_after": after},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=["the ad set re-enters learning when its placements change"],
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            previews=previews,
            # Narrowing placements can under-deliver; undoing it can spend more.
            tightens=bool(placement_widening(new, targeting)),
            note="Placements move where the budget is spent, not how much; projected 0.",
        ).with_inverse({"previous": {"targeting": targeting}})

    def current(self, ctx, payload):
        # The before/after shown is the placement half, but staleness checks
        # the WHOLE spec, since the whole spec is what gets sent: a targeting
        # edit made elsewhere since the preview would otherwise be overwritten.
        full = self.render(ctx, self.read_row(ctx, payload["entity_id"]))
        if full == payload["full_before"]:
            return payload["before"]
        short = full.split(" || ")[0]
        return short if short != payload["before"] else f"{short} (other targeting changed)"

    def build(self, ctx, payload):
        return self._plan(payload["entity_id"], payload.get("name", ""), payload["fields"],
                          payload["full_before"], payload["full_after"])

    def current_for_entry(self, ctx, entry):
        return self.render(ctx, self.read_row(ctx, entry["entity_id"])).split(" || ")[0]

    def inverse(self, ctx, entry):
        # Only the placement keys go back, onto the spec as it is NOW: the
        # revert's staleness check compares the placements alone, so sending
        # the whole previous spec would undo any targeting edit made since.
        previous = as_obj(((entry.get("inverse") or {}).get("previous") or {}).get("targeting"))
        if previous is None:
            return super().inverse(ctx, entry)
        now = as_obj(self.read_row(ctx, entry["entity_id"]).get("targeting")) or {}
        spec = with_dimensions(now, PLACEMENT_KEYS, _placements(previous))
        return self._plan(entry["entity_id"], entry.get("entity_name") or "",
                          {"targeting": spec}, entry["after"], entry["before"])


# ---------------------------------------------------------------------------
# A new creative with one thing changed, repointed
# ---------------------------------------------------------------------------


class _Repoint(UpdateAdCopy):
    """Copy the ad's creative, change one thing, point the ad at the copy."""

    verifies_ad = True
    apply_annotations = APPLY_SPENDS
    field_label = ""

    def transform(self, ctx, creative, payload: dict[str, Any], spec: dict[str, Any]) -> tuple[Any, Any]:
        """Edit ``spec`` in place; return (before, after) of the one thing changed."""
        raise NotImplementedError

    def extra_checks(self, ctx, row, creative, payload) -> list[Check]:
        return []

    def _plan(self, ctx, payload):
        source = ads.find(ctx.client, ad_id=payload["ad_id"])
        creative = ads._creative_of(ctx.client, source)
        spec = creatives.plan_payload(creative, {}, name=f"{source.get('name')} creative ({self.name})",
                                      allow_empty=True)
        before, after = self.transform(ctx, creative, payload, spec)
        if canonical(before) == canonical(after):
            raise ValueError(f"The ad's {self.field_label} is already {canonical(after)}.")
        requires = PlannedChange(action="create", entity="creative", entity_type="adcreative",
                                 node_id="", label=spec["name"], payload=spec, edge=ads.CREATIVE_EDGE)
        return PlannedChange(
            action="update", entity="ad", entity_type="ad", node_id=str(source["id"]),
            label=f"{source.get('name')} (id {source['id']})", payload={},
            changes=[FieldChange("creative", creative.id, "a new copy"),
                     FieldChange(self.field_label, canonical(before), canonical(after))],
            warnings=[
                "a new creative means a new post: likes and comments on the old one do not carry over",
                "a live ad re-enters review with the new creative",
            ],
            requires=requires,
        )

    def _propose(self, ctx, ad_id: str, **payload) -> Proposal:
        payload = {"ad_id": ad_id, **payload}
        plan = self._plan(ctx, payload)
        row = ads.find(ctx.client, ad_id=ad_id)
        creative = ads._creative_of(ctx.client, row)
        proposal = self._preview(
            ctx, plan, row, projected=Decimal("0"),
            note="The budget does not move; what changes is the creative the ad shows.",
            payload_extra={k: v for k, v in payload.items() if k != "ad_id"},
            inverse={"previous_creative_id": creative_id_of(row)},
        )
        proposal.field_name = self.field_label
        proposal.checks.extend(self.extra_checks(ctx, row, creative, payload))
        return proposal


class UrlTags(_Repoint):
    name = "url_tags"
    preview_title = "Preview setting an ad's URL parameters (UTM tags)"
    apply_title = "Apply new URL parameters (REPLACES THE CREATIVE)"
    field_label = "url_tags"

    def transform(self, ctx, creative, payload, spec):
        before = creative.raw.get("url_tags") or None
        spec["url_tags"] = payload["url_tags"]
        return before, payload["url_tags"]

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="From find_ads")],
        url_tags: Annotated[str, Field(description="e.g. utm_source=meta&utm_medium=paid&utm_campaign={{campaign.name}}")],
    ) -> Proposal:
        """Set or replace the URL parameters (UTM tags) on an ad's links.

        Writes nothing. url_tags live on the immutable creative, so this builds
        a new creative with the tags and repoints the ad (revert points it back).
        The preview checks the destination WITH the tags appended.
        """
        return self._propose(ctx, ad_id, url_tags=check_url_tags(url_tags))


class AdvantageCreativeOptions(_Repoint):
    name = "advantage_creative_options"
    preview_title = "Preview turning Advantage+ creative enhancements on or off"
    apply_title = "Apply Advantage+ creative options (REPLACES THE CREATIVE)"
    field_label = "advantage_creative_features"

    def transform(self, ctx, creative, payload, spec):
        freedom = dict(spec.get("degrees_of_freedom_spec") or {})
        features = dict(freedom.get("creative_features_spec") or {})
        before = {k: (v or {}).get("enroll_status") for k, v in sorted(features.items())}
        for feature in payload.get("opt_in") or []:
            features[feature] = {"enroll_status": "OPT_IN"}
        for feature in payload.get("opt_out") or []:
            features[feature] = {"enroll_status": "OPT_OUT"}
        freedom["creative_features_spec"] = features
        spec["degrees_of_freedom_spec"] = freedom
        after = {k: (v or {}).get("enroll_status") for k, v in sorted(features.items())}
        return before, after

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="From find_ads")],
        opt_in: Annotated[list[AdvantageFeature] | None, Field(description="Enhancements to turn ON")] = None,
        opt_out: Annotated[list[AdvantageFeature] | None, Field(description="Enhancements to turn OFF")] = None,
    ) -> Proposal:
        """Turn individual Advantage+ creative enhancements on or off for an ad.

        Writes nothing. The preview lists every feature's state before and
        after: a creative built without the spec silently opts back IN to
        everything, so the existing opt-outs are carried across. New creative,
        repointed; revert points the ad back.
        """
        if not opt_in and not opt_out:
            raise ValueError("Give opt_in and/or opt_out.")
        clash = set(opt_in or []) & set(opt_out or [])
        if clash:
            raise ValueError(f"{', '.join(sorted(clash))} cannot be both on and off.")
        return self._propose(ctx, ad_id, opt_in=list(opt_in or []), opt_out=list(opt_out or []))


class AttachLeadForm(_Repoint):
    name = "attach_lead_form"
    preview_title = "Preview attaching an existing lead form to an ad"
    apply_title = "Apply the lead form to the ad (REPLACES THE CREATIVE)"
    field_label = "lead_gen_form_id"

    def transform(self, ctx, creative, payload, spec):
        story = spec.get("object_story_spec") or {}
        block_key = "link_data" if "link_data" in story else "video_data" if "video_data" in story else None
        if block_key is None:
            raise ValueError("This creative has no link or video block to carry a lead form.")
        block = dict(story[block_key])
        cta = dict(block.get("call_to_action") or {"type": "SIGN_UP"})
        value = dict(cta.get("value") or {})
        before = value.get("lead_gen_form_id")
        value["lead_gen_form_id"] = str(payload["form_id"])
        cta["value"] = value
        block["call_to_action"] = cta
        story = dict(story)
        story[block_key] = block
        spec["object_story_spec"] = story
        return before, str(payload["form_id"])

    def extra_checks(self, ctx, row, creative, payload):
        checks = []
        form = ctx.read.node(str(payload["form_id"]), ("id", "name", "status", "page"))
        status = form.get("status")
        checks.append(Check("lead_form_active", status == "ACTIVE",
                            f"form {form.get('name')!r} is {status}"))
        form_page = str((form.get("page") or {}).get("id") or "")
        creative_page = str(creative.page_id or "")
        checks.append(Check("lead_form_page", bool(form_page) and form_page == creative_page,
                            f"form's Page {form_page or '?'} against the creative's Page {creative_page or '?'}"))
        adset = ctx.read.node(str(row.get("adset_id")), ("id", "destination_type", "promoted_object"))
        on_ad = adset.get("destination_type") == "ON_AD"
        checks.append(Check("ad_set_instant_form", on_ad,
                            f"ad set destination_type is {adset.get('destination_type')} (needs ON_AD)"))
        promoted_page = str(((as_obj(adset.get("promoted_object")) or {}).get("page_id")) or "")
        if promoted_page:
            checks.append(Check("promoted_page", promoted_page == form_page,
                                f"ad set promotes Page {promoted_page}, form is on {form_page}"))
        return checks

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="From find_ads")],
        form_id: Annotated[str, Field(description="An EXISTING form, from list_lead_forms")],
    ) -> Proposal:
        """Point an ad at an existing instant (lead) form. Writes nothing.

        Refuses a form that is not ACTIVE, on a different Page from the
        creative, or an ad set that is not an instant-form (ON_AD) ad set.
        Creating a form stays in Ads Manager. New creative, repointed; revert
        points the ad back.
        """
        return self._propose(ctx, ad_id, form_id=str(form_id))


class ReplaceAdCreative(UpdateAdCopy):
    name = "replace_ad_creative"
    preview_title = "Preview pointing an ad at an existing creative"
    apply_title = "Apply the creative swap (REPLACES THE CREATIVE)"
    apply_annotations = APPLY_SPENDS
    verifies_ad = True

    def _plan(self, ctx, payload):
        source = ads.find(ctx.client, ad_id=payload["ad_id"])
        current = creative_id_of(source)
        target = str(payload["creative_id"])
        if current == target:
            raise ValueError(f"The ad already uses creative {target}.")
        ctx.client.node(target, ("id",))  # must exist and be visible
        return PlannedChange(
            action="update", entity="ad", entity_type="ad", node_id=str(source["id"]),
            label=f"{source.get('name')} (id {source['id']})",
            payload={"creative": {"creative_id": target}},
            changes=[FieldChange("creative", current, target)],
            warnings=["a live ad re-enters review with the new creative"],
        )

    def prepare_apply(self, ctx, plan, payload):
        return plan

    def approved_for(self, ctx, payload, plan):
        from metaads_mcp import verify

        return verify.approved_from_spec(ctx.read.node(str(payload["creative_id"]), verify.CREATIVE_FIELDS))

    def inverse_for(self, payload, result, created):
        return {"new_creative_id": str(payload["creative_id"])}

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="From find_ads")],
        creative_id: Annotated[str, Field(description="An existing creative, e.g. from preview/apply_create_creative")],
    ) -> Proposal:
        """Point an existing ad at a creative that already exists. Writes nothing.

        The preview fetches that creative's previews on the ad set's placements
        and runs the ad checks. Revert points the ad back at its previous creative.
        """
        payload = {"ad_id": ad_id, "creative_id": str(creative_id)}
        plan = self._plan(ctx, payload)
        row = ads.find(ctx.client, ad_id=ad_id)
        before, after = describe(plan)
        return Proposal(
            entity_type="ad", entity_id=str(row["id"]), entity_name=row.get("name") or "",
            path=f"ad set {row.get('adset_id')} > {row.get('name')}", field_name="creative",
            before=before, after=after, checks=[], projected=Decimal("0"),
            payload={"ad_id": str(row["id"]), "creative_id": str(creative_id)},
            status=row.get("status"), effective_status=row.get("effective_status"),
            warnings=list(plan.warnings), campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            verify={"adset_id": str(row.get("adset_id")), "creative_id": str(creative_id)},
        ).with_inverse({"previous_creative_id": creative_id_of(row)})


OPTION_KINDS = (AdSetPlacements, UrlTags, AdvantageCreativeOptions, AttachLeadForm, ReplaceAdCreative)
