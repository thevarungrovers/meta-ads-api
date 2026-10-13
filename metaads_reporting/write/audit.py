"""Audit log for writes: every request this package sends to Meta.

Rows go to the ``mutations`` table of ``logs/meta-ads.db`` (see
``metaads_reporting.logdb``).

Each write produces TWO records: an ``attempt`` before the call and an
``outcome`` after it. A lone ``attempt`` means the process died mid-request --
which is exactly the case where you need to know what was in flight, and
exactly the case a single after-the-fact record would lose. In SQLite that is
one row, inserted then updated.

That gap matters more here than on most APIs. The write path does not retry,
because a POST that timed out may already have applied at Meta's end and
nothing on this side can tell. So "intent with no outcome" is not a rare
crash artefact -- it is the designed signal for "go and look".

This log records what was SENT, including the ``validate_only`` passes that
changed nothing. For what the agent actually changed and how to undo it, see
``metaads_mcp/ledger.py`` and the ``changes`` table.
"""

from __future__ import annotations

from typing import Any

from .. import logdb


class AuditLog:
    """Appends write records to the ``mutations`` table."""

    def attempt(
        self,
        *,
        account_id: str,
        node_id: str,
        method: str,
        edge: str | None,
        payload: dict[str, Any],
    ) -> str:
        """Record an intent to write. Returns a correlation id."""
        correlation_id = logdb.new_correlation_id()
        logdb.insert_mutation(
            correlation_id=correlation_id,
            account_id=account_id,
            node_id=node_id,
            method=method,
            edge=edge,
            payload=payload,
        )
        return correlation_id

    def outcome(
        self,
        correlation_id: str,
        *,
        ok: bool,
        response: Any = None,
        error: str | None = None,
    ) -> None:
        logdb.complete_mutation(
            correlation_id, ok=ok, response=response, error=error
        )
