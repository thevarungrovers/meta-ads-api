"""find_* / list_* naming filters and attribution, and reconcile_ledger's
naming section. Placeholders only: AB / max, CD / bot."""

from __future__ import annotations

import pytest

from metaads_reporting.naming import Identity
from tests.fakes import ad_set, campaign
from tests.kindkit import as_identity, rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim

C_OURS, C_OTHER, C_CLI, C_OWNER = "120200000000071", "120200000000072", "120200000000073", "120200000000074"


def named_sim():
    s = make_sim()
    for cid, name in ((C_OURS, "AB-max-spring"), (C_OTHER, "CD-bot-launch"),
                      (C_CLI, "AB-cli-QC 25-54"), (C_OWNER, "AB-summer")):
        s.objects[cid] = campaign(cid, name=name)
        s.edges["act_1234567890"]["campaigns"].append(cid)
    return s


def ids(out):
    return {r["campaign_id"] for r in out["rows"]}


def test_rows_carry_attribution_and_ledger_created(monkeypatch):
    tools, _ = rig(monkeypatch, named_sim())
    rows = {r["campaign_id"]: r for r in tools["find_campaigns"]()["rows"]}
    assert rows[C_OURS]["origin"] == "agent" and rows[C_OURS]["owner"] == "AB" and rows[C_OURS]["agent"] == "max"
    assert rows[C_CLI]["origin"] == "cli"
    assert rows[CAMPAIGN_ID]["origin"] == "manual" and rows[CAMPAIGN_ID]["base"] is None
    assert rows[C_OURS]["ledger_created"] is False


def test_mine_is_only_our_full_prefix(monkeypatch):
    tools, _ = rig(monkeypatch, named_sim())
    assert ids(tools["find_campaigns"](mine=True)) == {C_OURS}


def test_mine_with_initials_only_does_not_claim_other_agents_or_the_cli(monkeypatch):
    tools, state = rig(monkeypatch, named_sim())
    as_identity(state, Identity("AB", ""))
    assert ids(tools["find_campaigns"](mine=True)) == {C_OWNER}


@pytest.mark.parametrize("kwargs, expected", [
    ({"origin": "manual"}, {CAMPAIGN_ID, C_OWNER}),
    ({"origin": "cli"}, {C_CLI}),
    ({"origin": "agent"}, {C_OURS, C_OTHER}),
    ({"owner": "CD"}, {C_OTHER}),
    ({"agent_name": "bot"}, {C_OTHER}),
    ({"owner": "AB", "agent_name": "cli"}, {C_CLI}),
])
def test_the_filters(monkeypatch, kwargs, expected):
    tools, _ = rig(monkeypatch, named_sim())
    assert ids(tools["find_campaigns"](**kwargs)) == expected


@pytest.mark.parametrize("kwargs", [{"owner": "ab"}, {"owner": "A\"B"}, {"agent_name": "max-1"},
                                    {"agent_name": "x\" OR"}, {"origin": "robot"}])
def test_an_invalid_filter_is_refused_before_any_request(monkeypatch, kwargs):
    s = named_sim()
    tools, _ = rig(monkeypatch, s)
    before = len(s.calls)
    with pytest.raises(Exception):
        tools["find_campaigns"](**kwargs)
    assert len(s.calls) == before


def test_mine_needs_an_identity(monkeypatch):
    tools, state = rig(monkeypatch, named_sim())
    as_identity(state, Identity())
    with pytest.raises(Exception, match="mine=True needs"):
        tools["find_campaigns"](mine=True)


def test_find_ad_sets_and_ads_filter_too(monkeypatch):
    s = named_sim()
    s.objects["ADSET_OURS"] = ad_set("ADSET_OURS", name="AB-max-broad", campaign_id=CAMPAIGN_ID)
    s.edges["act_1234567890"]["adsets"].append("ADSET_OURS")
    tools, _ = rig(monkeypatch, s)
    assert [r["ad_set_id"] for r in tools["find_ad_sets"](mine=True)["rows"]] == ["ADSET_OURS"]
    assert tools["find_ads"](origin="agent")["rows"] == []


def test_list_labels_filters(monkeypatch):
    s = make_sim()
    s.objects["L1"] = {"id": "L1", "name": "AB-max-spring"}
    s.objects["L2"] = {"id": "L2", "name": "seasonal"}
    s.edges["act_1234567890"]["adlabels"] = ["L1", "L2"]
    tools, _ = rig(monkeypatch, s)
    assert [r["id"] for r in tools["list_labels"](mine=True)["rows"]] == ["L1"]
    assert [r["id"] for r in tools["list_labels"](origin="manual")["rows"]] == ["L2"]


# --- reconcile_ledger: naming ------------------------------------------------------------


def _create(tools, name="mine"):
    return tools["apply_create_campaign"](preview_token=tools["preview_create_campaign"](
        name=name, objective="OUTCOME_SALES", special_ad_categories=[],
        budget_level="adset").preview_token)


def test_reconcile_reports_a_lost_prefix_and_a_prefix_with_no_ledger_row(monkeypatch):
    s = named_sim()
    tools, _ = rig(monkeypatch, s)
    created = _create(tools)
    s.objects[created.entity_id]["name"] = "renamed by hand"
    naming = tools["reconcile_ledger"]()["naming"]
    assert naming["prefix_lost"] == [{"entity_id": created.entity_id, "live_name": "renamed by hand",
                                      "pre_feature": False}]
    assert [r["entity_id"] for r in naming["prefix_without_ledger"]] == [C_OURS]
    assert naming["other_prefixes"] == {"CD-bot-": 1, "AB-cli-": 1}


def test_reconcile_skips_naming_without_an_identity(monkeypatch):
    tools, state = rig(monkeypatch, named_sim())
    as_identity(state, Identity())
    assert "skipped" in tools["reconcile_ledger"]()["naming"]
