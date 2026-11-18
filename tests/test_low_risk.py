"""The general low-risk tool: a closed allowlist of names and labels."""

from __future__ import annotations

import json

import pytest

from metaads_mcp.changes.low_risk import ALLOWED
from metaads_reporting.fields import MAJOR_UNIT_FIELDS, MINOR_UNIT_FIELDS
from tests.kindkit import preview_ok, rig
from tests.test_mcp_write_path import AD_ID, ADSET_ID, CAMPAIGN_ID, make_sim

FORBIDDEN = ("budget", "bid", "status", "targeting", "optimization", "billing",
             "special_ad", "schedule", "start_time", "end_time", "promoted_object", "creative")


def test_the_allowlist_is_exactly_names_and_labels():
    assert set(ALLOWED.values()) == {"name", "adlabels"}
    for (_, field), graph_field in ALLOWED.items():
        for name in (field, graph_field):
            assert name not in MINOR_UNIT_FIELDS and name not in MAJOR_UNIT_FIELDS
            assert not any(word in name for word in FORBIDDEN), name


def test_url_tags_is_not_low_risk():
    assert not any("url_tags" in f for _, f in ALLOWED)


def test_a_rename_round_trips(monkeypatch):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "low_risk_change", s, entity_type="ad_set", entity_id=ADSET_ID,
                         field="name", value="Broad 25-54 (spring)")
    result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert s.objects[ADSET_ID]["name"] == "Broad 25-54 (spring)"
    assert state.counters.applies == 1
    tools["revert_change"](entry_id=result.entry_id)
    assert s.objects[ADSET_ID]["name"] == "Broad 25-54"


def test_a_label_is_created_attached_and_detached(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    created = tools["apply_low_risk_change"](preview_token=preview_ok(
        tools, "low_risk_change", s, entity_type="account", entity_id="-",
        field="label_create", value="spring").preview_token)
    label = created.entity_id
    attach = preview_ok(tools, "low_risk_change", s, entity_type="campaign", entity_id=CAMPAIGN_ID,
                        field="label_attach", value=label)
    result = tools["apply_low_risk_change"](preview_token=attach.preview_token)
    assert json.loads(s.objects[CAMPAIGN_ID]["adlabels"]) == [{"id": label}]
    tools["revert_change"](entry_id=result.entry_id)
    assert json.loads(s.objects[CAMPAIGN_ID]["adlabels"]) == []
    with pytest.raises(Exception):
        tools["revert_change"](entry_id=created.entry_id)


@pytest.mark.parametrize("entity_type, field", [("ad", "label_create"), ("account", "name")])
def test_a_pair_off_the_allowlist_is_refused(monkeypatch, entity_type, field):
    tools, _ = rig(monkeypatch, make_sim())
    with pytest.raises(Exception) as caught:
        tools["preview_low_risk_change"](entity_type=entity_type, entity_id=AD_ID, field=field, value="x")
    assert "allowlist" in str(caught.value)
