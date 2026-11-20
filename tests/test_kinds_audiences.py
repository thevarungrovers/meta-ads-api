"""Creating audiences of every kind -- and never a customer list."""

from __future__ import annotations

import json

import pytest

from metaads_reporting.write.audiences import (
    CustomerDataRefused,
    check_payload,
)
from tests.kindkit import failed, preview_ok, rig
from tests.test_mcp_write_path import make_sim

ACT = "act_1234567890"


def sim_with_sources():
    sim = make_sim()
    sim.objects["PX1"] = {"id": "PX1", "name": "Main pixel"}
    sim.objects["PAGE1"] = {"id": "PAGE1", "name": "Example Co"}
    sim.objects["IG1"] = {"id": "IG1", "username": "exampleco"}
    sim.objects["VID1"] = {"id": "VID1"}
    sim.objects["FORM1"] = {"id": "FORM1", "status": "ACTIVE"}
    sim.edges[ACT].update(adspixels=["PX1"], promote_pages=["PAGE1"], instagram_accounts=["IG1"])
    sim.canned["PX1/stats"] = {"data": [{"data": [{"value": "PageView", "count": 10}]}]}
    return sim


def created_rule(sim):
    payload = next(c for c in sim.created if c["edge"] == "customaudiences")["payload"]
    return payload, json.loads(payload["rule"])


def test_a_website_audience_is_a_pixel_rule(monkeypatch):
    sim = sim_with_sources()
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "create_audience", sim, kind="website", name="Visitors 30d",
                         pixel_id="PX1", retention_days=30)
    tools["apply_create_audience"](preview_token=preview.preview_token)
    payload, rule = created_rule(sim)
    source = rule["inclusions"]["rules"][0]
    assert source["event_sources"] == [{"id": "PX1", "type": "pixel"}]
    assert source["retention_seconds"] == 30 * 86400
    assert payload["subtype"] == "WEBSITE"


def test_a_pixel_outside_the_account_fails_and_a_silent_event_warns(monkeypatch):
    sim = sim_with_sources()
    tools, _ = rig(monkeypatch, sim)
    other = tools["preview_create_audience"](kind="website", name="x", pixel_id="NOPE")
    assert failed(other, "pixel_in_account")
    quiet = tools["preview_create_audience"](kind="website", name="x", pixel_id="PX1", event="Purchase")
    assert any("will be empty" in w for w in quiet.warnings)


def test_website_retention_is_bounded(monkeypatch):
    tools, _ = rig(monkeypatch, sim_with_sources())
    with pytest.raises(Exception):
        tools["preview_create_audience"](kind="website", name="x", pixel_id="PX1", retention_days=200)


@pytest.mark.parametrize("kind, args, source_type", [
    ("engagement_page", {"page_id": "PAGE1"}, "page"),
    ("engagement_ig", {"ig_account_id": "IG1", "engagement_event": "ig_business_profile_engaged"},
     "ig_business"),
    ("video_viewers", {"video_ids": ["VID1"], "watch_threshold": "thruplay"}, "video"),
    ("lead_form", {"lead_form_id": "FORM1", "form_event": "submitted"}, "lead"),
])
def test_engagement_kinds_build_rules_on_their_source(monkeypatch, kind, args, source_type):
    sim = sim_with_sources()
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "create_audience", sim, kind=kind, name="x", **args)
    tools["apply_create_audience"](preview_token=preview.preview_token)
    payload, rule = created_rule(sim)
    assert rule["inclusions"]["rules"][0]["event_sources"][0]["type"] == source_type
    assert payload["subtype"] == "ENGAGEMENT"


def test_an_unconnected_page_fails(monkeypatch):
    tools, _ = rig(monkeypatch, sim_with_sources())
    preview = tools["preview_create_audience"](kind="engagement_page", name="x", page_id="OTHER")
    assert failed(preview, "identity_connected")


def test_too_many_videos_are_refused(monkeypatch):
    tools, _ = rig(monkeypatch, sim_with_sources())
    with pytest.raises(Exception):
        tools["preview_create_audience"](kind="video_viewers", name="x",
                                         video_ids=[str(i) for i in range(51)])


@pytest.mark.parametrize("ratio", [0.005, 0.11, 0.2])
def test_lookalike_ratios_outside_one_to_ten_percent_are_refused(monkeypatch, ratio):
    from metaads_reporting.write import audiences

    sim = sim_with_sources()
    sim.objects["SRC"] = {"id": "SRC", "name": "Buyers"}
    tools, state = rig(monkeypatch, sim)
    with pytest.raises(Exception):
        audiences.plan_create_lookalike(state.write_client, name="x",
                                        origin_audience_id="SRC", country="CA", ratio=ratio)


def test_a_lookalike_from_a_too_small_source_is_refused(monkeypatch):
    sim = sim_with_sources()
    sim.objects["SRC"] = {"id": "SRC", "name": "Tiny", "approximate_count_lower_bound": 40,
                          "delivery_status": {"code": 200}}
    tools, _ = rig(monkeypatch, sim)
    with pytest.raises(Exception) as caught:
        tools["preview_create_audience"](kind="lookalike", name="x", origin_audience_id="SRC",
                                         country="CA")
    assert "at least 100" in str(caught.value)


@pytest.mark.parametrize("subtype", ["WEBSITE", "ENGAGEMENT", "APP", "LOOKALIKE"])
@pytest.mark.parametrize("key", ["data", "schema", "users", "payload", "is_raw", "customer_file_source"])
def test_every_subtype_still_refuses_customer_data(subtype, key):
    with pytest.raises(CustomerDataRefused):
        check_payload(subtype, {"name": "x", "subtype": subtype, key: "anything"})


def test_subtype_custom_is_refused_outright():
    with pytest.raises(CustomerDataRefused):
        check_payload("CUSTOM", {"name": "x", "subtype": "CUSTOM"})


def test_website_and_engagement_accept_their_rule():
    check_payload("WEBSITE", {"name": "x", "subtype": "WEBSITE", "rule": {}, "retention_days": 30})
    check_payload("ENGAGEMENT", {"name": "x", "subtype": "ENGAGEMENT", "rule": {}})


def test_an_audience_create_has_no_revert(monkeypatch):
    sim = sim_with_sources()
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "create_audience", sim, kind="engagement_page", name="x", page_id="PAGE1")
    result = tools["apply_create_audience"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "unused_only" in str(caught.value)
