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

from .client import MutatingMetaAdsClient, MutationResult


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
    """A write that has been built but not sent."""

    action: str
    entity: str
    entity_type: str
    node_id: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    changes: list[FieldChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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
    """Send ``plan``. Validates server-side unless ``apply`` is True."""
    return client.update(
        plan.node_id,
        entity_type=plan.entity_type,
        payload=plan.payload,
        apply=apply,
        account_id=account_id,
    )
