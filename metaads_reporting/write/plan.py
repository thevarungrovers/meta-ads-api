"""Planned changes: what a write would do, before it does it.

A :class:`PlannedChange` carries both halves of a write -- the payload the API
needs, and a human-readable diff. Keeping them together is what lets the
confirmation prompt describe exactly the request that will be sent, rather than
a separately-written summary that can drift from it.

Nothing here touches the API. :func:`execute` does, and only with
``apply=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dataclasses import replace

from .client import MutatingMetaAdsClient, MutationError, MutationResult


@dataclass(frozen=True)
class FieldChange:
    """One field moving from ``before`` to ``after``."""

    field: str
    before: Any
    after: Any

    def render(self) -> str:
        if self.before is None:
            return f"{self.field}: {self.after!r}"
        return f"{self.field}: {self.before!r} -> {self.after!r}"


@dataclass
class PlannedChange:
    """A write that has been built but not sent.

    Covers both an UPDATE and a CREATE, told apart by ``edge``. A create names
    the edge it posts to (``ads``, ``adcreatives``, ``customaudiences``) and
    has no ``node_id`` yet, because the thing does not exist; an update names
    the node and no edge. :func:`execute` routes on exactly that.

    Keeping them in one type rather than two means the confirmation flow, the
    rendering and the audit trail are shared -- a create is as reviewable as an
    update, which is the point.
    """

    action: str
    entity: str
    entity_type: str
    node_id: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    changes: list[FieldChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Set for a CREATE: the account edge to post to. None means update.
    edge: str | None = None
    #: A create that must happen first, and whose new id this plan depends on.
    #: An ad needs its creative to exist before it can point at one.
    requires: "PlannedChange | None" = None

    @property
    def is_create(self) -> bool:
        return self.edge is not None

    def render(self) -> str:
        lines = [f"{self.action.upper()} {self.entity}: {self.label}"]
        lines.extend(f"    {change.render()}" for change in self.changes)
        lines.extend(f"    ! {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def describe(self) -> dict[str, Any]:
        """A JSON-safe rendering for the audit log."""
        return {
            "action": self.action,
            "entity": self.entity,
            "entity_type": self.entity_type,
            "node_id": self.node_id,
            "label": self.label,
            "changes": [
                {"field": c.field, "before": c.before, "after": c.after}
                for c in self.changes
            ],
        }


def execute(
    client: MutatingMetaAdsClient,
    plan: PlannedChange,
    *,
    apply: bool = False,
    account_id: str | None = None,
) -> MutationResult:
    """Send ``plan``. Validates server-side unless ``apply`` is True.

    Routes on ``plan.edge``: a create posts to an account edge, an update posts
    to a node. A plan with ``requires`` is NOT handled here -- see
    :func:`execute_chain`, because a dependent create needs the first one's id
    and that is a sequence, not a single call.
    """
    if plan.requires is not None:
        raise MutationError(
            f"{plan.label} depends on {plan.requires.label} being created "
            f"first. Use execute_chain, which sequences them and reports what "
            f"landed if the second step fails."
        )
    if plan.is_create:
        return client.create(
            entity_type=plan.entity_type,
            edge=plan.edge,
            payload=plan.payload,
            apply=apply,
            account_id=account_id,
        )
    return client.update(
        plan.node_id,
        entity_type=plan.entity_type,
        payload=plan.payload,
        apply=apply,
        account_id=account_id,
    )


#: Where a dependent create writes its prerequisite's new id. An ad references
#: its creative as {"creative_id": "<id>"}, so the chain has to splice the id in
#: once it exists rather than knowing it up front.
DEPENDENCY_KEY = "creative"


def execute_chain(
    client: MutatingMetaAdsClient,
    plan: PlannedChange,
    *,
    apply: bool = False,
    account_id: str | None = None,
) -> tuple[MutationResult, list[str]]:
    """Run a plan and whatever it depends on, in order.

    Returns the final result and the ids created along the way.

    THE HALF-DONE CASE IS THE WHOLE REASON THIS EXISTS. Creating an ad with new
    copy is two writes: the creative, then the ad that points at it. If the
    second fails, the first has already landed, and the caller needs to know
    that an orphan creative exists rather than assuming nothing happened. The
    created ids come back on the exception for exactly that.

    An orphan creative is harmless -- unattached creatives cost nothing and do
    not appear in Ads Manager's ad list -- which is why this sequences rather
    than attempting a rollback that could fail in turn.
    """
    created: list[str] = []
    if plan.requires is not None:
        first = execute(client, plan.requires, apply=apply, account_id=account_id)
        if apply:
            if not first.node_id:
                raise MutationError(
                    f"{plan.requires.label} was created but Meta returned no "
                    f"id: {first.response!r}. Nothing further was sent."
                )
            created.append(first.node_id)
            # `requires` must be cleared along with splicing the id in. The
            # prerequisite has been done; leaving it set makes execute() refuse
            # the very step this function exists to run.
            plan = replace(
                plan,
                payload={**plan.payload, DEPENDENCY_KEY: {"creative_id": first.node_id}},
                requires=None,
            )
        else:
            # Validate-only: there is no real id to reference, so the second
            # step cannot be validated against it. Say so rather than sending a
            # request that fails for a reason that is an artefact of the dry run.
            return first, created

    try:
        result = execute(client, plan, apply=apply, account_id=account_id)
    except MutationError as exc:
        if created:
            raise MutationError(
                f"{exc}\n\n*** {len(created)} object(s) WERE created before "
                f"this failed: {', '.join(created)}. They are orphaned but "
                f"harmless -- an unattached creative costs nothing and does "
                f"not show in the ad list. Do not re-run blind; check what "
                f"exists first."
            ) from exc
        raise
    return result, created
