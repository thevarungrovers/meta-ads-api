"""Changing what an ad says: a new creative, on a new ad or the same one.

A Meta creative is IMMUTABLE once attached, so neither kind edits one. Both
build a NEW creative and differ in what they point at it, which is why they are
separate tool names rather than one tool with a mode flag: the risk differs, so
the permission prompt should too.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from metaads_mcp.changes.base import APPLY_REVERSIBLE, APPLY_SPENDS, ChangeKind, Proposal
from metaads_reporting.write import ads, media
from metaads_reporting.write.plan import FieldChange, PlannedChange


def copy_edits(primary_text, headline, description, link) -> dict[str, Any]:
    edits = {
        "primary_text": primary_text,
        "headline": headline,
        "description": description,
        "link": link,
    }
    return {k: v for k, v in edits.items() if v is not None}


def describe(plan) -> tuple[str, str]:
    """before/after strings for a creative plan, the staleness key too."""
    before = " | ".join(
        f"{c.field}: {c.before!r}" for c in plan.changes if c.before is not None
    ) or "(see metrics)"
    after = " | ".join(f"{c.field}: {c.after!r}" for c in plan.changes)
    return before, after


def creative_id_of(row: dict[str, Any]) -> str | None:
    creative = row.get("creative")
    if isinstance(creative, str):
        import json

        try:
            creative = json.loads(creative)
        except ValueError:
            return creative
    if isinstance(creative, dict):
        return str(creative.get("id") or creative.get("creative_id") or "") or None
    return None


class _CreativeKind(ChangeKind):
    """Shared: the plan is rebuilt at apply time and compared, field by field."""

    #: Every creative change is verified: previews and checks before, a field
    #: diff and the review state after. Declared here, enforced by the engine.
    verifies_ad = True

    def _plan(self, ctx, payload: dict[str, Any]):  # pragma: no cover
        raise NotImplementedError

    def current(self, ctx, payload: dict[str, Any]) -> str:
        # Rebuilt from the account as it is NOW: if the source ad's copy moved
        # since the preview, the before-string moves with it and the engine
        # refuses the apply as stale -- the check the hand-written creative
        # path used to skip.
        return describe(self._plan(ctx, payload))[0]

    def build(self, ctx, payload: dict[str, Any]):
        return self._plan(ctx, payload)

    def prepare_apply(self, ctx, plan, payload: dict[str, Any]):
        # The image upload happens here, at apply time, because it is a write.
        # The preview described the FILE rather than inventing a hash for it.
        image_path = payload.get("image_path")
        if image_path:
            image = media.inspect_image(image_path)
            image_hash = media.upload_image(ctx.client, image, apply=True)
            plan.requires.payload = ads._fill_uploaded_image(plan.requires.payload, image_hash)
        return plan

    def approved_for(self, ctx, payload, plan) -> dict[str, Any]:
        from metaads_mcp import verify

        return verify.approved_from_spec(plan.requires.payload if plan.requires else {})

    def _preview(self, ctx, plan, row, *, projected, note, payload_extra, inverse) -> Proposal:
        before, after = describe(plan)
        return Proposal(
            entity_type="ad",
            entity_id=str(row.get("id")),
            entity_name=row.get("name") or "",
            path=f"ad set {row.get('adset_id')} > {row.get('name')}",
            field_name="creative",
            before=before,
            after=after,
            checks=[],
            projected=projected,
            payload={"ad_id": str(row["id"]), **payload_extra},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            metrics=ctx.metrics_7d(str(row["id"]), "ad"),
            warnings=list(plan.warnings),
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            note=note,
            verify={
                "adset_id": str(row.get("adset_id")),
                "spec": plan.requires.payload if plan.requires else None,
                "image_path": payload_extra.get("image_path"),
                "video_id": payload_extra.get("video_id"),
            },
        ).with_inverse(inverse)


class DuplicateAd(_CreativeKind):
    name = "duplicate_ad"
    adds_no_spend = True
    preview_title = "Preview duplicating an ad with new copy or media"
    apply_title = "Create the duplicate ad the preview described"
    apply_annotations = APPLY_REVERSIBLE
    creates_named = ("ad", "creative")

    def names(self, ctx, source_name: str, new_name: str | None):
        """(the ad's name decision, its creative's): ours, on new_name or the
        source's base with " (copy)"; the creative is "<ad name> creative"."""
        if new_name:
            ad = ctx.name_for_create("ad", new_name)
        else:
            ad = ctx.name_for_copy("ad", source_name, " (copy)")
        return ad, ctx.name_derived("creative", f"{ad.final} creative")

    def _plan(self, ctx, payload):
        names = payload.get("names") or {}
        return ads.plan_duplicate_ad(
            ctx.client,
            ad_id=payload["ad_id"],
            new_name=names.get("ad") or payload.get("new_name"),
            creative_name=names.get("creative"),
            edits=payload.get("edits") or {},
            image_path=payload.get("image_path"),
            video_id=payload.get("video_id"),
        )

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="The ad to copy, from find_ads")],
        primary_text: Annotated[
            str | None, Field(description="New body text (Ads Manager: primary text)")
        ] = None,
        headline: Annotated[str | None, Field(description="New headline")] = None,
        description: Annotated[str | None, Field(description="New description")] = None,
        link: Annotated[str | None, Field(description="New destination URL")] = None,
        image_path: Annotated[
            str | None, Field(description="Local image file to upload and use")
        ] = None,
        video_id: Annotated[
            str | None, Field(description="An EXISTING Meta video id to use")
        ] = None,
        new_name: Annotated[str | None, Field(description="BASE name for the new ad (the owner/agent prefix is added); default: the source's base + ' (copy)'")] = None,
    ) -> Proposal:
        """Show a NEW ad built beside an existing one, with changed copy.

        The safe half of the pair. The original is untouched -- it keeps
        delivering, keeps its history and keeps its likes and comments. The new
        ad is created PAUSED, so going live is a separate deliberate step.

        On a multi-locale creative a single value is applied to every locale.
        Pass nothing for a field to leave it as it is.
        """
        row = ads.find(ctx.client, ad_id=ad_id)
        ad_name, creative_name = self.names(ctx, row.get("name") or "", new_name)
        extra = {
            "edits": copy_edits(primary_text, headline, description, link),
            "image_path": image_path,
            "video_id": video_id,
            "new_name": new_name,
            "names": {"ad": ad_name.final, "creative": creative_name.final},
        }
        plan = self._plan(ctx, {"ad_id": ad_id, **extra})
        proposal = self._preview(
            ctx,
            plan,
            row,
            # A new PAUSED ad cannot spend: it is not delivering, and its ad
            # set's budget is unchanged either way.
            projected=Decimal("0"),
            note=(
                "The new ad is PAUSED, so the projected delta is 0 and nothing "
                "changes until somebody activates it. The original ad is not "
                "modified."
            ),
            payload_extra=extra,
            inverse={"source_ad_id": str(row["id"])},
        )
        proposal.name(ad_name)
        proposal.name(creative_name)
        return proposal

    def verify_target(self, payload, result, created) -> str | None:
        return result.node_id or None

    def inverse_for(self, payload, result, created) -> dict[str, Any]:
        return {
            "created_ad_id": result.node_id,
            "creative_id": created[0] if created else None,
        }

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    def revert_reduces_spend(self, ctx, entry):
        # The undo pauses (or ends) what was created: it can only lower spend.
        return True

    def inverse(self, ctx, entry: dict[str, Any]):
        created = (entry.get("inverse") or {}).get("created_ad_id")
        if not created:
            raise ValueError(
                f"Entry {entry['entry_id']} recorded no created ad id, so it "
                "cannot be traced to exactly one ad. Pausing nothing."
            )
        # Undoing a create is PAUSING what was created -- never deleting it.
        return ads.plan_set_status(ctx.client, ad_id=created, status="PAUSED")


class UpdateAdCopy(_CreativeKind):
    name = "update_ad_copy"
    preview_title = "Preview changing a live ad's copy or media"
    apply_title = "Apply the ad copy change (REPLACES THE CREATIVE)"
    apply_annotations = APPLY_SPENDS
    creates_named = ("creative",)
    #: What the new creative's name adds to the ad's base.
    creative_suffix = " creative (updated)"

    def creative_name(self, ctx, ad_name: str):
        """The new creative: our prefix on the AD's base. The ad itself keeps
        its name -- a copy edit is not a rename."""
        return ctx.name_for_copy("creative", ad_name or "", self.creative_suffix)

    def _plan(self, ctx, payload):
        return ads.plan_update_copy(
            ctx.client,
            ad_id=payload["ad_id"],
            edits=payload.get("edits") or {},
            image_path=payload.get("image_path"),
            video_id=payload.get("video_id"),
            creative_name=payload.get("creative_name"),
        )

    def propose(
        self,
        ctx,
        ad_id: Annotated[str, Field(description="The ad to change, from find_ads")],
        primary_text: Annotated[str | None, Field(description="New body text")] = None,
        headline: Annotated[str | None, Field(description="New headline")] = None,
        description: Annotated[str | None, Field(description="New description")] = None,
        link: Annotated[str | None, Field(description="New destination URL")] = None,
        image_path: Annotated[
            str | None, Field(description="Local image file to upload and use")
        ] = None,
        video_id: Annotated[
            str | None, Field(description="An EXISTING Meta video id to use")
        ] = None,
    ) -> Proposal:
        """Show repointing an EXISTING ad at a new creative. Keeps the ad id.

        **This usually discards social proof.** A new creative means a new
        post, so the likes, comments and shares on the current one do not carry
        over -- the preview reports the exact counts at stake. Prefer
        preview_duplicate_ad when that engagement matters.

        A live ad also re-enters review, and delivery can pause until the new
        copy is approved. revert_change points the ad back at its previous
        creative.
        """
        row = ads.find(ctx.client, ad_id=ad_id)
        naming = self.creative_name(ctx, row.get("name") or "")
        extra = {
            "edits": copy_edits(primary_text, headline, description, link),
            "image_path": image_path,
            "video_id": video_id,
            "creative_name": naming.final,
        }
        plan = self._plan(ctx, {"ad_id": ad_id, **extra})
        proposal = self._preview(
            ctx,
            plan,
            row,
            projected=Decimal("0"),
            note=(
                "The ad set's budget does not move, so the projected delta is "
                "0. What changes is what the ad says -- and, usually, the "
                "social proof it had accumulated."
            ),
            payload_extra=extra,
            inverse={"previous_creative_id": creative_id_of(row)},
        )
        proposal.name(naming)
        return proposal

    def verify_target(self, payload, result, created) -> str | None:
        return payload["ad_id"]

    def inverse_for(self, payload, result, created) -> dict[str, Any]:
        return {"new_creative_id": created[0] if created else None}

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        # Stale when the ad no longer points at the creative this change made:
        # someone repointed it since, and putting ours back would undo theirs.
        new = (entry.get("inverse") or {}).get("new_creative_id")
        if not new:
            return None
        live = creative_id_of(ads.find(ctx.client, ad_id=entry["entity_id"]))
        return entry["after"] if live == new else f"creative {live}"

    def revert_row_inverse(self, ctx, entry: dict[str, Any]) -> dict[str, Any] | None:
        # The revert points the ad back at the previous creative, replacing
        # the new one: a revert of THAT points it at the new one again.
        inverse = entry.get("inverse") or {}
        if not (inverse.get("previous_creative_id") and inverse.get("new_creative_id")):
            return None
        return {"previous_creative_id": inverse["new_creative_id"],
                "new_creative_id": inverse["previous_creative_id"]}

    def inverse(self, ctx, entry: dict[str, Any]):
        previous = (entry.get("inverse") or {}).get("previous_creative_id")
        if not previous:
            raise ValueError(
                f"Entry {entry['entry_id']} recorded no previous creative id, so "
                "there is nothing to point the ad back at. Use "
                "preview_update_ad_copy with the old copy instead."
            )
        return PlannedChange(
            action="update",
            entity="ad",
            entity_type="ad",
            node_id=str(entry["entity_id"]),
            label=f"{entry['entity_name']} (id {entry['entity_id']})",
            payload={"creative": {"creative_id": str(previous)}},
            changes=[FieldChange("creative", "this change's creative", previous)],
        )
