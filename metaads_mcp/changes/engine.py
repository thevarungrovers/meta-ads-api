"""The one preview -> apply -> revert sequence every change kind goes through.

Before this existed each pair carried its own copy of the apply sequence, and
the copies had drifted: the creative path skipped the staleness check, the
audience path ignored ``result.success`` and recorded ``(new)`` as the entity,
and revert_change re-implemented the gates by hand. With the sequence written
once, a kind supplies only what is specific to it (see :mod:`.base`) and cannot
skip a gate.

THE APPLY SEQUENCE, in this order, and the order matters:

  1. take the token        single-use, consumed before any check, so a token
                           that fails cannot be retried until something shifts
  2. the kill switch       re-read now, never cached: a preview is good for ten
                           minutes and the switch must stop the very next apply
  3. staleness             the entity's CURRENT value must still be the preview's
                           ``before``, or someone edited it in Ads Manager
  4. the caps              session, then day -- after staleness, so a stale
                           apply does not burn a slot it never used
  5. ledger intent         BEFORE the request leaves the process
  6. the mutation          (plus any apply-time upload the kind needs)
  7. result.success        Meta answering without confirming is NOT applied
  8. ledger outcome        and only then the counters
  9. ad verification       for a kind that changes an ad: re-read and diff

This module deliberately has no ``from __future__ import annotations``: the
tool functions it generates carry annotations built at runtime (a Field whose
description names the matching preview), and string annotations would have to
be resolved against globals where those values do not exist.
"""

import inspect
import secrets
import typing
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any, Callable

from pydantic import Field

from metaads_mcp import ledger
from metaads_mcp.changes.base import PREVIEW_SAFE, ChangeKind, LedgerItem, Proposal
from metaads_mcp.guardrails import (
    Check,
    any_failed,
    check_account_in_scope,
    check_campaign_in_scope,
    check_count,
    check_projected_delta,
    check_writes_enabled,
    render_checks,
    require_writes_enabled,
)
from metaads_mcp.previews import (
    TOKEN_TTL_SECONDS,
    ApplyResult,
    ChangePreview,
    PreviewExpired,
    PreviewStale,
)


class Engine:
    """Runs kinds against one :class:`~.context.WriteContext`."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.kinds: dict[str, ChangeKind] = {}

    # -- registry --------------------------------------------------------

    def add(self, kind: ChangeKind) -> ChangeKind:
        if kind.name in self.kinds:
            raise ValueError(f"change kind {kind.name!r} registered twice")
        self.kinds[kind.name] = kind
        return kind

    def kind_for_entry(self, entry: dict[str, Any]) -> ChangeKind:
        """The kind that wrote this ledger row.

        By the name stored in ``inverse_json`` when there is one; otherwise --
        a row written before schema version 2 -- by (entity_type, field).
        Never by the tool name, because a revert's own row carries
        tool="revert_change" and reverting a revert has to find the same kind.
        """
        inverse = entry.get("inverse")
        if isinstance(inverse, dict) and inverse.get("kind") in self.kinds:
            return self.kinds[inverse["kind"]]
        key = (entry.get("entity_type"), entry.get("field"))
        for kind in self.kinds.values():
            if key in kind.ledger_keys:
                return kind
        raise ValueError(
            f"Cannot revert field {entry.get('field')!r} on a "
            f"{entry.get('entity_type')} automatically (entry "
            f"{entry.get('entry_id')}): no change kind owns it."
        )

    def _require_live_token(self) -> None:
        """Refuse a write with a token /debug_token says is dead.

        Checked at apply, against the hour-cached health, so a write never
        leaves this process on a credential that cannot carry it. "unknown"
        does not block: Meta itself will answer for the token.
        """
        from metaads_mcp.errors import AUTH_EXPIRED
        from metaads_reporting.token_health import token_health

        health = token_health(self.ctx.read)
        if health.get("status") == "expired":
            raise PreviewExpired(
                f"{AUTH_EXPIRED}: the access token is expired or invalid "
                f"({health.get('expires')}). Nothing was sent. Generate a new "
                "System User token and restart the server."
            )

    # -- caps ------------------------------------------------------------

    def _refuse_if_over_caps(self, projected: Decimal, weight: int) -> None:
        """Session first (the tighter, per-process bound), then the day."""
        blocked = self.ctx.counters.would_exceed(projected, weight=weight)
        if blocked:
            raise PreviewExpired(blocked)
        blocked = self.ctx.daily.would_exceed(projected, weight=weight)
        if blocked:
            raise PreviewExpired(blocked)

    # -- preview ---------------------------------------------------------

    def preview(self, kind: ChangeKind, args: dict[str, Any]) -> ChangePreview:
        ctx = self.ctx
        proposal: Proposal = kind.propose(ctx, **args)
        items = proposal.ledger_items()

        # The ENTITY's account, not the default compared with itself: one
        # token routinely reaches several accounts, and before this any
        # campaign id the token could see was writable. Something not yet
        # created has no account of its own; it goes to the write client's.
        checks: list[Check] = [
            check_writes_enabled(),
            check_account_in_scope(
                ctx.limits,
                str(proposal.account_id or ctx.target_account()).replace("act_", ""),
                ctx.default_account,
            ),
        ]
        if proposal.campaign_id is not None:
            checks.append(check_campaign_in_scope(ctx.limits, str(proposal.campaign_id)))
        checks.extend(proposal.checks)
        if proposal.batch:
            checks.append(check_count(ctx.limits, len(items)))
        if proposal.is_activation:
            # Activating resumes spend up to the whole budget, which on any
            # real campaign is over the per-change limit -- so the limit does
            # not apply, and the activation is counted in full against the
            # DAILY total (and the session total) instead.
            checks.append(Check(
                "projected_delta_per_day", True,
                "activation: counted against the daily total", skipped=True,
            ))
        else:
            checks.append(
                check_projected_delta(ctx.limits, max(proposal.projected, Decimal("0")))
            )
        daily_total = ctx.daily.check(proposal.projected)
        checks.append(daily_total)
        activation_warnings = []
        if proposal.is_activation and proposal.projected > 0:
            activation_warnings.append(
                f"activating resumes spend up to {proposal.projected:.2f}/day; it "
                f"{daily_total.detail}"
            )

        payload = dict(proposal.payload)
        payload["before"] = proposal.before
        payload["after"] = proposal.after
        payload["_items"] = [asdict(item) for item in items]
        payload["_entity"] = {
            "entity_type": proposal.entity_type,
            "entity_id": str(proposal.entity_id),
            "entity_name": proposal.entity_name,
            "field_name": proposal.field_name,
        }

        previews = proposal.previews
        if kind.verifies_ad:
            from metaads_mcp import verify

            if proposal.verify is None:
                raise RuntimeError(
                    f"{kind.name} declares verifies_ad but proposed no verify spec"
                )
            ad_checks, previews = verify.before_apply(ctx, proposal.verify)
            checks.extend(ad_checks)

        # Meta's own verdict, without changing anything. Only once our own
        # checks pass: a blocked preview will never be applied, so asking Meta
        # about it costs a request against the rate limit and tells nobody
        # anything.
        if kind.validates(payload) and not any_failed(checks):
            checks.append(ctx.validate(lambda: kind.build(ctx, payload)))

        blocked = any_failed(checks)
        token = ctx.previews.mint(
            tool=f"apply_{kind.name}",
            payload=payload,
            projected_delta=proposal.projected,
            blocked=blocked,
        )
        return ChangePreview(
            tool=f"apply_{kind.name}",
            preview_token=token,
            expires_in_seconds=TOKEN_TTL_SECONDS,
            entity_type=proposal.entity_type,
            entity_id=str(proposal.entity_id),
            entity_name=proposal.entity_name,
            path=proposal.path,
            field_name=proposal.field_name,
            before=proposal.before,
            after=proposal.after,
            account_id=ctx.default_account,
            currency=ctx.currency(),
            status=proposal.status,
            effective_status=proposal.effective_status,
            metrics_7d=proposal.metrics,
            projected_daily_spend_delta=f"{proposal.projected:.2f}",
            checks=render_checks(checks),
            blocked=blocked,
            warnings=[
                *proposal.warnings,
                *activation_warnings,
                *ctx.cap_warnings(proposal.projected, kind.weight),
            ],
            note=proposal.note,
            items=(
                [{k: v for k, v in asdict(i).items() if k != "inverse"} for i in items]
                if proposal.batch
                else None
            ),
            apply_weight=kind.weight,
            previews=previews,
        )

    # -- apply -----------------------------------------------------------

    def apply(self, kind: ChangeKind, token: str) -> ApplyResult:
        ctx = self.ctx
        tool = f"apply_{kind.name}"
        pending = ctx.previews.take(token, tool)
        # The kill switch FIRST, re-read here and never cached. The preview ran
        # check_writes_enabled() too, but that was up to ten minutes ago, and
        # the switch is a file precisely so somebody can throw it from another
        # terminal mid-session.
        require_writes_enabled()
        self._require_live_token()
        payload = pending.payload
        entity = payload["_entity"]

        current = kind.current(ctx, payload)
        if payload["before"] != current:
            raise PreviewStale(
                f"{entity['entity_name']} has changed since the preview: it was "
                f"{payload['before']!r} then and is {current!r} now. "
                "Someone edited it in between -- Ads Manager is the usual "
                "culprit. Take a fresh preview."
            )
        self._refuse_if_over_caps(pending.projected_delta, kind.weight)

        plan = kind.build(ctx, payload)
        items = [LedgerItem(**item) for item in payload["_items"]]
        result, created, entry_ids, batch_id, plan = self._send_recorded(
            kind,
            plan,
            items,
            tool=tool,
            projected=pending.projected_delta,
            weight=kind.weight,
            payload=payload,
        )
        ctx.counters.record(pending.projected_delta, weight=kind.weight)

        verification = None
        if kind.verifies_ad:
            from metaads_mcp import verify

            ad_id = kind.verify_target(payload, result, created)
            approved = kind.approved_for(ctx, payload, plan)
            if ad_id:
                verification = verify.after_apply(ctx, ad_id, approved)
            else:
                verification = verify.after_create_creative(
                    ctx, kind.verify_creative_target(payload, result, created), approved,
                    payload.get("adset_id"),
                )
            ledger.set_verification(entry_ids[0], verify.for_ledger(verification))

        own = kind.verify_target(payload, result, created) or (
            created[-1] if created else None
        )
        return ApplyResult(
            applied=True,
            entry_id=entry_ids[0],
            entity_type=entity["entity_type"],
            entity_id=str(self._result_entity_id(entity, result, created)),
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            session=ctx.counters.snapshot(),
            daily=self._daily_snapshot(),
            resource_name=own,
            batch_id=batch_id,
            entry_ids=entry_ids if batch_id else [],
            projected_daily_spend_delta=f"{pending.projected_delta:.2f}",
            verification=verification,
        )

    @staticmethod
    def _result_entity_id(entity: dict[str, Any], result, created: list[str]) -> str:
        """The id the human should look for: a created thing's NEW id."""
        return result.node_id or (created[-1] if created else None) or entity["entity_id"]

    def _daily_snapshot(self):
        daily = getattr(self.ctx, "daily", None)
        return daily.snapshot() if daily is not None else None

    def _send_recorded(
        self,
        kind: ChangeKind | None,
        plan,
        items: list[LedgerItem],
        *,
        tool: str,
        projected: Decimal,
        weight: int,
        payload: dict[str, Any] | None = None,
        reverts: list[str] | None = None,
        kind_name: str | None = None,
    ):
        """Ledger intents, the mutation, ledger outcomes. Raises on failure.

        One ledger row per item. A batch's rows share a batch_id and each
        carries the WHOLE batch's projected delta and weight: the daily cap
        groups by batch and takes one row's numbers, so a batch costs its
        weight once, not once per element.
        """
        from metaads_reporting.write.client import MutationError

        ctx = self.ctx
        batch_id = secrets.token_hex(6) if len(items) > 1 else None
        entry_ids: list[str] = []
        name = kind_name or (kind.name if kind else None)
        for index, item in enumerate(items):
            entry_id = ledger.new_entry_id()
            entry_ids.append(entry_id)
            inverse = {
                "kind": name,
                "weight": weight,
                **({"batch_id": batch_id} if batch_id else {}),
                **(item.inverse or {}),
            }
            ledger.write_intent(
                entry_id=entry_id,
                tool=tool,
                account_id=ctx.default_account,
                entity_type=item.entity_type,
                entity_id=str(item.entity_id),
                entity_name=item.entity_name,
                field_name=item.field_name,
                before=item.before,
                after=item.after,
                projected_delta=str(projected),
                inverse=inverse,
                # In the INTENT, not only the outcome: a revert that fails (or
                # whose outcome never arrives) must still block a second
                # attempt -- the first may have landed at Meta's end.
                reverts=reverts[index] if reverts else None,
            )
        if isinstance(plan, list):
            return self._send_each(plan, entry_ids, batch_id, reverts)
        try:
            if kind is not None and payload is not None:
                plan = kind.prepare_apply(ctx, plan, payload)
            result, created = ctx.send(plan, apply=True)
            if not result.success:
                # Meta answers an update with {"success": true} and a create
                # with an id. Anything else means the request was accepted and
                # the change was not made, which must not be recorded as applied.
                raise MutationError(
                    f"Meta accepted the request but did not confirm the change: "
                    f"{result.response!r}. Nothing has been recorded as applied. "
                    f"Check the object in Ads Manager before retrying."
                )
        except Exception as exc:
            for entry_id in entry_ids:
                ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise

        extra = (
            kind.inverse_for(payload, result, created)
            if kind is not None and payload is not None
            else {}
        )
        resource = result.node_id or (created[-1] if created else None)
        for index, entry_id in enumerate(entry_ids):
            # Known only now: the created id, merged into the row so the ledger
            # names something findable and the inverse can reach it.
            ledger.amend(entry_id, entity_id=resource, inverse_extra=extra or None)
            ledger.write_outcome(
                entry_id=entry_id,
                ok=True,
                resource_name=resource,
                reverts=reverts[index] if reverts else None,
            )
        return result, created, entry_ids, batch_id, plan

    def _send_each(self, plans: list, entry_ids: list[str], batch_id, reverts):
        """A batch: one write per element, one outcome per row.

        Meta's batch endpoint is not atomic and neither is this. A failure
        stops the loop: rows already sent are recorded applied, the failing
        one failed, the rest "not sent", and the error names which landed.
        """
        from metaads_reporting.write.client import MutationError

        done: list[str] = []
        result = None
        for index, (plan, entry_id) in enumerate(zip(plans, entry_ids)):
            try:
                result, _ = self.ctx.send(plan, apply=True)
                if not result.success:
                    raise MutationError(f"Meta did not confirm: {result.response!r}")
            except Exception as exc:
                ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
                for later in entry_ids[index + 1:]:
                    ledger.write_outcome(entry_id=later, ok=False, error="not sent: an earlier element failed")
                raise MutationError(
                    f"{exc}\n\nBatch {batch_id}: {len(done)} of {len(plans)} applied before this "
                    f"failed ({', '.join(done) or 'none'}); those are in the ledger and "
                    "can be reverted one by one."
                ) from exc
            ledger.write_outcome(entry_id=entry_id, ok=True, resource_name=plan.node_id,
                                 reverts=reverts[index] if reverts else None)
            done.append(entry_id)
        return result, [], entry_ids, batch_id, plans

    # -- revert ----------------------------------------------------------

    def revert(self, entry_id: str) -> ApplyResult:
        ctx = self.ctx
        # A revert is a real write, so it answers to the same gates every
        # apply_* does: the kill switch, and the caps. The APPLIES half first,
        # before any lookup: a revert loop must stop even on entries that turn
        # out not to exist -- find_entry reaches back over the whole ledger,
        # including earlier sessions this process never saw.
        require_writes_enabled()
        self._require_live_token()
        self._refuse_if_over_caps(Decimal("0"), 1)

        entry = ledger.find_entry(entry_id)
        if entry is None:
            batch = ledger.batch_entries(entry_id)
            if batch:
                return self._revert_batch(entry_id, batch)
            raise ValueError(f"No ledger entry {entry_id!r}. See list_my_changes.")
        self._revertible(entry)
        kind = self.kind_for_entry(entry)
        if not kind.revertible:
            kind.inverse(ctx, entry)  # raises, with the kind's own reason
        self._not_stale(kind, entry)
        # Then the SPEND half, now that we know what undoing this restores.
        projected, _ = kind.revert_projection(ctx, entry)
        self._refuse_if_over_caps(projected, 1)
        plan = kind.inverse(ctx, entry)

        _, _, entry_ids, _, _ = self._send_recorded(
            None,
            plan,
            [self._inverse_item(entry)],
            tool="revert_change",
            projected=projected,
            weight=1,
            reverts=[entry_id],
            kind_name=kind.name,
        )
        ctx.counters.record(projected)
        result = self._revert_result(entry, entry_ids[0])
        result.projected_daily_spend_delta = f"{projected:.2f}"
        return result

    def _revert_batch(self, batch_id: str, batch: list[dict[str, Any]]) -> ApplyResult:
        """Undo every still-applied row of one batch, one entry after another.

        Meta's batch endpoint is not atomic, so neither is this: each element
        is its own write and its own ledger row. A failure stops the loop and
        says which entries were reverted before it.
        """
        reverted = ledger.reverted_entry_ids()
        live = [e for e in batch if e.get("applied") and e["entry_id"] not in reverted]
        if not live:
            raise ValueError(
                f"Batch {batch_id} has nothing left to revert: every entry in it "
                "was never applied or has already been reverted."
            )
        done: list[str] = []
        last = None
        for entry in live:
            try:
                last = self.revert(entry["entry_id"])
            except Exception as exc:
                raise type(exc)(
                    f"{exc}\n\nReverted before this failed: {done or 'none'}."
                ) from exc
            done.append(last.entry_id)
        last.batch_id = batch_id
        last.entry_ids = done
        last.entity_name = f"{len(done)} entries of batch {batch_id}"
        return last

    @staticmethod
    def _revertible(entry: dict[str, Any]) -> None:
        if not entry.get("applied"):
            raise ValueError(
                f"Entry {entry['entry_id']} was never recorded as applied; there "
                f"is nothing to revert. If it has no error either, the write left "
                f"this process and nothing recorded how it ended -- check the "
                f"object in Ads Manager rather than reverting blind."
            )
        if entry["entry_id"] in ledger.reverted_entry_ids():
            raise ValueError(f"Entry {entry['entry_id']} has already been reverted.")

    def _not_stale(self, kind: ChangeKind, entry: dict[str, Any]) -> None:
        # STALENESS, the same check apply does. If the value is no longer what
        # our change left behind, somebody moved it since -- most often a
        # person in Ads Manager -- and putting our old value back would silently
        # discard THEIR change, while the ledger recorded it as a tidy undo.
        current = kind.current_for_entry(self.ctx, entry)
        if current is not None and current != entry["after"]:
            raise PreviewStale(
                f"{entry['entity_name']} {entry['field']} is {current!r} now, but "
                f"this entry left it at {entry['after']!r}. Something changed it "
                f"since, so reverting would overwrite that instead of undoing "
                f"this. Look at list_my_changes and Ads Manager before deciding."
            )

    @staticmethod
    def _inverse_item(entry: dict[str, Any]) -> LedgerItem:
        inverse = dict(entry.get("inverse") or {})
        for key in ("kind", "weight", "batch_id"):
            inverse.pop(key, None)
        return LedgerItem(
            entity_type=entry["entity_type"],
            entity_id=str(entry["entity_id"]),
            entity_name=entry["entity_name"],
            field_name=entry["field"],
            before=entry["after"],
            after=entry["before"],
            inverse=inverse or None,
        )

    def _revert_result(self, entry: dict[str, Any], new_entry: str) -> ApplyResult:
        return ApplyResult(
            applied=True,
            entry_id=new_entry,
            entity_type=entry["entity_type"],
            entity_id=str(entry["entity_id"]),
            entity_name=entry["entity_name"],
            field_name=entry["field"],
            before=entry["after"],
            after=entry["before"],
            session=self.ctx.counters.snapshot(),
            daily=self._daily_snapshot(),
            resource_name=entry.get("resource_name"),
        )


# ---------------------------------------------------------------------------
# Publishing a kind as two MCP tools
# ---------------------------------------------------------------------------


def _preview_function(engine: Engine, kind: ChangeKind) -> Callable[..., ChangePreview]:
    """``preview_<name>``, with exactly ``kind.propose``'s parameters.

    The MCP SDK builds the tool's input schema from the function signature, so
    the generated function has to LOOK like the hand-written tool it replaces:
    same parameter names, same Annotated descriptions, same docstring.
    """
    method = kind.propose
    signature = inspect.signature(method)
    hints = typing.get_type_hints(method, include_extras=True)
    parameters = [p for p in signature.parameters.values() if p.name != "ctx"]

    def preview(**kwargs: Any) -> ChangePreview:
        return engine.preview(kind, kwargs)

    preview.__name__ = preview.__qualname__ = f"preview_{kind.name}"
    preview.__doc__ = inspect.getdoc(method)
    preview.__signature__ = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in parameters],
        return_annotation=ChangePreview,
    )
    preview.__annotations__ = {
        **{p.name: hints[p.name] for p in parameters if p.name in hints},
        "return": ChangePreview,
    }
    return preview


def _apply_function(engine: Engine, kind: ChangeKind) -> Callable[..., ApplyResult]:
    """``apply_<name>(preview_token)`` -- and nothing else, ever.

    An apply that accepted the values directly could be called cold, with no
    preview shown first; test_mcp_surface pins that every apply takes only the
    token.
    """
    token_type = Annotated[str, Field(description=f"From preview_{kind.name}")]

    def apply(preview_token: str) -> ApplyResult:
        return engine.apply(kind, preview_token)

    apply.__name__ = apply.__qualname__ = f"apply_{kind.name}"
    apply.__doc__ = (
        f"Apply the change preview_{kind.name} showed. Takes only its token; "
        "show the human the preview first -- their approval of this call is the "
        "authorization."
    )
    apply.__signature__ = inspect.Signature(
        [inspect.Parameter("preview_token", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           annotation=token_type)],
        return_annotation=ApplyResult,
    )
    apply.__annotations__ = {"preview_token": token_type, "return": ApplyResult}
    return apply


def register_pair(
    mcp,
    engine: Engine,
    kind: ChangeKind,
    *,
    refusals: Callable,
    apply_tool: Callable,
) -> None:
    """Register ``preview_<kind>`` and ``apply_<kind>`` for one kind.

    ``apply_tool`` is the read-only-aware decorator from tools_write: in
    read-only mode it registers nothing, so the apply is absent from tools/list
    rather than refused at call time.
    """
    engine.add(kind)
    mcp.tool(title=kind.preview_title, annotations=PREVIEW_SAFE)(
        refusals(_preview_function(engine, kind))
    )
    apply_tool(title=kind.apply_title, annotations=kind.apply_annotations)(
        refusals(_apply_function(engine, kind))
    )
