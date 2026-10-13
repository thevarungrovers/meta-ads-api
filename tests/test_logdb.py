"""The log database: two error policies, two phases, and the shape revert reads."""

from __future__ import annotations

import pytest

from metaads_reporting import logdb


def rows(table: str) -> list[dict]:
    return [dict(r) for r in logdb.connect().execute(f"SELECT * FROM {table}")]


# --- schema -----------------------------------------------------------------


def test_the_schema_is_created_on_first_use():
    names = {
        r["name"]
        for r in logdb.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"tool_calls", "api_calls", "changes", "mutations", "sessions"} <= names


def test_wal_is_on_so_a_cli_can_write_while_the_server_reads():
    mode = logdb.connect().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# --- observability never raises ---------------------------------------------


def test_a_broken_logger_does_not_break_the_tool(monkeypatch):
    """The MCP server's stderr is invisible in normal use, so a logging error
    would be both fatal and silent."""
    monkeypatch.setattr(logdb, "connect", _boom)
    with logdb.record_tool_call("whatever", {"a": 1}):
        pass  # must not raise
    with logdb.record_api_call("GET", path="/x"):
        pass


def _boom(*args, **kwargs):
    raise RuntimeError("database is on fire")


def test_a_tool_call_is_recorded_with_its_outcome():
    with logdb.record_tool_call("find_campaigns", {"limit": 5}):
        pass
    row = rows("tool_calls")[0]
    assert row["tool_name"] == "find_campaigns"
    assert row["ok"] == 1
    assert row["duration_ms"] is not None


def test_a_failing_tool_call_is_recorded_as_failed_and_re_raises():
    with pytest.raises(ValueError):
        with logdb.record_tool_call("x"):
            raise ValueError("nope")
    row = rows("tool_calls")[0]
    assert row["ok"] == 0
    assert "nope" in row["error"]


def test_an_api_call_is_linked_to_the_tool_call_that_caused_it():
    with logdb.record_tool_call("run_report"):
        with logdb.record_api_call("GET", path="/act_1/insights") as slot:
            slot["row_count"] = 12
            slot["status"] = 200
            slot["rate_limit_pct"] = 42.0

    tool_row = rows("tool_calls")[0]
    api_row = rows("api_calls")[0]
    assert api_row["tool_call_id"] == tool_row["id"]
    assert api_row["row_count"] == 12
    assert api_row["http_status"] == 200
    assert api_row["rate_limit_pct"] == 42.0


def test_an_abandoned_generator_is_not_recorded_as_a_failure():
    """A `break` over a paged read is a clean close, not a broken API call."""

    def reader():
        with logdb.record_api_call("GET", path="/x") as slot:
            slot["row_count"] = 1
            yield 1
            yield 2

    gen = reader()
    next(gen)
    gen.close()
    assert rows("api_calls")[0]["ok"] == 1


# --- the ledger DOES raise --------------------------------------------------


def test_a_ledger_write_failure_is_not_swallowed(monkeypatch):
    """These tables are the only record of a real-money mutation. Losing a row
    quietly is worse than failing the write that would have produced it."""
    monkeypatch.setattr(logdb, "connect", _boom)
    with pytest.raises(RuntimeError):
        logdb.insert_change(
            entry_id="x",
            tool="t",
            account_id="1",
            entity_type="campaign",
            entity_id="2",
            entity_name="n",
            field_name="status",
            before="ACTIVE",
            after="PAUSED",
            projected_delta="0",
        )


def insert(entry_id: str, **overrides) -> None:
    payload = dict(
        entry_id=entry_id,
        tool="apply_campaign_status",
        account_id="1234567890",
        entity_type="campaign",
        entity_id="999",
        entity_name="Brand",
        field_name="status",
        before="ACTIVE",
        after="PAUSED",
        projected_delta="0",
    )
    payload.update(overrides)
    logdb.insert_change(**payload)


def test_an_intent_with_no_outcome_reads_as_not_applied():
    """The dangerous row: the write left this process and nothing recorded how
    it ended. On Meta that is not rare -- the write path does not retry, because
    a POST that timed out may already have landed."""
    insert("aaa")
    entry = logdb.find_change("aaa")
    assert entry["applied"] is False
    assert entry["error"] is None


def test_an_outcome_updates_the_same_row_rather_than_adding_one():
    insert("aaa")
    logdb.complete_change(entry_id="aaa", ok=True, resource_name="999")
    assert len(logdb.change_entries()) == 1
    assert logdb.find_change("aaa")["applied"] is True


def test_entries_come_back_oldest_first():
    """Load-bearing: list_my_changes slices [-limit:] to get the most recent,
    which silently returns the OLDEST if this is reversed."""
    for entry_id in ("a1", "a2", "a3"):
        insert(entry_id)
    assert [e["entry_id"] for e in logdb.change_entries()] == ["a1", "a2", "a3"]


def test_the_entry_shape_is_the_contract_revert_change_reads():
    insert("aaa")
    logdb.complete_change(entry_id="aaa", ok=True)
    entry = logdb.find_change("aaa")
    for key in (
        "entry_id", "at", "tool", "account_id", "entity_type", "entity_id",
        "entity_name", "field", "before", "after", "applied", "error", "reverts",
    ):
        assert key in entry, key


def test_a_reverted_entry_is_reported_as_reverted():
    insert("aaa")
    logdb.complete_change(entry_id="aaa", ok=True)
    insert("bbb", tool="revert_change", before="PAUSED", after="ACTIVE")
    logdb.complete_change(entry_id="bbb", ok=True, reverts="aaa")
    assert logdb.reverted_entry_ids() == {"aaa"}


def test_a_failed_revert_still_blocks_a_second_attempt():
    """The safe direction: the first may yet have landed at Meta's end, and the
    write path does not retry precisely because it cannot tell."""
    insert("aaa")
    logdb.complete_change(entry_id="aaa", ok=True)
    insert("bbb", tool="revert_change")
    logdb.complete_change(entry_id="bbb", ok=False, error="boom", reverts="aaa")
    assert "aaa" in logdb.reverted_entry_ids()


def test_an_unknown_entry_is_none():
    assert logdb.find_change("nope") is None


# --- the mutation log -------------------------------------------------------


def test_a_mutation_records_its_payload_and_its_outcome():
    correlation = logdb.new_correlation_id()
    logdb.insert_mutation(
        correlation_id=correlation,
        account_id="1234567890",
        node_id="999",
        method="POST",
        edge=None,
        payload={"status": "PAUSED"},
    )
    logdb.complete_mutation(correlation, ok=True, response={"success": True})
    row = logdb.mutation_rows()[0]
    assert row["payload"] == {"status": "PAUSED"}
    assert row["response"] == {"success": True}
    assert row["ok"] == 1


def test_a_huge_payload_is_truncated_rather_than_stored_whole():
    """A targeting spec is large and not worth an unbounded row."""
    correlation = logdb.new_correlation_id()
    logdb.insert_mutation(
        correlation_id=correlation,
        account_id="1",
        node_id="2",
        method="POST",
        edge=None,
        payload={"big": "x" * 20000},
    )
    stored = rows("mutations")[0]["payload_json"]
    assert len(stored) < 20000
    assert "truncated" in stored


# --- ids --------------------------------------------------------------------


def test_entry_ids_are_unique():
    assert len({logdb.new_entry_id() for _ in range(200)}) == 200


def test_correlation_ids_are_unique():
    assert len({logdb.new_correlation_id() for _ in range(200)}) == 200
