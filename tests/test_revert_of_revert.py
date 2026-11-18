"""Reverting a revert re-applies the original change, and reconcile sees reverts.

A revert row used to copy the ORIGINAL entry's inverse, so reverting it
re-sent the values that were already live: a no-op the ledger recorded as
applied.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import build_tools
from tests.kindkit import preview_ok, rig
from tests.test_kinds_creative_options import sim as creative_sim
from tests.test_kinds_targeting import BASE, _targeting
from tests.test_kinds_targeting import sim as targeting_sim
from tests.test_mcp_creative_path import AD_ID, CREATIVE_ID
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim


def _undo_twice(tools, entry_id):
    first = tools["revert_change"](entry_id=entry_id)
    return first, tools["revert_change"](entry_id=first.entry_id)


def test_reverting_a_budget_revert_reapplies_the_budget(monkeypatch):
    s = make_sim()
    registrar, _ = build_tools(s, monkeypatch=monkeypatch)
    tools = registrar.tools
    preview = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN_ID,
                                                     new_daily_budget="260.00")
    result = tools["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    first, second = _undo_twice(tools, result.entry_id)
    assert str(s.objects[CAMPAIGN_ID]["daily_budget"]) == "26000", "the increase is back"
    assert second.after == "260.00 CAD"
    third = tools["revert_change"](entry_id=second.entry_id)
    assert str(s.objects[CAMPAIGN_ID]["daily_budget"]) == "25000"
    assert third.after == "250.00 CAD"


def test_reverting_a_targeting_revert_reattaches_the_audience(monkeypatch):
    s = targeting_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    _undo_twice(tools, result.entry_id)
    assert _targeting(s)["custom_audiences"] == [{"id": "A"}]
    assert _targeting(s)["flexible_spec"] == BASE["flexible_spec"]


def test_reverting_a_creative_revert_points_at_the_new_creative_again(monkeypatch):
    s = creative_sim()
    s.objects["CR2"] = {**s.objects[CREATIVE_ID], "id": "CR2"}
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "replace_ad_creative", s, ad_id=AD_ID, creative_id="CR2")
    result = tools["apply_replace_ad_creative"](preview_token=preview.preview_token)
    _undo_twice(tools, result.entry_id)
    assert json.loads(s.objects[AD_ID]["creative"]) == {"creative_id": "CR2"}


def test_reverting_a_bidding_revert_reapplies_it(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID].update(bid_strategy="LOWEST_COST_WITHOUT_CAP", optimization_goal="REACH",
                           frequency_control_specs=[{"event": "IMPRESSIONS", "interval_days": 7,
                                                     "max_frequency": 5}])
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_frequency_cap"](ad_set_id=ADSET_ID, max_frequency=2,
                                                    interval_days=7)
    if preview.blocked:
        pytest.skip(f"frequency cap preview blocked in this fixture: {preview.checks}")
    result = tools["apply_ad_set_frequency_cap"](preview_token=preview.preview_token)
    sent = s.objects[ADSET_ID]["frequency_control_specs"]
    tools["revert_change"](entry_id=result.entry_id)
    undone = s.objects[ADSET_ID].get("frequency_control_specs")
    first = [r for r in tools["list_my_changes"]() if r["tool"] == "revert_change"][0]
    tools["revert_change"](entry_id=first["entry_id"])
    assert s.objects[ADSET_ID]["frequency_control_specs"] == sent != undone


def test_reconcile_checks_revert_rows(monkeypatch):
    s = make_sim()
    registrar, _ = build_tools(s, monkeypatch=monkeypatch)
    tools = registrar.tools
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tools["apply_campaign_status"](preview_token=preview.preview_token)
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert tools["reconcile_ledger"]()["matching"] == 1

    s.objects[CAMPAIGN_ID]["status"] = "PAUSED"  # someone in Ads Manager
    out = tools["reconcile_ledger"]()
    assert [d["entry_id"] for d in out["drift"]] == [undo.entry_id]


# --- a field the original change CLEARED --------------------------------------


def test_reverting_a_revert_also_re_clears_a_field_the_original_cleared(monkeypatch):
    """The original emptied adset_schedule and switched pacing. Reverting it
    puts both back; reverting THAT must empty adset_schedule again, not only
    re-send the pacing: a live value of None is an explicit clear."""
    from metaads_mcp.changes.common import as_obj, canonical
    from metaads_reporting import logdb
    from tests.test_kinds_bidding import abo_sim

    schedule = [{"days": [1, 2], "start_minute": 540, "end_minute": 1020}]
    s = abo_sim(lifetime_budget="100000", pacing_type=["standard"])
    s.objects[ADSET_ID].pop("daily_budget")
    tools, _ = rig(monkeypatch, s)
    logdb.insert_change(
        entry_id="cleared1", tool="apply_ad_set_dayparting", account_id="1234567890",
        entity_type="ad set", entity_id=ADSET_ID, entity_name="Example ad set",
        field_name="dayparting", before=canonical(schedule), after=canonical(None),
        projected_delta="0",
        inverse={"kind": "ad_set_dayparting", "weight": 1,
                 "previous": {"adset_schedule": schedule, "pacing_type": ["day_parting"]}},
    )
    logdb.complete_change(entry_id="cleared1", ok=True)

    first = tools["revert_change"](entry_id="cleared1")
    assert as_obj(s.objects[ADSET_ID]["adset_schedule"]) == schedule
    tools["revert_change"](entry_id=first.entry_id)
    assert as_obj(s.objects[ADSET_ID]["adset_schedule"]) in (None, []), "the clear was re-applied"
    assert as_obj(s.objects[ADSET_ID]["pacing_type"]) == ["standard"]
