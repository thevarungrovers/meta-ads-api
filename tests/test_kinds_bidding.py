"""Lifetime budgets, bid strategies, ROAS floors, schedules, dayparting, caps."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from tests.kindkit import apply_and_revert, failed, preview_ok, rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim


def future(days):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(microsecond=0).isoformat()


def abo_sim(**adset):
    sim = make_sim()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    sim.objects[ADSET_ID].update(daily_budget="5000", **adset)
    return sim


def test_ad_set_lifetime_budget_round_trips(monkeypatch):
    sim = abo_sim(lifetime_budget="100000", end_time=future(10))
    sim.objects[ADSET_ID].pop("daily_budget")
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_lifetime_budget", sim, ad_set_id=ADSET_ID,
                         new_lifetime_budget="1200.00")
    assert preview.before == "1000.00 CAD" and preview.after == "1200.00 CAD"
    assert Decimal(preview.projected_daily_spend_delta) == pytest.approx(Decimal("20"), abs=Decimal("1"))
    apply_and_revert(tools, "ad_set_lifetime_budget", preview)
    assert sim.objects[ADSET_ID]["lifetime_budget"] == "100000"


def test_lifetime_budget_refuses_a_daily_ad_set(monkeypatch):
    tools, _ = rig(monkeypatch, abo_sim())
    with pytest.raises(Exception) as caught:
        tools["preview_ad_set_lifetime_budget"](ad_set_id=ADSET_ID, new_lifetime_budget="10.00")
    assert "not on a LIFETIME budget" in str(caught.value)


def test_a_bid_cap_strategy_needs_and_checks_its_bid(monkeypatch):
    sim = abo_sim()
    tools, _ = rig(monkeypatch, sim)
    with pytest.raises(Exception):
        tools["preview_ad_set_bid_strategy"](ad_set_id=ADSET_ID, strategy="COST_CAP")
    too_high = tools["preview_ad_set_bid_strategy"](
        ad_set_id=ADSET_ID, strategy="COST_CAP", bid_amount="80.00")
    assert failed(too_high, "max_cost_cap")
    preview = preview_ok(tools, "ad_set_bid_strategy", sim, ad_set_id=ADSET_ID,
                         strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="4.00")
    assert preview.after == "LOWEST_COST_WITH_BID_CAP | bid 4.00 CAD"
    apply_and_revert(tools, "ad_set_bid_strategy", preview)
    assert sim.objects[ADSET_ID]["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"


def test_min_roas_sets_the_floor_scaled_and_checks_the_minimum(monkeypatch):
    sim = abo_sim()
    tools, _ = rig(monkeypatch, sim)
    low = tools["preview_ad_set_bid_strategy"](
        ad_set_id=ADSET_ID, strategy="LOWEST_COST_WITH_MIN_ROAS", roas_floor="0.2")
    assert failed(low, "min_roas_floor")
    preview = preview_ok(tools, "ad_set_bid_strategy", sim, ad_set_id=ADSET_ID,
                         strategy="LOWEST_COST_WITH_MIN_ROAS", roas_floor="2.5")
    tools["apply_ad_set_bid_strategy"](preview_token=preview.preview_token)
    assert json.loads(sim.objects[ADSET_ID]["bid_constraints"]) == {"roas_average_floor": 25000}


def test_the_ad_set_strategy_refuses_under_campaign_budget(monkeypatch):
    tools, _ = rig(monkeypatch, make_sim())
    with pytest.raises(Exception) as caught:
        tools["preview_ad_set_bid_strategy"](ad_set_id=ADSET_ID, strategy="LOWEST_COST_WITHOUT_CAP")
    assert "preview_campaign_bid_strategy" in str(caught.value)


def test_removing_a_cap_projects_the_unused_budget(monkeypatch):
    sim = abo_sim(bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="300")
    sim.insights = []
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_ad_set_bid_strategy"](ad_set_id=ADSET_ID, strategy="LOWEST_COST_WITHOUT_CAP")
    assert preview.projected_daily_spend_delta == "50.00"


def test_campaign_bid_strategy_round_trips(monkeypatch):
    sim = make_sim()
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "campaign_bid_strategy", sim, campaign_id=CAMPAIGN_ID,
                         strategy="COST_CAP")
    apply_and_revert(tools, "campaign_bid_strategy", preview)
    assert sim.objects[CAMPAIGN_ID]["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"


def test_roas_floor_only_under_min_roas_and_is_stale_checked(monkeypatch):
    sim = abo_sim(bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
                  bid_constraints={"roas_average_floor": 20000})
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_roas_floor", sim, ad_set_id=ADSET_ID, new_roas_floor="2.4")
    assert preview.before == "ROAS floor 2.00" and preview.after == "ROAS floor 2.40"
    sim.objects[ADSET_ID]["bid_constraints"] = {"roas_average_floor": 30000}
    with pytest.raises(Exception) as caught:
        tools["apply_ad_set_roas_floor"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)

    other = abo_sim()
    tools2, _ = rig(monkeypatch, other)
    with pytest.raises(Exception):
        tools2["preview_ad_set_roas_floor"](ad_set_id=ADSET_ID, new_roas_floor="2")


def test_schedule_end_time_round_trips_and_start_is_refused_after_launch(monkeypatch):
    sim = abo_sim(start_time="2026-01-01T00:00:00+0000", end_time=future(20))
    tools, _ = rig(monkeypatch, sim)
    original = sim.objects[ADSET_ID]["end_time"]
    preview = preview_ok(tools, "ad_set_schedule", sim, ad_set_id=ADSET_ID, end_time=future(40))
    result, _ = apply_and_revert(tools, "ad_set_schedule", preview)
    assert sim.objects[ADSET_ID]["end_time"] == original
    with pytest.raises(Exception) as caught:
        tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, start_time=future(2))
    assert "already started" in str(caught.value)
    with pytest.raises(Exception):
        tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, end_time="2020-01-01T00:00:00+00:00")


def test_shortening_a_lifetime_flight_raises_the_daily_pace(monkeypatch):
    sim = abo_sim(lifetime_budget="100000", end_time=future(20))
    sim.objects[ADSET_ID].pop("daily_budget")
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, end_time=future(10))
    assert Decimal(preview.projected_daily_spend_delta) > 0


def test_dayparting_refuses_a_daily_budget(monkeypatch):
    tools, _ = rig(monkeypatch, abo_sim())
    with pytest.raises(Exception) as caught:
        tools["preview_ad_set_dayparting"](ad_set_id=ADSET_ID, schedule=[
            {"days": [1, 2], "start_minute": 480, "end_minute": 1200}])
    assert "LIFETIME budget" in str(caught.value)


def test_dayparting_on_a_lifetime_budget(monkeypatch):
    sim = abo_sim(lifetime_budget="100000", end_time=future(20))
    sim.objects[ADSET_ID].pop("daily_budget")
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_dayparting", sim, ad_set_id=ADSET_ID, schedule=[
        {"days": [1, 2], "start_minute": 480, "end_minute": 1200}])
    tools["apply_ad_set_dayparting"](preview_token=preview.preview_token)
    assert json.loads(sim.objects[ADSET_ID]["pacing_type"]) == ["day_parting"]
    with pytest.raises(Exception):
        tools["preview_ad_set_dayparting"](ad_set_id=ADSET_ID, schedule=[
            {"days": [9], "start_minute": 30, "end_minute": 20}])


def test_frequency_cap_only_where_meta_honours_it(monkeypatch):
    tools, _ = rig(monkeypatch, abo_sim())
    with pytest.raises(Exception) as caught:
        tools["preview_ad_set_frequency_cap"](ad_set_id=ADSET_ID, max_frequency=3, interval_days=7)
    assert "accepted and ignored" in str(caught.value)
    sim = abo_sim(optimization_goal="REACH")
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_frequency_cap", sim, ad_set_id=ADSET_ID,
                         max_frequency=3, interval_days=7)
    result = tools["apply_ad_set_frequency_cap"](preview_token=preview.preview_token)
    assert result.applied
    assert tools["reconcile_ledger"]()["matching"] == 1


def test_read_only_mode_publishes_no_apply_for_any_kind(monkeypatch):
    from metaads_mcp.changes import all_kinds
    from tests.harness import Registrar

    monkeypatch.setenv("META_ADS_MCP_READ_ONLY", "1")
    from metaads_mcp import tools_read, tools_write
    from metaads_mcp.guardrails import DEFAULTS, Limits, SessionCounters
    from metaads_mcp.previews import PreviewStore
    from metaads_mcp.server import ServerState
    import metaads_reporting.graph as graph

    sim = make_sim()
    monkeypatch.setattr(graph.requests, "Session", lambda: sim)
    limits = Limits(**DEFAULTS)
    state = ServerState(limits=limits, counters=SessionCounters(limits), previews=PreviewStore())
    registrar = Registrar()
    tools_read.register(registrar, state)
    tools_write.register(registrar, state)
    names = set(registrar.tools)
    for kind in all_kinds():
        assert f"preview_{kind.name}" in names
        assert f"apply_{kind.name}" not in names, kind.name
    assert "revert_change" not in names
