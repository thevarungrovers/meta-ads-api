"""Creating audiences. Never deleting one: the undo is not using it."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from metaads_mcp.changes.base import APPLY_REVERSIBLE, NOTHING, ChangeKind, Proposal
from metaads_reporting.write import audiences


class CreateAudience(ChangeKind):
    name = "create_audience"
    preview_title = "Preview creating an audience"
    apply_title = "Create the audience the preview described"
    apply_annotations = APPLY_REVERSIBLE
    revertible = False

    def _plan(self, ctx, payload: dict[str, Any]):
        kind = payload["kind"]
        if kind == "saved":
            return audiences.plan_create_saved(
                ctx.client, name=payload["name"], targeting=payload.get("targeting") or {}
            )
        if kind == "lookalike":
            if not payload.get("origin_audience_id") or not payload.get("country"):
                raise ValueError(
                    "A lookalike needs origin_audience_id and country. Use "
                    "list_audiences to find a source."
                )
            return audiences.plan_create_lookalike(
                ctx.client,
                name=payload["name"],
                origin_audience_id=payload["origin_audience_id"],
                country=payload["country"],
                ratio=payload["ratio"],
            )
        raise ValueError(
            f"kind must be 'saved' or 'lookalike', got {kind!r}. A "
            f"customer-list audience is not available here: it would "
            f"upload personal data to Meta."
        )

    def propose(
        self,
        ctx,
        kind: Annotated[str, Field(description="'saved' or 'lookalike'")],
        name: Annotated[str, Field(description="Name for the new audience")],
        targeting: Annotated[
            dict[str, Any] | None,
            Field(description="For 'saved': Meta's targeting spec"),
        ] = None,
        origin_audience_id: Annotated[
            str | None, Field(description="For 'lookalike': the source, from list_audiences")
        ] = None,
        country: Annotated[
            str | None, Field(description="For 'lookalike': two-letter code, e.g. CA")
        ] = None,
        ratio: Annotated[
            float, Field(description="For 'lookalike': 0.01 to 0.20", ge=0.01, le=0.20)
        ] = 0.01,
    ) -> Proposal:
        """Show a saved targeting spec or a lookalike. Writes nothing.

        **Customer-list audiences are not available here and never will be.**
        Building one uploads hashed customer emails and phone numbers to Meta;
        that is personal data leaving the company, and it belongs in Ads
        Manager where a person accepts the data terms. The attempt is refused
        structurally, not just undocumented.

        A lookalike is derived from a source Meta already holds, so nothing is
        uploaded. An audience is never deleted here: an unused one simply sits
        there, and revert_change cannot remove it.
        """
        payload = {
            "kind": (kind or "").strip().lower(),
            "name": name,
            "targeting": targeting,
            "origin_audience_id": origin_audience_id,
            "country": country,
            "ratio": ratio,
        }
        plan = self._plan(ctx, payload)
        return Proposal(
            entity_type=plan.entity,
            entity_id="(new)",
            entity_name=name,
            path=f"account {ctx.default_account}",
            field_name="audience",
            before=NOTHING,
            after=" | ".join(f"{c.field}: {c.after}" for c in plan.changes),
            checks=[],
            projected=Decimal("0"),
            payload=payload,
            warnings=list(plan.warnings),
            note=(
                "Creating an audience changes no delivery on its own -- it is "
                "available for an ad set to use. Projected delta is 0."
            ),
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return NOTHING

    def build(self, ctx, payload: dict[str, Any]):
        return self._plan(ctx, payload)

    def inverse(self, ctx, entry: dict[str, Any]):
        raise ValueError(
            f"Entry {entry['entry_id']} created an audience, and audiences are "
            "never deleted here (remove no). An audience no ad set uses changes "
            "nothing; if one of yours is attached, detach it from the ad set. "
            "list_audiences shows which audiences exist."
        )
