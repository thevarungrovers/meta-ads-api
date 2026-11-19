"""What activating something can spend, and which cap it answers to."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from metaads_mcp.guardrails import DEFAULTS, Limits
from metaads_reporting import logdb
from tests.fakes import ad, ad_set
from tests.harness import build_tools
from tests.test_mcp_write_path import AD_ID, ADSET_ID, CAMPAIGN_ID, make_sim


def lim(**over):
    return Limits(**{**DEFAULTS, **over})


def used_today(amount: str):
    logdb.connect().execute(
        "INSERT INTO changes(entry_id, at, tool, projected_daily_spend_delta, ok) "
        "VALUES('earlier', ?, 'apply_ad_set_daily_budget', ?, 1)",
        (dt.datetime.now(dt.timezone.utc).isoformat(), amount),
    )


def paused_cbo(budget_minor="10000"):
    sim = make_sim()
    sim.objects[CAMPAIGN_ID].update(status="PAUSED", daily_budget=budget_minor)
    return sim


def test_activating_a_100_a_day_campaign_with_nothing_used_passes(monkeypatch):
    registrar, _ = build_tools(paused_cbo(), limits=lim(
        max_projected_delta_per_day_total=Decimal("150")), monkeypatch=monkeypatch)
    preview = registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    assert preview.blocked is False
    assert preview.projected_daily_spend_delta == "100.00"
    assert any("uses 100.00 of today's remaining 150.00" in w for w in preview.warnings)


def test_with_60_used_of_150_the_activation_is_refused_naming_both(monkeypatch):
    used_today("60")
    registrar, _ = build_tools(paused_cbo(), limits=lim(
        max_projected_delta_per_day_total=Decimal("150")), monkeypatch=monkeypatch)
    preview = registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    assert preview.blocked is True
    line = next(c for c in preview.checks if "max_projected_delta_per_day_total" in c)
    assert line == "FAIL max_projected_delta_per_day_total: uses 100.00 of today's remaining 90.00"


def test_an_abo_campaign_is_bounded_by_its_ad_sets_budgets(monkeypatch):
    sim = make_sim()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    sim.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    sim.objects[ADSET_ID]["daily_budget"] = "4000"
    sim.objects["ADSET2"] = ad_set("ADSET2", daily_budget="6000")
    sim.edges[CAMPAIGN_ID]["adsets"].append("ADSET2")
    sim.insights = []
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    assert preview.projected_daily_spend_delta == "100.00"
    assert any("summed budgets of its 2 ACTIVE ad set" in w for w in preview.warnings)


def test_a_lifetime_budget_is_spread_over_the_days_left(monkeypatch):
    sim = make_sim()
    end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10, hours=1)).isoformat()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    sim.objects[CAMPAIGN_ID].update(status="PAUSED", lifetime_budget="100000", stop_time=end)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    assert Decimal(preview.projected_daily_spend_delta) == pytest.approx(Decimal("100"), abs=1)


def test_an_ad_set_under_a_campaign_budget_is_bounded_by_it(monkeypatch):
    sim = make_sim()
    sim.objects[ADSET_ID]["status"] = "PAUSED"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_ad_set_status"](ad_set_id=ADSET_ID, new_status="ACTIVE")
    assert preview.projected_daily_spend_delta == "250.00"


def test_the_only_active_ad_restarts_its_ad_set(monkeypatch):
    sim = make_sim()
    sim.objects[AD_ID]["status"] = "PAUSED"
    sim.objects[ADSET_ID]["daily_budget"] = "5000"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_ad_status"](ad_id=AD_ID, new_status="ACTIVE")
    assert preview.projected_daily_spend_delta == "50.00"


def test_an_ad_beside_active_siblings_projects_zero(monkeypatch):
    sim = make_sim()
    sim.objects[AD_ID]["status"] = "PAUSED"
    sim.objects["AD2"] = ad("AD2")
    sim.edges[ADSET_ID]["ads"].append("AD2")
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_ad_status"](ad_id=AD_ID, new_status="ACTIVE")
    assert preview.projected_daily_spend_delta == "0.00"


def test_reverting_a_pause_projects_the_spend_it_restores(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, limits=lim(
        max_projected_delta_per_day_total=Decimal("200")), monkeypatch=monkeypatch)
    tool = registrar.tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)
    # A 250/day campaign resumed is over a 200 daily total: refused.
    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "daily spend total reached" in str(caught.value)


def test_a_revert_that_restores_spend_is_counted(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, monkeypatch=monkeypatch)
    tool = registrar.tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)
    undo = tool["revert_change"](entry_id=result.entry_id)
    assert undo.projected_daily_spend_delta == "250.00"
    assert state.counters.projected_delta == Decimal("250.00")


def test_startup_warns_about_a_budget_over_the_daily_total(monkeypatch):
    from metaads_mcp.server import budget_cap_warnings

    registrar, state = build_tools(make_sim(), limits=lim(
        max_projected_delta_per_day_total=Decimal("100")), monkeypatch=monkeypatch)
    warnings = budget_cap_warnings(state)
    assert len(warnings) == 1 and "250.00/day" in warnings[0]


def test_the_startup_check_reads_ad_sets_once_for_the_whole_account(monkeypatch):
    # One ad-set request per campaign at every start burned the account's
    # rate limit before the agent made a single call of its own.
    from tests.fakes import campaign
    from metaads_mcp.server import budget_cap_warnings

    sim = make_sim()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    sim.objects[ADSET_ID]["daily_budget"] = "30000"
    sim.objects[ADSET_ID]["status"] = "ACTIVE"
    for n in range(2, 7):
        cid, aid = f"1203000000000{n}", f"ADSET_{n}"
        sim.objects[cid] = campaign(cid, daily_budget=None)
        sim.objects[cid].pop("daily_budget", None)
        sim.objects[aid] = ad_set(aid, daily_budget="1000", campaign_id=cid)
        sim.edges["act_1234567890"]["campaigns"].append(cid)
        sim.edges["act_1234567890"]["adsets"].append(aid)
        sim.edges[cid] = {"adsets": [aid]}
    _, state = build_tools(sim, limits=lim(
        max_projected_delta_per_day_total=Decimal("200")), monkeypatch=monkeypatch)
    sim.calls.clear()
    warnings = budget_cap_warnings(state)
    assert len(warnings) == 1 and CAMPAIGN_ID in warnings[0] and "300.00/day" in warnings[0]
    adset_reads = [c for c in sim.calls if "adsets" in c["url"]]
    assert len(adset_reads) == 1
