"""Preview tokens: the link between what a human saw and what gets applied.

A ``preview_*`` tool returns a readable before/after plus a token. The matching
``apply_*`` tool takes only that token. Three things follow from that, and all
three are the point:

* an ``apply_*`` cannot be called cold -- there is always a preview directly
  above it in the transcript, which is what the human is actually approving;
* the token is single-use and short-lived, so an approval cannot be replayed;
* the recorded ``before`` is re-checked against the entity's CURRENT value at
  apply time, so a change someone made in Ads Manager in between is caught
  rather than silently overwritten.

That last one is the reason a token carries the before-value at all, and on
Meta it earns its keep: Ads Manager is a browser tab somebody usually has open
while they are talking to an agent about the same account.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

#: Long enough to read a preview and decide; short enough that an approval
#: cannot be reused an hour later against different numbers.
TOKEN_TTL_SECONDS = 600


class ChangePreview(BaseModel):
    """What one change would do. Returned by every preview_* tool."""

    tool: str = Field(description="The apply_* tool this token is for")
    preview_token: str = Field(description="Pass this to the apply_* tool")
    expires_in_seconds: int

    entity_type: str
    entity_id: str
    entity_name: str
    path: str = Field(description="Where the entity sits: campaign > ad set > ad")

    field_name: str
    before: str
    after: str

    account_id: str
    currency: str
    status: str | None = None
    effective_status: str | None = Field(
        default=None,
        description=(
            "What Meta says is ACTUALLY happening, parents and ad review "
            "folded in. An ACTIVE ad under a paused campaign reads "
            "CAMPAIGN_PAUSED here and is not delivering."
        ),
    )
    metrics_7d: dict[str, Any] | None = None

    projected_daily_spend_delta: str = Field(
        description="Upper bound on how much more could be spent per day"
    )
    checks: list[str]
    blocked: bool = Field(description="True when a check failed; apply would refuse")
    warnings: list[str] = []
    note: str | None = None


class ApplyResult(BaseModel):
    """What an apply_* actually did."""

    applied: bool
    entry_id: str = Field(description="Ledger id; pass to revert_change to undo")
    entity_type: str
    entity_id: str
    entity_name: str
    field_name: str
    before: str
    after: str
    session: dict[str, Any] = Field(description="Session budget left after this")


class PreviewExpired(RuntimeError):
    """The token is unknown, used, or past its TTL."""


class PreviewStale(RuntimeError):
    """The entity moved since the preview was taken."""


@dataclass
class _Pending:
    tool: str
    created_at: float
    payload: dict[str, Any]
    projected_delta: Decimal
    blocked: bool

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > TOKEN_TTL_SECONDS

    @property
    def age_seconds(self) -> int:
        return int(time.monotonic() - self.created_at)


class PreviewStore:
    """In-memory, per-process. Tokens do not survive a restart, on purpose."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}

    def mint(
        self,
        *,
        tool: str,
        payload: dict[str, Any],
        projected_delta: Decimal,
        blocked: bool,
    ) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(18)
        self._pending[token] = _Pending(
            tool=tool,
            created_at=time.monotonic(),
            payload=payload,
            projected_delta=projected_delta,
            blocked=blocked,
        )
        return token

    def take(self, token: str, tool: str) -> _Pending:
        """Consume a token. Single-use: taken out of the store before any check.

        Removing it first matters -- a token that failed validation must not be
        retryable, or a blocked change could be re-applied until some unrelated
        thing changed and it slipped through.
        """
        self._evict_expired()
        pending = self._pending.pop(token, None)
        if pending is None:
            raise PreviewExpired(
                "That preview_token is unknown, already used, or older than "
                f"{TOKEN_TTL_SECONDS // 60} minutes. Take a fresh preview and "
                "show it to the human before applying."
            )
        if pending.tool != tool:
            raise PreviewExpired(
                f"That token was minted for {pending.tool}, not {tool}. Tokens "
                "are not interchangeable between tools."
            )
        if pending.blocked:
            raise PreviewExpired(
                "That preview failed a guardrail check, so it cannot be applied. "
                "The preview's `checks` list says which one."
            )
        return pending

    def _evict_expired(self) -> None:
        for token in [t for t, p in self._pending.items() if p.expired]:
            del self._pending[token]

    @property
    def pending_count(self) -> int:
        self._evict_expired()
        return len(self._pending)


def project_budget_change(before: Decimal | None, after: Decimal) -> Decimal:
    """Exact: a daily budget IS the cap, so the delta is the cap moving."""
    if before is None:
        return after
    return after - before


def project_campaign_budget_change(
    before: Decimal | None, after: Decimal, ad_set_count: int
) -> Decimal:
    """The same, but a campaign budget is shared across its ad sets.

    The delta is NOT multiplied by the ad set count -- a campaign budget is one
    pot, and raising it by 50 lets the campaign spend 50 more in total, not 50
    more per ad set. The count is carried into the preview text instead,
    because "this moves spend for 14 ad sets" is the part a human needs, and it
    is not visible in the number.
    """
    return project_budget_change(before, after)


def project_bid_change(
    before: Decimal | None, after: Decimal, clicks_7d: int
) -> Decimal:
    """Upper bound on added daily spend from a bid change.

    Assumes the same click volume at the new bid, which overstates it when the
    bid falls and understates it when a higher bid wins more auctions. It is a
    bound to reason with, not a forecast, and the preview says so.

    On Meta there is a second reason it is only a bound: ``bid_amount`` is a
    CAP or a TARGET, not a price. Raising a cap does not raise what is paid
    unless the auction was already pressing against it.
    """
    if before is None or clicks_7d <= 0:
        return Decimal("0")
    per_day = Decimal(clicks_7d) / Decimal(7)
    return (after - before) * per_day


def project_pause() -> Decimal:
    """Pausing can only reduce spend, so the added-spend delta is zero."""
    return Decimal("0")


def pct_change(before: Decimal | None, after: Decimal) -> str | None:
    if before is None or before == 0:
        return None
    return f"{float((after - before) / before * 100):+.1f}%"
