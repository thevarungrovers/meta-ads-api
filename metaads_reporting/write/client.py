"""The mutating client.

Deliberately a sibling of :class:`~metaads_reporting.client.ReadOnlyMetaAdsClient`
rather than an extension of it. The read-only class keeps its guarantee because
nothing was added to it -- you reach writes by importing a different class from
a different subpackage, which is a decision someone has to make on purpose.

Both share :func:`~metaads_reporting.client.build_transport`, so there is still
exactly one place where credentials become a client.

THREE THINGS THIS CLIENT CANNOT DO, structurally:

* **It cannot DELETE.** There is no method on it that sends one. Meta's delete
  is not a soft state you can set back -- a deleted campaign is gone from the
  API -- so the capability is absent rather than guarded.
* **It cannot set a status to DELETED or ARCHIVED.** Those are reachable
  through an ordinary field update, which is exactly why the check is here and
  not only in the tool layer. ARCHIVED in particular reads as harmless and is
  not: an archived object cannot be un-archived through the API.
* **It cannot write a field nobody allowlisted.** :data:`WRITABLE_FIELDS` is
  per entity type, and a payload key that is not in it is refused before a
  request is built. Meta's update endpoint takes the entire node -- targeting,
  billing event, special ad categories, the lot -- so "which fields may this
  code change" is a question worth answering explicitly rather than inheriting
  from whatever the API happens to accept.

``apply=False`` is not a local dry run. It sends Meta's own
``execution_options=["validate_only"]``, so the full server-side check runs and
nothing changes. That is the difference between a preview and a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import logdb
from ..account_id import account_path
from ..client import build_transport
from ..config import Settings
from ..graph import GraphAPIError, GraphTransport, WRITE_METHODS

#: Fields this package may write, per entity type. Narrower than "whatever the
#: API accepts": each entry is one someone decided to support. Adding to this
#: mapping is the deliberate act that widens the blast radius.
WRITABLE_FIELDS: dict[str, frozenset[str]] = {
    "campaign": frozenset({"status", "name", "daily_budget", "lifetime_budget"}),
    "adset": frozenset(
        {"status", "name", "daily_budget", "lifetime_budget", "bid_amount"}
    ),
    "ad": frozenset({"status", "name"}),
}

#: Statuses this package will set. DELETED and ARCHIVED are absent on purpose:
#: both are irreversible through the API, and PAUSED achieves what anyone
#: actually wants from them.
SETTABLE_STATUSES = frozenset({"ACTIVE", "PAUSED"})

#: Statuses that exist and are refused, so the error can say why rather than
#: just "not allowed".
REFUSED_STATUSES = {
    "DELETED": (
        "DELETED removes the object from the API. It cannot be undone and it "
        "cannot be read back."
    ),
    "ARCHIVED": (
        "ARCHIVED cannot be reversed through the API -- only in Ads Manager, "
        "and not always there."
    ),
}

#: Budget above which a write is refused unless explicitly overridden. Not a
#: security control -- it is a typo catch, in the account's MAJOR units. The
#: failure it exists for is an amount entered in minor units by mistake, which
#: turns 50 into 5,000.
DEFAULT_MAX_DAILY_BUDGET = 10_000.0


class MutationError(RuntimeError):
    """A write failed, or was refused before being sent."""


class GuardrailViolation(MutationError):
    """A write was refused by a local guardrail before reaching the API."""


@dataclass
class MutationResult:
    """What happened to one write."""

    validated_only: bool
    node_id: str = ""
    response: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return not self.validated_only

    @property
    def success(self) -> bool:
        """Meta answers an update with ``{"success": true}``, a create with an id."""
        if self.response.get("success") is True:
            return True
        return bool(self.response.get("id"))

    def __str__(self) -> str:
        verb = "validated" if self.validated_only else "APPLIED"
        return f"{verb}: {self.node_id or '(new)'}"


def check_writable(entity_type: str, payload: dict[str, Any]) -> None:
    """Refuse a payload touching a field this package does not write.

    Raises before anything is built, so an unsupported field never reaches a
    request -- not even as a validate-only one.
    """
    allowed = WRITABLE_FIELDS.get(entity_type)
    if allowed is None:
        raise MutationError(
            f"{entity_type!r} is not an entity this package writes. Known: "
            f"{', '.join(sorted(WRITABLE_FIELDS))}."
        )
    unknown = sorted(set(payload) - allowed - {"execution_options"})
    if unknown:
        raise MutationError(
            f"Refusing to write {', '.join(unknown)} on a {entity_type}: not in "
            f"the allowlist. Writable here: {', '.join(sorted(allowed))}. "
            f"Widening that set is a deliberate edit to "
            f"metaads_reporting/write/client.py."
        )


def check_status(status: str) -> str:
    """Return a settable status, or raise with the reason it is refused."""
    wanted = (status or "").strip().upper()
    if wanted in REFUSED_STATUSES:
        raise MutationError(
            f"{wanted} is not available here. {REFUSED_STATUSES[wanted]} Use "
            f"PAUSED, which stops delivery just as completely and can be undone."
        )
    if wanted not in SETTABLE_STATUSES:
        raise MutationError(
            f"{status!r} is not a settable status. Choose one of: "
            f"{', '.join(sorted(SETTABLE_STATUSES))}."
        )
    return wanted


class MutatingMetaAdsClient:
    """A client that can change an account -- but not by accident.

    :meth:`update` never writes unless ``apply=True`` is passed explicitly.
    With ``apply=False`` (the default) it still contacts the API, sending
    ``execution_options=["validate_only"]`` so Meta performs the full
    server-side check and changes nothing.
    """

    def __init__(
        self,
        transport: GraphTransport,
        settings: Settings,
        *,
        audit_log: Any | None = None,
        max_daily_budget: float = DEFAULT_MAX_DAILY_BUDGET,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self.max_daily_budget = max_daily_budget
        if audit_log is None:
            from .audit import AuditLog

            audit_log = AuditLog()
        self._audit = audit_log
        # A read client built on the SAME transport, so a before/after diff
        # costs no second connection and no second token read.
        from ..client import ReadOnlyMetaAdsClient

        self._read = ReadOnlyMetaAdsClient(transport, settings)

    @classmethod
    def from_env(
        cls, *, settings: Settings | None = None, session: Any | None = None, **kwargs: Any
    ) -> "MutatingMetaAdsClient":
        resolved = settings or Settings.from_env()
        return cls(build_transport(resolved, session=session), resolved, **kwargs)

    # -- plumbing ----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def read(self) -> Any:
        """The read side, for fetching current state before a write."""
        return self._read

    @property
    def transport(self) -> GraphTransport:
        return self._transport

    def resolve_account_id(self, override: str | None = None) -> str:
        return self._settings.resolve_account_id(override)

    def currency(self, account_id: str | None = None) -> str:
        return self._read.currency(account_id)

    def node(self, node_id: str, fields: tuple[str, ...]) -> dict[str, Any]:
        """Read one object. Writes need current state to build a diff."""
        return self._read.node(node_id, fields)

    # -- the one write path ------------------------------------------------

    def update(
        self,
        node_id: str,
        *,
        entity_type: str,
        payload: dict[str, Any],
        apply: bool = False,
        account_id: str | None = None,
    ) -> MutationResult:
        """Write ``payload`` to one object, validating unless ``apply`` is True.

        The only method in the package that can change anything, and the only
        place ``validate_only`` is decided.

        There is no retry. A POST that timed out may already have landed at
        Meta's end, and this cannot tell -- so it fails, leaving an intent in
        the ledger with no outcome, which is the honest record of "this may
        have applied". See :mod:`metaads_reporting.graph`.
        """
        if not payload:
            raise MutationError("Nothing to write: the payload is empty.")
        check_writable(entity_type, payload)
        if "status" in payload:
            payload = dict(payload, status=check_status(payload["status"]))

        target = self.resolve_account_id(account_id)
        body = dict(payload)
        if not apply:
            # Meta's own server-side check. A list, JSON-encoded: passing the
            # bare string reads as a one-character option and is ignored.
            body["execution_options"] = json.dumps(["validate_only"])

        correlation_id = self._audit.attempt(
            account_id=target,
            node_id=str(node_id),
            method="POST",
            edge=None,
            payload=payload,
        )

        try:
            response = self._transport.request(
                "POST", str(node_id), data=body
            )
        except GraphAPIError as exc:
            failure = MutationError(
                f"Updating {entity_type} {node_id} failed.\n{exc}"
            )
            self._audit.outcome(correlation_id, ok=False, error=str(failure))
            raise failure from exc

        self._audit.outcome(correlation_id, ok=True, response=response)
        return MutationResult(
            validated_only=not apply, node_id=str(node_id), response=response
        )

    def create(
        self,
        *,
        entity_type: str,
        edge: str,
        payload: dict[str, Any],
        apply: bool = False,
        account_id: str | None = None,
    ) -> MutationResult:
        """Create one object under the ad account.

        *** NOTHING CALLS THIS YET. *** It is the foundation for a create path,
        not a create path. ``scripts/manage.py`` has no create command, and the
        MCP server cannot reach this by construction. Earlier versions of the
        README and the server instructions claimed a
        ``manage.py campaign new`` existed; it never did, and that claim is
        gone.

        Deliberately NOT exposed over MCP: creating a campaign carries an
        objective, a budget and a ``special_ad_categories`` declaration that is
        a legal statement about housing, employment, credit and social-issue
        advertising. That belongs to a human who can answer for it.

        ``payload`` is not field-allowlisted the way :meth:`update` is -- a
        create needs fields no update ever sets -- so the only guard is that
        nothing in the MCP server can call it. Anyone wiring this up owes it a
        guard of its own.
        """
        if not payload:
            raise MutationError("Nothing to create: the payload is empty.")
        target = self.resolve_account_id(account_id)
        body = dict(payload)
        if not apply:
            body["execution_options"] = json.dumps(["validate_only"])

        path = f"{account_path(target)}/{edge}"
        correlation_id = self._audit.attempt(
            account_id=target,
            node_id="(new)",
            method="POST",
            edge=edge,
            payload=payload,
        )
        try:
            response = self._transport.request("POST", path, data=body)
        except GraphAPIError as exc:
            failure = MutationError(f"Creating a {entity_type} failed.\n{exc}")
            self._audit.outcome(correlation_id, ok=False, error=str(failure))
            raise failure from exc

        self._audit.outcome(correlation_id, ok=True, response=response)
        return MutationResult(
            validated_only=not apply,
            node_id=str(response.get("id") or ""),
            response=response,
        )

    # -- guardrails --------------------------------------------------------

    def check_daily_budget(self, amount: float, *, override: bool = False) -> None:
        """Refuse an implausibly large daily budget.

        Catches the amount-entered-in-minor-units mistake, which silently turns
        50.00 into 5,000.00 and is indistinguishable from a real value once it
        reaches the API.
        """
        if override:
            return
        if amount > self.max_daily_budget:
            raise GuardrailViolation(
                f"Refusing a daily budget of {amount:,.2f} "
                f"(guardrail: {self.max_daily_budget:,.2f}).\n"
                "  Amounts here are in the account's currency, NOT minor units "
                "-- this tool does that conversion for you.\n"
                f"  If you meant {amount / 100:,.2f}, pass that instead.\n"
                f"  If you really do want {amount:,.2f} per day, re-run with "
                "--override-budget-guardrail."
            )


#: Asserted by tests/test_readonly_guard.py: this module is the only one
#: outside the write package allowed to name a write method at all.
assert WRITE_METHODS == frozenset({"POST", "DELETE"})
