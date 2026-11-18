"""find_*: the whole account is searched, and a cut-off says so."""

from __future__ import annotations

import json

from tests.fakes import campaign
from tests.harness import build_tools
from tests.test_mcp_write_path import make_sim


def many(n):
    sim = make_sim()
    ids = []
    for i in range(n):
        cid = f"C{i:03d}"
        sim.objects[cid] = campaign(cid, name=f"Filler {i}")
        ids.append(cid)
    sim.objects["CBRAND"] = campaign("CBRAND", name="Example Brand Search")
    sim.edges["act_1234567890"]["campaigns"] = ids + ["CBRAND"]
    return sim


def test_a_match_past_the_limit_is_still_found(monkeypatch):
    """The search used to fetch `limit` rows and THEN filter by name."""
    registrar, _ = build_tools(many(60), monkeypatch=monkeypatch)
    out = registrar.tools["find_campaigns"](name_contains_text="brand", limit=5)
    assert [r["campaign_id"] for r in out["rows"]] == ["CBRAND"]
    assert out["truncated"] is False


def test_the_name_goes_to_meta_as_a_contain_filter(monkeypatch):
    sim = many(3)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    registrar.tools["find_campaigns"](name_contains_text="Brand")
    sent = [c["payload"].get("filtering") for c in sim.calls if "campaigns" in c["url"]]
    assert any(f and json.loads(f)[0]["operator"] == "CONTAIN" for f in sent)


def test_truncated_is_true_when_more_matched(monkeypatch):
    registrar, _ = build_tools(many(30), monkeypatch=monkeypatch)
    out = registrar.tools["find_campaigns"](limit=10)
    assert out["count"] == 10 and out["truncated"] is True


def test_status_is_an_effective_status_filter(monkeypatch):
    sim = many(2)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    registrar.tools["find_campaigns"](status="CAMPAIGN_PAUSED")
    sent = [c["payload"].get("effective_status") for c in sim.calls if "campaigns" in c["url"]]
    assert json.loads(sent[-1]) == ["CAMPAIGN_PAUSED"]


# --- run_report ---------------------------------------------------------------


def test_run_report_caps_rows_and_says_so(monkeypatch):
    from tests.fakes import insights_row

    sim = make_sim()
    sim.insights = [insights_row(date_start=f"2026-10-{i:02d}") for i in range(1, 31)]
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    out = registrar.tools["run_report"](report="campaigns", max_rows=10)
    assert out["rows_returned"] == 10 and out["truncated"] is True


def test_run_report_uses_a_preset_so_meta_picks_the_account_zone(monkeypatch):
    sim = make_sim()
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    registrar.tools["run_report"](report="campaigns", days=7)
    sent = [c["payload"] for c in sim.calls if c["url"].endswith("/insights")]
    assert sent[-1].get("date_preset") == "last_7d" and "time_range" not in sent[-1]
    assert "time_increment" not in sent[-1], "aggregate is the default"


def test_run_report_can_project_action_types(monkeypatch):
    registrar, _ = build_tools(make_sim(), monkeypatch=monkeypatch)
    out = registrar.tools["run_report"](report="campaigns", action_types=["purchase"])
    row = out["rows"][0]
    if "actions" in row:
        assert {a["action_type"] for a in row["actions"]} == {"purchase"}


def test_a_range_is_computed_in_the_accounts_zone():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from metaads_reporting.insights import window_for_days

    window = window_for_days(10, "Pacific/Kiritimati")
    yesterday = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date() - dt.timedelta(days=1)
    assert window["until"] == yesterday.isoformat()
