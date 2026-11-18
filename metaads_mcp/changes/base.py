"""The shapes every change kind speaks in.

A *kind* is one sort of change -- a campaign's status, an ad set's budget, a
new lookalike. It knows four things and nothing else:

  * how to read the entity and turn the agent's arguments into a
    :class:`Proposal` (before, after, the bounds it has to clear, the projected
    spend, and a JSON-able ``payload`` the apply will need);
  * how to read the CURRENT value again at apply time, so a change somebody
    made in Ads Manager in between is caught rather than overwritten;
  * how to build the :class:`~metaads_reporting.write.plan.PlannedChange`;
  * how to build the change that undoes one ledger entry.

Everything else -- the token, the kill switch, the session and daily caps, the
ledger's two phases, the ``validate_only`` round trip, the ad verification, the
ToolError mapping -- lives once, in :mod:`metaads_mcp.changes.engine`. Before
it, each of the nine original pairs hand-rolled its own copy of the apply
sequence, and the copies had drifted: the creative and audience paths skipped
the staleness check, and the audience path ignored ``result.success``. A new
kind cannot forget a gate it never implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.types import ToolAnnotations

from metaads_mcp.guardrails import Check

# All four hints, always -- see the note in tools_read.py for why an omitted
# hint is worse than a wrong one.
#
# A preview reads the account, mints a token in memory and writes nothing, so it
# is safe to allowlist. Calling one twice just produces a second token. The
# validate_only round trip it makes is a server-side CHECK, not a write: Meta
# evaluates the request and discards it.
PREVIEW_SAFE = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

#: destructive_hint is set only where the change moves money, stops delivery,
#: changes targeting or creates something. Marking a reversible pause
#: destructive would train someone to click through the loud prompts, which is
#: how loud prompts stop working.
APPLY_SPENDS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

#: Pausing one ad, a rename, and reverting: writes, but not new risk.
APPLY_REVERSIBLE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

SETTABLE_STATUSES = ("ACTIVE", "PAUSED")

#: What "before" reads as for something that does not exist yet.
NOTHING = "(does not exist)"


def parse_money(raw: str, name: str) -> Decimal:
    """Money arrives as a decimal string in MAJOR units, never minor ones.

    The argument is rendered verbatim in the prompt a human approves, so "25.00"
    has to mean twenty-five. Accepting a float here would also let 0.1 + 0.2
    arrive as 0.30000000000000004 in the approval text.

    The upper bound is the point: ``2500`` is what you get typing ``25.00`` as
    the integer Meta actually wants. It is a valid Decimal and a catastrophic
    budget, so it is caught before the ceiling check, where the message can say
    what went wrong rather than just "too big".
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(
            f"{name} must be a decimal string in the account's currency, "
            f'e.g. "25.00" -- got {raw!r}. Never pass minor units.'
        ) from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite number, got {raw!r}.")
    if value < 0:
        raise ValueError(f"{name} cannot be negative, got {value}.")
    if value > Decimal("100000"):
        raise ValueError(
            f"{name} of {value} looks like minor units, not currency. Meta "
            f"stores budgets as an integer in the currency's minor unit, but "
            f"this tool takes the major-unit amount and converts it. If you "
            f"meant {value / 100}, pass that."
        )
    return value


def amount_from_stored(stored: str) -> Decimal:
    """Pull the number back out of a stored ``"50.00 CAD"`` string.

    The ledger keeps money WITH its currency code, because a bare "50.00" in an
    audit trail does not say 50 of what. Only legacy rows need this: new rows
    carry the amount in ``inverse_json``.
    """
    text = (stored or "").strip()
    if not text or text == "(not set)":
        raise ValueError(
            f"That entry's previous value was {stored!r}, which is not an "
            f"amount this can put back. Set it explicitly instead."
        )
    try:
        return Decimal(text.split()[0])
    except InvalidOperation as exc:
        raise ValueError(
            f"Could not read an amount out of the stored value {stored!r}."
        ) from exc


def _decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw if raw is not None else "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def from_cap_currency(ctx, entry: dict[str, Any], amount: Decimal) -> Decimal:
    """A cap-currency figure off a LEGACY row, back in the entity's currency.

    Only rows written before ``projected_entity`` was recorded need this. With
    no rate configured the row was never converted (it was blocked or already
    in the cap currency), so it is returned as is.
    """
    from metaads_mcp.guardrails import cap_currency

    currency = (ctx.currency(str(entry.get("account_id") or "") or None) or "").upper()
    if not currency or currency == cap_currency(ctx.limits):
        return amount
    rate = dict(ctx.limits.currency_rates).get(currency)
    if not rate:
        return amount
    return amount / rate


def entity_projection(ctx, entry: dict[str, Any]) -> Decimal:
    """What ``entry`` projected, in its ENTITY's currency.

    The row's ``projected_daily_spend_delta`` is in the CAP currency (it feeds
    the daily total); the entity's own figure is kept in the inverse as
    ``projected_entity``. Reading the cap figure as the entity's and
    converting it again was a double conversion.
    """
    inverse = entry.get("inverse") if isinstance(entry.get("inverse"), dict) else {}
    if inverse.get("projected_entity") is not None:
        return _decimal(inverse["projected_entity"])
    return from_cap_currency(ctx, entry, _decimal(entry.get("projected_daily_spend_delta")))


def undone_projection(ctx, entry: dict[str, Any]) -> Decimal:
    """For a revert row: what the change it undid projected, in the entity's
    currency -- what reverting the revert re-applies."""
    inverse = entry.get("inverse") if isinstance(entry.get("inverse"), dict) else {}
    if inverse.get("undoes_projected_entity") is not None:
        return _decimal(inverse["undoes_projected_entity"])
    return from_cap_currency(ctx, entry, _decimal(inverse.get("undoes_projected")))


def revert_chain(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """(the original change, how many reverts deep ``entry`` is).

    Follows ``reverts`` back to the row that is not a revert: the original is
    depth 0, its revert 1, the revert of that 2, and so on.
    """
    from metaads_mcp import ledger

    root, depth, seen = entry, 0, set()
    while root.get("reverts") and root["reverts"] not in seen:
        seen.add(root["reverts"])
        parent = ledger.find_entry(root["reverts"])
        if parent is None:
            break
        root, depth = parent, depth + 1
    return root, depth


def chain_tightens(entry: dict[str, Any]) -> bool:
    """Whether the change at the root of ``entry``'s revert chain tightened."""
    root, _ = revert_chain(entry)
    inverse = root.get("inverse") if isinstance(root.get("inverse"), dict) else {}
    return bool(inverse.get("tightens"))


def lifts_a_restriction(entry: dict[str, Any]) -> bool:
    """Whether REVERTING ``entry`` lifts a restriction: the chain's original
    tightened, and ``entry`` is an even number of reverts from it (it applies
    the tightening, so undoing it lifts)."""
    root, depth = revert_chain(entry)
    inverse = root.get("inverse") if isinstance(root.get("inverse"), dict) else {}
    return bool(inverse.get("tightens")) and depth % 2 == 0


@dataclass
class LedgerItem:
    """One row in the ledger: one entity, one field, one before/after.

    A batch writes one of these per entity, all sharing a ``batch_id``, so any
    single element can be reverted on its own and the batch as one.

    ``inverse`` is whatever undoing it needs beyond before/after -- the
    previous creative id, the full previous targeting spec. It lands in the
    row's ``inverse_json`` alongside the kind's name.
    """

    entity_type: str
    entity_id: str
    entity_name: str
    field_name: str
    before: str
    after: str
    inverse: dict[str, Any] | None = None


@dataclass
class Proposal:
    """What a kind wants to do, before the engine checks and tokenises it."""

    entity_type: str
    entity_id: str
    entity_name: str
    path: str
    field_name: str
    before: str
    after: str
    checks: list[Check]
    projected: Decimal
    #: Everything the apply needs, carried in the token. JSON-able values only.
    payload: dict[str, Any]
    status: str | None = None
    effective_status: str | None = None
    metrics: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    note: str | None = None
    #: One per ledger row. Empty means "one row, described by the fields above".
    items: list[LedgerItem] = field(default_factory=list)
    #: The campaign this touches, for the campaign scope check. None when the
    #: change is account-level (an audience, a label).
    campaign_id: str | None = None
    #: The ad account the ENTITY belongs to, for the account scope check. None
    #: for something that does not exist yet (it is created in the default
    #: account, which is what the check then compares).
    account_id: str | None = None
    #: A batch: the engine adds the entities_per_call check.
    batch: bool = False
    #: An ACTIVATION -- resuming delivery of something paused. Its projection
    #: is the whole budget it can resume, which on any real campaign exceeds
    #: the per-change max_projected_delta_per_day; if that limit applied, a
    #: campaign with a budget over it could never be re-activated. So an
    #: activation is exempt from the per-change limit and counted, in full,
    #: against the DAILY total and the session total instead. Budget and bid
    #: increases are not activations and must pass both.
    is_activation: bool = False
    #: For a kind with ``verifies_ad``: what the preview checks and fetches
    #: previews for. See :mod:`metaads_mcp.verify`.
    verify: dict[str, Any] | None = None
    #: Preview links a kind fetched itself (placements: every ad, new set).
    previews: list[dict[str, Any]] | None = None
    #: What revert_change needs beyond before/after, for the single-row case.
    inverse: dict[str, Any] | None = None
    #: This change TIGHTENS delivery (a cap or floor added or raised, a bid
    #: lowered, targeting narrowed, hours restricted). It projects nothing
    #: itself, but undoing it lifts the restriction: the ledger marks it, and
    #: its revert projects the unused budget headroom and counts it.
    tightens: bool = False
    #: The kind says this change can only LOWER spend (a cut, a pause): the
    #: engine may then skip the cap_currency refusal when nothing is
    #: projected. A projection of 0 alone never proves that.
    reduces_spend: bool = False
    #: The preview's ``naming`` blocks (metaads_reporting.naming), one per name
    #: decided: requested, final, prefix, base, length, max_length, rule.
    naming: list[dict[str, Any]] = field(default_factory=list)

    def name(self, decision) -> str:
        """Fold a :class:`~metaads_mcp.changes.context.NameDecision` in: its
        checks, warnings and block. Returns the final name."""
        self.checks.extend(decision.checks)
        self.warnings.extend(w for w in decision.warnings if w not in self.warnings)
        if decision.block:
            self.naming.append(decision.block)
        return decision.final

    def with_inverse(self, inverse: dict[str, Any]) -> "Proposal":
        self.inverse = inverse
        return self

    def ledger_items(self) -> list[LedgerItem]:
        if self.items:
            return self.items
        return [
            LedgerItem(
                entity_type=self.entity_type,
                entity_id=str(self.entity_id),
                entity_name=self.entity_name,
                field_name=self.field_name,
                before=self.before,
                after=self.after,
                inverse=self.inverse,
            )
        ]


class ChangeKind:
    """One sort of change. Subclass, fill in the methods, register it.

    ``propose`` is also the TOOL SIGNATURE: the engine publishes
    ``preview_<name>`` with exactly ``propose``'s parameters (minus ``ctx``)
    and its docstring as the description. So annotate every parameter with
    ``Annotated[..., Field(description=...)]`` the way a hand-written tool
    would, and write the docstring for the agent that will read it.
    """

    #: preview_<name> / apply_<name>.
    name: str = ""
    preview_title: str = ""
    apply_title: str = ""
    apply_annotations: ToolAnnotations = APPLY_SPENDS
    #: How many applies one of these costs against the session and daily caps.
    #: A creation costs 3, so an agent cannot build out an account in a loop.
    weight: int = 1
    #: (entity_type, field) pairs this kind's rows use. How revert_change finds
    #: the kind for a LEGACY row, one written before inverse_json named it.
    ledger_keys: tuple[tuple[str, str], ...] = ()
    #: False for a kind whose entries cannot be undone here at all (an audience
    #: is never deleted): revert_change gives the kind's reason first.
    revertible: bool = True
    #: Whether the preview also sends the change with validate_only, so it
    #: reports whether META accepts it and not only whether our bounds pass.
    validate_in_preview: bool = True
    #: A kind that creates or changes an ad or creative. The engine then runs
    #: the ad verification (previews and checks before, field diff and review
    #: after) and no kind can opt out of it -- see metaads_mcp.verify.
    verifies_ad: bool = False
    #: Entity types this kind CREATES with a name. With a prefix configured,
    #: the engine builds the request and requires every name it actually
    #: SENDS to carry the prefix (and at least one name to be sent);
    #: tests/test_mcp_surface.py requires every creating kind to declare it.
    creates_named: tuple[str, ...] = ()
    #: The kind cannot add spend BY CONSTRUCTION: names and labels, and
    #: creations (everything is created PAUSED, enforced by the write client).
    #: Only these skip the allowed_currencies check; every other kind is
    #: checked whatever it projects.
    adds_no_spend: bool = False

    def follow_ups(self, ctx, payload: dict[str, Any], result, created: list[str]) -> list:
        """Writes to make right after a successful apply, each its own ledger
        row: ``[(PlannedChange, LedgerItem), ...]``. The copies' renames.

        A ``None`` plan is a write the kind refused: it is reported as failed
        (the reason in ``item.inverse["refused"]``), never sent or ledgered."""
        return []

    def validates(self, payload: dict[str, Any]) -> bool:
        return self.validate_in_preview

    def propose(self, ctx, **kwargs) -> Proposal:  # pragma: no cover - abstract
        raise NotImplementedError

    def current(self, ctx, payload: dict[str, Any]) -> str:  # pragma: no cover
        """Today's value, spelled exactly as the proposal spelled ``before``."""
        raise NotImplementedError

    def build(self, ctx, payload: dict[str, Any]):  # pragma: no cover
        """The PlannedChange to send (validate_only in the preview, for real at apply)."""
        raise NotImplementedError

    def prepare_apply(self, ctx, plan, payload: dict[str, Any]):
        """Writes that must happen at APPLY time before the plan (an image upload).

        Runs after the ledger intent, so a failure here is recorded.
        """
        return plan

    def landed(self, ctx, payload: dict[str, Any]) -> str | None:
        """After Meta confirmed the write: why it did NOT do what was approved.

        None (the default) trusts ``result.success``. A kind whose write Meta
        can accept without making the change re-reads and says what is wrong;
        the engine then records the entry as failed, not applied.
        """
        return None

    def revert_landed(self, ctx, entry: dict[str, Any]) -> str | None:
        """The same, for a revert of ``entry``."""
        return None

    def inverse_for(self, payload: dict[str, Any], result, created: list[str]) -> dict[str, Any]:
        """Extra inverse data known only after the apply (a created id)."""
        return {}

    def approved_for(self, ctx, payload: dict[str, Any], plan) -> dict[str, Any]:
        """For ``verifies_ad``: what was approved, to diff the live ad against."""
        return {}

    def verify_target(self, payload: dict[str, Any], result, created: list[str]) -> str | None:
        """For ``verifies_ad``: the ad id to re-read after apply."""
        return None

    def verify_creative_target(self, payload: dict[str, Any], result, created: list[str]) -> str | None:
        """For ``verifies_ad`` kinds that make a creative but no ad."""
        return None

    # -- revert ----------------------------------------------------------

    def revert_projection(self, ctx, entry: dict[str, Any]) -> tuple[Decimal, bool]:
        """(projected added daily spend, is_activation) for reverting ``entry``,
        in the ENTITY's currency -- the engine converts it to the cap currency,
        once.

        A revert is not free: reverting a budget CUT puts spend back. The
        default is the inverse of what the original change projected -- a cut
        of 10 reverts as +10 -- counted like an activation, since it restores a
        state the account already had. A change the ledger marks ``tightens``
        (see :attr:`Proposal.tightens`) reverts at the entity's unused budget
        headroom. Kinds whose original projected 0 but whose revert resumes
        delivery (undoing a pause) override this.
        """
        inverse = entry.get("inverse") if isinstance(entry.get("inverse"), dict) else {}
        if inverse.get("revert_row") and (inverse.get("undoes_projected_entity") is not None
                                          or inverse.get("undoes_projected") is not None):
            # Reverting a REVERT re-applies the change it undid: it costs what
            # that change projected, not the inverse of the revert's own.
            stored = undone_projection(ctx, entry)
        else:
            stored = -entity_projection(ctx, entry)
        if lifts_a_restriction(entry):
            # Undoing a tightening -- or re-applying a revert that undid one,
            # however deep the chain -- lifts the restriction: the entity can
            # spend its unused budget again. The headroom is re-read NOW (the
            # budget may have moved since any stored figure), never below
            # what was stored. Counted like an activation.
            from metaads_mcp.changes.common import unused_headroom

            lifted = max(unused_headroom(ctx, entry), stored, Decimal("0"))
            return lifted, lifted > 0
        again = max(stored, Decimal("0"))
        return again, again > 0

    def revert_reduces_spend(self, ctx, entry: dict[str, Any]) -> bool:
        """Whether undoing ``entry`` can only LOWER spend.

        Such a revert is never refused by the apply-count caps (the kill
        switch still applies). By default: the original projected added
        spend, so undoing it takes that spend away. Kinds whose undo is a
        pause say so themselves.

        A revert ROW is different: reverting it re-applies the original
        change, so it lowers spend only when the original did (projected
        below 0). An original projection of 0 proves nothing -- it may have
        been a tightening, or a mix -- so that is not reducing.
        """
        inverse = entry.get("inverse") if isinstance(entry.get("inverse"), dict) else {}
        if inverse.get("revert_row"):
            try:
                return Decimal(str(inverse.get("undoes_projected") or "0")) < 0
            except (InvalidOperation, ValueError):
                return False
        try:
            return Decimal(str(entry.get("projected_daily_spend_delta") or "0")) > 0
        except (InvalidOperation, ValueError):
            return False

    def revert_row_inverse(self, ctx, entry: dict[str, Any]) -> dict[str, Any] | None:
        """What the row a revert of ``entry`` writes keeps as ITS inverse.

        It must hold the value the revert REPLACES -- ``entry``'s after, which
        is live now (the staleness check just said so) -- so that reverting
        the revert re-applies ``entry``'s change. Copying ``entry``'s own
        inverse instead made a revert of a revert re-send what was already
        live: a no-op the ledger recorded as applied.

        None (the default) records that the revert row cannot be reverted, and
        reverting it is refused with a pointer to the preview tools. Called
        before the revert is sent.
        """
        return None

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        """Today's value for a ledger entry's field, as the entry stored it.

        None means "cannot read it", and the revert skips the staleness check
        rather than refusing every revert of a field nobody taught it about.
        """
        return None

    def inverse(self, ctx, entry: dict[str, Any]):  # pragma: no cover
        """The PlannedChange that puts ``entry['before']`` back."""
        raise ValueError(
            f"{entry.get('field')!r} on {entry.get('entity_type')} cannot be "
            "reverted automatically. Change it back with the matching preview_* "
            "tool instead."
        )
