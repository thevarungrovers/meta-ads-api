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


# --- a label write that does not replace the set -------------------------------


def _append_only_labels(s):
    """Meta as the review feared: POST adlabels ADDS, it never removes."""
    original = s._post

    def post(segments, body):
        node = segments[0]
        before = s.objects.get(node, {}).get("adlabels")
        answer = original(segments, body)
        if "adlabels" in body and node in s.objects and before is not None:
            def parse(v):
                return v if isinstance(v, list) else json.loads(v)

            old, new = parse(before), parse(s.objects[node]["adlabels"])
            merged = {l["id"]: l for l in old + new}
            s.objects[node]["adlabels"] = json.dumps(sorted(merged.values(), key=lambda l: l["id"]))
        return answer

    s._post = post


def test_a_detach_that_did_not_remove_the_label_is_not_recorded_as_applied(monkeypatch):
    from metaads_mcp import ledger

    s = make_sim()
    s.objects["L1"] = {"id": "L1", "name": "spring"}
    s.objects["L2"] = {"id": "L2", "name": "summer"}
    s.objects[CAMPAIGN_ID]["adlabels"] = [{"id": "L1"}, {"id": "L2"}]
    _append_only_labels(s)
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "low_risk_change", s, entity_type="campaign", entity_id=CAMPAIGN_ID,
                         field="label_detach", value="L2")
    with pytest.raises(Exception) as caught:
        tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert "still" in str(caught.value) and "L2" in str(caught.value)
    entry = ledger.entries()[-1]
    assert not entry["applied"] and entry["error"]


def test_a_revert_of_an_attach_that_did_not_remove_the_label_fails(monkeypatch):
    from metaads_mcp import ledger

    s = make_sim()
    s.objects["L1"] = {"id": "L1", "name": "spring"}
    s.objects[CAMPAIGN_ID]["adlabels"] = []
    tools, _ = rig(monkeypatch, s)
    attach = preview_ok(tools, "low_risk_change", s, entity_type="campaign", entity_id=CAMPAIGN_ID,
                        field="label_attach", value="L1")
    result = tools["apply_low_risk_change"](preview_token=attach.preview_token)
    _append_only_labels(s)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "still" in str(caught.value)
    assert not ledger.entries()[-1]["applied"]


def test_a_detach_that_landed_is_applied(monkeypatch):
    s = make_sim()
    s.objects["L1"] = {"id": "L1", "name": "spring"}
    s.objects["L2"] = {"id": "L2", "name": "summer"}
    s.objects[CAMPAIGN_ID]["adlabels"] = [{"id": "L1"}, {"id": "L2"}]
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "low_risk_change", s, entity_type="campaign", entity_id=CAMPAIGN_ID,
                         field="label_detach", value="L2")
    assert tools["apply_low_risk_change"](preview_token=preview.preview_token).applied
    assert json.loads(s.objects[CAMPAIGN_ID]["adlabels"]) == [{"id": "L1"}]
