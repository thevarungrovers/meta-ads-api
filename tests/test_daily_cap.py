"""The daily change cap: counted from the ledger, so it survives a restart."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from metaads_mcp.guardrails import DEFAULTS, DailyUsage, Limits, local_midnight_utc
from metaads_reporting import logdb
from tests.harness import build_tools
from tests.test_mcp_write_path import AD_ID, CAMPAIGN_ID, make_sim


def row(entry_id, at, *, ok=1, tool="apply_campaign_status", delta="0", inverse=None):
    logdb.connect().execute(
        "INSERT INTO changes(entry_id, at, tool, projected_daily_spend_delta, ok, inverse_json) "
        "VALUES(?,?,?,?,?,?)",
        (entry_id, at, tool, delta, ok, None if inverse is None else __import__("json").dumps(inverse)),
    )


def limits(**over):
    return Limits(**{**DEFAULTS, **over})


def test_the_boundary_is_local_midnight_converted_to_utc():
    eastern = dt.timezone(dt.timedelta(hours=-5))
    assert local_midnight_utc(dt.datetime(2026, 11, 18, 23, 30, tzinfo=eastern)) == (
        "2026-11-18T05:00:00+00:00"
    )


def test_a_row_from_late_yesterday_is_not_counted():
    now = dt.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_2359 = (midnight - dt.timedelta(minutes=1)).astimezone(dt.timezone.utc)
    row("old", yesterday_2359.isoformat(), delta="50")
    row("new", dt.datetime.now(dt.timezone.utc).isoformat(), delta="20")
    applies, delta = DailyUsage(limits()).usage()
    assert (applies, delta) == (1, Decimal("20"))


def test_failed_and_unfinished_rows_are_not_counted():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    row("failed", now, ok=0, delta="99")
    row("ok", now, delta="5")
    logdb.connect().execute(
        "INSERT INTO changes(entry_id, at, tool) VALUES('inflight', ?, 'apply_x')", (now,)
    )
    assert DailyUsage(limits()).usage() == (1, Decimal("5"))


def test_a_batch_counts_once_and_a_creation_its_weight():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for i in range(3):
        row(f"b{i}", now, delta="10", inverse={"batch_id": "B1", "weight": 1})
    row("create", now, inverse={"weight": 3})
    assert DailyUsage(limits()).usage() == (4, Decimal("10"))


def test_reads_and_other_tools_are_not_counted():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    row("r", now, tool="revert_change")
    row("x", now, tool="something_else")
    assert DailyUsage(limits()).usage()[0] == 1


def test_two_server_processes_share_one_day(monkeypatch):
    """Two build_tools() calls are two processes' worth of state on one DB."""
    tight = limits(max_applies_per_day=1)
    first, _ = build_tools(make_sim(), limits=tight, monkeypatch=monkeypatch)
    preview = first.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    first.tools["apply_campaign_status"](preview_token=preview.preview_token)

    second, state = build_tools(make_sim(), limits=tight, monkeypatch=monkeypatch)
    assert state.counters.applies == 0, "a fresh session"
    preview = second.tools["preview_ad_status"](ad_id=AD_ID, new_status="PAUSED")
    assert any("REFUSED: daily apply limit" in w for w in preview.warnings)
    with pytest.raises(Exception) as caught:
        second.tools["apply_ad_status"](preview_token=preview.preview_token)
    assert "daily apply limit reached" in str(caught.value)


def test_the_daily_total_line_names_both_figures(monkeypatch):
    registrar, _ = build_tools(
        make_sim(), limits=limits(max_projected_delta_per_day_total=Decimal("30")),
        monkeypatch=monkeypatch,
    )
    preview = registrar.tools["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    line = next(c for c in preview.checks if "max_projected_delta_per_day_total" in c)
    assert line.startswith("FAIL") and "uses 50.00 of today's remaining 30.00" in line
    assert preview.blocked is True


def test_get_guardrails_reports_today(monkeypatch):
    registrar, _ = build_tools(make_sim(), monkeypatch=monkeypatch)
    out = registrar.tools["get_guardrails"]()
    assert out["today"]["applies_remaining"] == DEFAULTS["max_applies_per_day"]
    assert "manage.py" in out["today"]["note"]
