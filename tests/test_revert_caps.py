"""Reverts and the apply caps: a batch reverts at the cost of its apply, and a
revert that only lowers spend is never stranded by an apply-count cap."""

from __future__ import annotations

import pytest

from tests.fakes import ad
from tests.kindkit import rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim


def many_ads(n: int, status: str):
    s = make_sim()
    ids = [f"12020000000{i:04d}" for i in range(n)]
    for ad_id in ids:
        s.objects[ad_id] = ad(ad_id, name=f"Ad {ad_id[-4:]}", status=status)
    s.edges[ADSET_ID] = {"ads": ids}
    s.edges["act_1234567890"]["ads"] = ids
    return s, ids


def test_a_25_ad_activation_reverts_in_full(monkeypatch):
    s, ids = many_ads(25, "PAUSED")
    tools, state = rig(monkeypatch, s)  # 25 per call, 20 applies per session
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=ids, new_status="ACTIVE")
    assert not preview.blocked, [c for c in preview.checks if not c.startswith("OK")]
    result = tools["apply_multi_ad_status"](preview_token=preview.preview_token)
    undo = tools["revert_change"](entry_id=result.batch_id)
    assert len(undo.entry_ids) == 25
    assert all(s.objects[a]["status"] == "PAUSED" for a in ids), "nothing stranded ACTIVE"


def test_a_batch_revert_costs_once_and_is_checked_up_front(monkeypatch):
    s, ids = many_ads(3, "ACTIVE")
    tools, state = rig(monkeypatch, s, max_applies_per_session=2)
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=ids, new_status="PAUSED")
    result = tools["apply_multi_ad_status"](preview_token=preview.preview_token)
    tools["revert_change"](entry_id=result.batch_id)  # re-activates: a spend revert
    assert all(s.objects[a]["status"] == "ACTIVE" for a in ids)
    assert state.counters.applies == 2


def test_a_spend_raising_batch_revert_over_the_cap_sends_nothing(monkeypatch):
    s, ids = many_ads(3, "ACTIVE")
    tools, state = rig(monkeypatch, s, max_applies_per_session=1)
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=ids, new_status="PAUSED")
    result = tools["apply_multi_ad_status"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.batch_id)
    assert "session apply limit" in str(caught.value)
    assert all(s.objects[a]["status"] == "PAUSED" for a in ids), "refused whole, not halfway"


def test_a_pausing_revert_is_not_stopped_by_the_apply_caps(monkeypatch):
    s = make_sim()
    s.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    tools, state = rig(monkeypatch, s, max_applies_per_session=1, max_applies_per_day=1)
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    result = tools["apply_campaign_status"](preview_token=preview.preview_token)
    tools["revert_change"](entry_id=result.entry_id)
    assert s.objects[CAMPAIGN_ID]["status"] == "PAUSED"


def test_a_pausing_revert_still_stops_at_the_kill_switch(monkeypatch, tmp_path):
    s = make_sim()
    s.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    tools, _ = rig(monkeypatch, s, max_applies_per_session=1)
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    result = tools["apply_campaign_status"](preview_token=preview.preview_token)
    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("ops paused writes")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "writes are disabled" in str(caught.value)


def test_a_spend_raising_revert_still_answers_to_the_caps(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s, max_applies_per_session=1)
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tools["apply_campaign_status"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "session apply limit" in str(caught.value)
    assert s.objects[CAMPAIGN_ID]["status"] == "PAUSED"
