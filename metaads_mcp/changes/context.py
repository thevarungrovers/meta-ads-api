"""What every change kind is handed: the clients, the limits, and the helpers.

One object rather than a dozen closures, so a kind is a plain class that can be
tested with the simulator, and so the engine and the kinds agree on where the
write client, the counters and the daily budget live.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from metaads_mcp.guardrails import Check
from metaads_reporting.insights import InsightsQuery

#: What a not-yet-uploaded image looks like inside a creative payload.
PENDING_UPLOAD = "<pending upload>"


class NameDecision:
    """The name a kind will SEND, with what the preview says about it.

    A refused name does not raise: it becomes a FAIL line (``agent_identity``,
    ``name_length`` or ``naming``) in the preview the human reads, and the
    apply is blocked with it. ``final`` is then the name as requested, which
    nothing will send.
    """

    def __init__(self, final: str, *, block: dict | None = None, checks=None,
                 warnings=None, entity_type: str = "") -> None:
        self.final = final
        self.block = block
        self.checks = list(checks or [])
        self.warnings = list(warnings or [])
        self.entity_type = entity_type

    @property
    def refused(self) -> bool:
        return any(not c.ok for c in self.checks)


def _decide(identity, entity_type: str, fallback: str, make) -> NameDecision:
    from metaads_reporting.naming import NameRefused

    try:
        stamped = make()
    except NameRefused as exc:
        message = str(exc)
        if identity.problem and identity.problem in message:
            name = "agent_identity"
        elif "too long" in message:
            name = "name_length"
        else:
            name = "naming"
        return NameDecision((fallback or "").strip(), entity_type=entity_type,
                            checks=[Check(name, False, message)])
    warnings = [w for w in [stamped.warning(), *stamped.notes] if w]
    return NameDecision(stamped.final, block=stamped.block(), warnings=warnings,
                        entity_type=entity_type)


class WriteContext:
    def __init__(
        self,
        state,
        client,
        execute: Callable[..., Any],
        execute_chain: Callable[..., Any],
    ) -> None:
        self.state = state
        self.client = client
        self._execute = execute
        self._execute_chain = execute_chain

    # -- state -----------------------------------------------------------

    @property
    def read(self):
        return self.client.read

    @property
    def limits(self):
        return self.state.limits

    @property
    def counters(self):
        return self.state.counters

    @property
    def previews(self):
        return self.state.previews

    @property
    def identity(self):
        """The owner/agent prefix this server stamps (metaads_reporting.naming)."""
        identity = getattr(self.state, "identity", None)
        if identity is None:
            from metaads_reporting.naming import Identity

            identity = Identity()
        return identity

    def name_for_create(self, entity_type: str, requested: str) -> NameDecision:
        """The final name of something the agent CREATES (metaads_reporting.naming)."""
        from metaads_reporting.naming import stamp

        return _decide(self.identity, entity_type, requested,
                       lambda: stamp(self.identity, requested, entity_type=entity_type))

    def name_for_copy(self, entity_type: str, source_name: str, suffix: str = "") -> NameDecision:
        """The name of a copy the agent makes: our prefix on the source's base."""
        from metaads_reporting.naming import restamp_for_copy

        return _decide(self.identity, entity_type, f"{source_name}{suffix}",
                       lambda: restamp_for_copy(self.identity, source_name,
                                                entity_type=entity_type, suffix=suffix))

    def name_derived(self, entity_type: str, name: str) -> NameDecision:
        """A name derived from one already decided: checked for length only."""
        from metaads_reporting.naming import derived

        return _decide(self.identity, entity_type, name,
                       lambda: derived(name, entity_type=entity_type))

    def name_for_rename(self, entity_type: str, current: str, requested: str, *,
                        ledger_owned: bool) -> NameDecision:
        """The new name of one of the agent's OWN entities; others are refused."""
        from metaads_reporting.naming import rename

        return _decide(self.identity, entity_type, requested,
                       lambda: rename(self.identity, current, requested,
                                      entity_type=entity_type, ledger_owned=ledger_owned))

    @property
    def default_account(self) -> str:
        return self.state.default_account_id

    def target_account(self) -> str:
        """The account a create will actually be sent to.

        Asked of the WRITE client, which resolves the account on every create,
        rather than read off the server's default.
        """
        resolve = getattr(self.client, "resolve_account_id", None)
        if callable(resolve):
            return str(resolve()).replace("act_", "")
        return self.default_account

    def currency(self, account_id: str | None = None) -> str:
        """The currency of ``account_id`` -- the default account's only when None.

        Every kind passes the account of the object it touches (see
        :meth:`currency_of`): scope allows any allowed_account_ids account, and
        a JPY ad set read as CAD is a hundredfold error in the approval prompt.
        """
        return self.client.currency(account_id)

    def currency_of(self, row: dict[str, Any] | None) -> str:
        """The currency of the account ``row`` (a campaign, ad set or ad) is in."""
        account = (row or {}).get("account_id")
        return self.currency(str(account) if account else None)

    # -- sending ---------------------------------------------------------

    def send(self, plan, *, apply: bool):
        """(result, created ids). A plan with ``requires`` goes as a chain."""
        if getattr(plan, "requires", None) is not None:
            return self._execute_chain(self.client, plan, apply=apply)
        return self._execute(self.client, plan, apply=apply), []

    def validate(self, build: Callable[[], Any]) -> Check:
        """Build the change and send it with validate_only.

        Meta runs its own server-side checks -- an invalid targeting spec, a
        budget under the minimum, a creative it would reject -- and changes
        nothing. A refusal becomes a FAIL line in the preview the human reads,
        rather than an error after they approved it. Building happens in here
        too, because a plan function can refuse on its own ("already PAUSED;
        nothing to do") and that refusal belongs on the same line.
        """
        from metaads_reporting.write.client import MutationError

        try:
            plan = build()
        except (MutationError, ValueError, LookupError) as exc:
            return Check("meta_validate_only", False, str(exc).strip())
        if isinstance(plan, list):
            for index, element in enumerate(plan):
                try:
                    self.send(element, apply=False)
                except Exception as exc:  # noqa: BLE001 - reported on the line
                    return Check("meta_validate_only", False,
                                 f"element {index + 1} of {len(plan)}: {str(exc).strip()}")
            return Check("meta_validate_only", True,
                         f"Meta accepted all {len(plan)} elements (validate_only)")
        chained = getattr(plan, "requires", None) is not None
        first = plan.requires if chained else plan
        if PENDING_UPLOAD in json.dumps(getattr(first, "payload", {}), default=str):
            return Check(
                "meta_validate_only",
                True,
                "not sent: the image is uploaded only at apply, so there is no "
                "hash to validate yet",
                skipped=True,
            )
        try:
            self.send(plan, apply=False)
        except (MutationError, ValueError, LookupError) as exc:
            return Check("meta_validate_only", False, str(exc).strip())
        except Exception as exc:  # noqa: BLE001 - a refusal, mapped by errors.py upstream
            return Check("meta_validate_only", False, f"{type(exc).__name__}: {exc}")
        if chained:
            return Check(
                "meta_validate_only",
                True,
                f"Meta accepted step 1 of 2 ({plan.requires.label}); step 2 needs "
                "the id step 1 creates, so it is validated only at apply",
            )
        return Check("meta_validate_only", True, "Meta accepted the request (validate_only)")

    # -- caps ------------------------------------------------------------

    @property
    def daily(self):
        from metaads_mcp.guardrails import DailyUsage

        # Built per use, not cached: it holds no state, only the limits, and
        # every answer comes from the ledger.
        return DailyUsage(self.limits)

    def cap_warnings(self, projected: Decimal, weight: int) -> list[str]:
        """The session and daily caps, as warnings on the preview.

        Warnings rather than FAIL checks on purpose: the caps are enforced at
        APPLY time (another process may use up the day in between), and an
        apply refused for the cap says which cap.
        """
        warnings = []
        for reason in (
            self.counters.would_exceed(projected, weight=weight),
            self.daily.would_exceed(projected, weight=weight),
        ):
            if reason:
                warnings.append(f"the apply will be REFUSED: {reason}")
        return warnings

    # -- reads kinds share -------------------------------------------------

    def metrics_7d(self, node_id: str, level: str) -> dict[str, Any]:
        """Last 7 whole days for one entity, for context in the preview.

        Collapsed into ONE row (no ``time_increment``) rather than summed from
        daily rows, because ``reach`` and ``frequency`` cannot be added across
        days -- the same person reached twice is one person.
        """
        # A preset, so Meta evaluates "the last 7 days" in the ACCOUNT's time
        # zone rather than this machine's.
        query = InsightsQuery(
            fields=("spend", "impressions", "clicks", "reach", "frequency", "ctr", "cpc"),
            level=level,
            date_preset="last_7d",
            time_increment=None,
            limit=5,
        )
        try:
            rows = self.read.insight_rows(query, node_id=str(node_id))
        except Exception as exc:  # noqa: BLE001
            # Context, not correctness. A preview whose metrics lookup failed is
            # still worth showing -- with the failure named, so nobody reads a
            # missing section as "it spent nothing".
            return {"unavailable": f"{type(exc).__name__}: {exc}"}
        if not rows:
            return {"note": "no delivery in the last 7 days"}
        row = rows[0]
        return {
            "spend": row.get("spend"),
            "impressions": row.get("impressions"),
            "clicks": row.get("clicks"),
            "reach": row.get("reach"),
            "frequency": row.get("frequency"),
            "ctr": row.get("ctr"),
            "cpc": row.get("cpc"),
            "window": f"{row.get('date_start')} to {row.get('date_stop')} (account time zone)",
        }

    @staticmethod
    def clicks_7d(metrics: dict[str, Any]) -> int:
        try:
            return int(Decimal(str(metrics.get("clicks") or 0)))
        except (InvalidOperation, ValueError, TypeError):
            return 0

    @staticmethod
    def spend_7d(metrics: dict[str, Any]) -> Decimal:
        try:
            return Decimal(str(metrics.get("spend") or 0))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")
