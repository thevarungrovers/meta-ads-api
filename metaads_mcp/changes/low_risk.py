"""The general low-risk tool: names and labels, on a closed allowlist.

One preview/apply pair for the edits that cannot move money, delivery or
targeting -- renaming a campaign, ad set or ad, and creating, attaching or
detaching ad labels. The allowlist is a fixed set in code, pinned by a test
that also checks no allowlisted field is a money field or carries a forbidden
name (budget, bid, status, targeting ...). Anything else goes through a typed
kind with guardrails. ``url_tags`` is NOT here: it lives on the immutable
creative, so it is a new creative -- see preview_url_tags.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from metaads_mcp.changes.base import APPLY_REVERSIBLE, NOTHING, Proposal
from metaads_mcp.changes.common import NodeFieldKind, as_obj, canonical, previous_values
from metaads_reporting.account_id import account_path
from metaads_reporting.write.plan import FieldChange, PlannedChange

#: (entity_type, field) -> the Graph field it writes. THE allowlist.
ALLOWED: dict[tuple[str, str], str] = {
    ("campaign", "name"): "name",
    ("ad_set", "name"): "name",
    ("ad", "name"): "name",
    ("campaign", "label_attach"): "adlabels",
    ("ad_set", "label_attach"): "adlabels",
    ("ad", "label_attach"): "adlabels",
    ("campaign", "label_detach"): "adlabels",
    ("ad_set", "label_detach"): "adlabels",
    ("ad", "label_detach"): "adlabels",
    ("account", "label_create"): "name",
}

GRAPH_TYPE = {"campaign": "campaign", "ad_set": "adset", "ad": "ad"}
READABLE = {"campaign": "campaign", "ad_set": "ad set", "ad": "ad"}


def _labels(row: dict[str, Any]) -> list[dict[str, str]]:
    return sorted(({"id": str(l.get("id"))} for l in as_obj(row.get("adlabels")) or []
                   if isinstance(l, dict)), key=lambda l: l["id"])


class LowRiskChange(NodeFieldKind):
    name = "low_risk_change"
    preview_title = "Preview a low-risk edit: a name or an ad label"
    apply_title = "Apply a low-risk edit (name or label)"
    apply_annotations = APPLY_REVERSIBLE
    read_fields = ("name", "adlabels", "account_id", "status", "effective_status")

    def read_typed(self, ctx, entity_type: str, entity_id: str) -> dict[str, Any]:
        # A campaign has no campaign_id field, and asking Meta for a field a
        # node does not have fails the whole read.
        extra = () if entity_type == "campaign" else ("campaign_id",)
        return ctx.client.node(str(entity_id), ("id", *self.read_fields, *extra))
    ledger_keys = (("campaign", "name"), ("ad set", "name"), ("ad", "name"))

    def render(self, ctx, row):
        field = getattr(self, "_field", None)
        return row.get("name") or "" if field == "name" else canonical(_labels(row))

    def _render_for(self, field: str, row: dict[str, Any]) -> str:
        return (row.get("name") or "") if field == "name" else canonical(_labels(row))

    def current(self, ctx, payload):
        if payload["field"] == "label_create":
            return NOTHING
        return self._render_for(payload["graph_field"], self.read_row(ctx, payload["entity_id"]))

    def read_row(self, ctx, entity_id):
        return ctx.client.node(str(entity_id), ("id", *self.read_fields))

    def current_for_entry(self, ctx, entry):
        inverse = entry.get("inverse") or {}
        if inverse.get("field") == "label_create":
            return None
        return self._render_for(inverse.get("graph_field", "name"), self.read_row(ctx, entry["entity_id"]))

    def build(self, ctx, payload):
        if payload["field"] == "label_create":
            return PlannedChange(action="create", entity="ad label", entity_type="adlabel",
                                 node_id="", label=payload["value"], payload={"name": payload["value"]},
                                 changes=[FieldChange("label", None, payload["value"])], edge="adlabels")
        plan = self._plan(payload["entity_id"], payload.get("name", ""), payload["fields"],
                          payload["before"], payload["after"])
        plan.entity_type = payload["graph_type"]
        return plan

    def inverse(self, ctx, entry):
        inverse = entry.get("inverse") or {}
        if inverse.get("field") == "label_create":
            raise ValueError("A created label is harmless and is not deleted; detach it instead.")
        plan = super().inverse(ctx, entry)
        plan.entity_type = inverse["graph_type"]
        # A revert restores the previous name VERBATIM, never through naming:
        # it may predate the prefix.
        plan.allow_unprefixed_name = True
        return plan

    def revert_projection(self, ctx, entry):
        return Decimal("0"), False

    # Meta documents label REMOVAL as DELETE /{object}/adlabels, and the write
    # client has no DELETE (nothing here deletes). A detach -- and the revert
    # of an attach -- POSTs the remaining set instead, on the reading that Meta
    # replaces it. Rather than add a DELETE path, every label write re-reads
    # the labels and fails when the set is not exactly the one approved: a
    # label still attached is reported, never recorded as detached.

    def _labels_landed(self, ctx, entity_id: str, expected: str) -> str | None:
        live = _labels(self.read_row(ctx, entity_id))
        if canonical(live) == expected:
            return None
        wanted = {l["id"] for l in as_obj(expected) or []}
        still = sorted(l["id"] for l in live if l["id"] not in wanted)
        missing = sorted(wanted - {l["id"] for l in live})
        parts = []
        if still:
            parts.append(f"label(s) {', '.join(still)} still attached")
        if missing:
            parts.append(f"label(s) {', '.join(missing)} not attached")
        return "; ".join(parts) or f"labels are {canonical(live)}, not {expected}"

    def landed(self, ctx, payload):
        if payload.get("graph_field") != "adlabels":
            return None
        return self._labels_landed(ctx, payload["entity_id"], payload["after"])

    def revert_landed(self, ctx, entry):
        if (entry.get("inverse") or {}).get("graph_field") != "adlabels":
            return None
        return self._labels_landed(ctx, entry["entity_id"], entry["before"])

    def propose(
        self,
        ctx,
        entity_type: Annotated[Literal["campaign", "ad_set", "ad", "account"], Field(description="What to edit")],
        entity_id: Annotated[str, Field(description="Its id (for account: ignored, the default account)")],
        field: Annotated[
            Literal["name", "label_attach", "label_detach", "label_create"],
            Field(description="name: rename. label_attach/detach: an ad label id from list_labels. "
                              "label_create (entity_type=account): a new label's name"),
        ],
        value: Annotated[str, Field(description="The new name, the label id, or the new label's name")],
    ) -> Proposal:
        """Rename something, or create, attach or detach an ad label. Writes nothing.

        The only edits allowed here are on a fixed list in code: names and
        labels, which move no money, delivery or targeting. Everything else is
        a typed preview_* tool with guardrails. Counts one apply; revert puts the
        old name or label set back (a created label is not deleted).

        A rename is allowed only on the agent's OWN entities (its owner/agent
        prefix, or the ledger shows this server created it); a manual entity
        or another agent's is refused. Give the base name: the prefix is kept.
        A new label is named with the prefix too.
        """
        key = (entity_type, field)
        if key not in ALLOWED:
            raise ValueError(
                f"{field} on {entity_type} is not on the low-risk allowlist: "
                f"{sorted(ALLOWED)}. Use the typed preview_* tool for it."
            )
        value = (value or "").strip()
        if not value:
            raise ValueError("value is empty.")
        if field == "label_create":
            # Labels are prefixed like everything else this server creates.
            decision = ctx.name_for_create("label", value)
            value = decision.final
            proposal = Proposal(
                entity_type="ad label", entity_id="(new)", entity_name=value,
                path=f"account {ctx.default_account}", field_name="label_create",
                before=NOTHING, after=f"label {value!r}", checks=[], projected=Decimal("0"),
                payload={"field": field, "value": value, "graph_field": "name"},
            ).with_inverse({"field": field})
            proposal.name(decision)
            return proposal
        graph_field = ALLOWED[key]
        row = self.read_typed(ctx, entity_type, entity_id)
        decision = None
        if graph_field == "name":
            # Only the agent's OWN entities: its prefix, or the ledger shows
            # this server created it. The prefix is kept (or restored); the
            # length limit counts it.
            from metaads_mcp import ledger

            decision = ctx.name_for_rename(
                READABLE[entity_type], row.get("name") or "", value,
                ledger_owned=ledger.created_by_us(str(row["id"])))
            value = decision.final
            fields = {"name": value}
            after_row = {**row, "name": value}
        else:
            labels = _labels(row)
            ids = {l["id"] for l in labels}
            if field == "label_attach":
                ctx.read.node(value, ("id", "name"))  # must exist
                if value in ids:
                    raise ValueError(f"Label {value} is already attached.")
                labels = sorted(labels + [{"id": value}], key=lambda l: l["id"])
            else:
                if value not in ids:
                    raise ValueError(f"Label {value} is not attached.")
                labels = [l for l in labels if l["id"] != value]
            fields = {"adlabels": labels}
            after_row = {**row, "adlabels": labels}
        before = self._render_for(graph_field, row)
        after = self._render_for(graph_field, after_row)
        if before == after and not (decision and decision.refused):
            raise ValueError("Nothing would change.")
        previous = previous_values(row, fields) or {graph_field: [] if graph_field == "adlabels" else row.get("name")}
        proposal = Proposal(
            entity_type=READABLE[entity_type], entity_id=str(row["id"]),
            entity_name=row.get("name") or "", path=row.get("name") or "",
            field_name=field, before=before, after=after, checks=[], projected=Decimal("0"),
            payload={"entity_id": str(row["id"]), "name": row.get("name") or "", "fields": fields,
                     "field": field, "graph_field": graph_field, "graph_type": GRAPH_TYPE[entity_type]},
            status=row.get("status"), effective_status=row.get("effective_status"),
            campaign_id=str(row.get("campaign_id") or row["id"]) if entity_type != "campaign" else str(row["id"]),
            account_id=row.get("account_id"),
            note="A name or a label moves no money and no delivery.",
        ).with_inverse({"previous": previous, "field": field, "graph_field": graph_field,
                        "graph_type": GRAPH_TYPE[entity_type]})
        if decision is not None:
            proposal.name(decision)
        return proposal


__all__ = ["ALLOWED", "LowRiskChange", "account_path"]
