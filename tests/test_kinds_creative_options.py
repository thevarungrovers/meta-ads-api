"""Placements, UTM tags, Advantage+ options, lead forms, creative swaps."""

from __future__ import annotations

import json

import pytest

from tests.kindkit import failed, preview_ok, rig
from tests.test_mcp_creative_path import AD_ID, ADSET_ID, CREATIVE_ID, make_sim


def sim():
    s = make_sim()
    s.objects[ADSET_ID]["targeting"] = {"geo_locations": {"countries": ["CA"]}}
    s.edges[ADSET_ID] = {"ads": [AD_ID]}
    return s


def test_placements_preview_every_ad_on_the_new_set_and_revert(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_placements", s, ad_set_id=ADSET_ID,
                         publisher_platforms=["instagram"], instagram_positions=["story", "reels"])
    assert {p["ad_format"] for p in preview.previews} == {"INSTAGRAM_STORY", "INSTAGRAM_REELS"}
    assert all(p["ad_id"] == AD_ID for p in preview.previews)
    result = tools["apply_ad_set_placements"](preview_token=preview.preview_token)
    sent = json.loads(s.objects[ADSET_ID]["targeting"])
    assert sent["geo_locations"] == {"countries": ["CA"]}, "the rest of the spec kept"
    assert sent["instagram_positions"] == ["story", "reels"]
    tools["revert_change"](entry_id=result.entry_id)
    assert json.loads(s.objects[ADSET_ID]["targeting"]) == {"geo_locations": {"countries": ["CA"]}}


def test_placements_refuse_positions_without_their_platform(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception):
        tools["preview_ad_set_placements"](ad_set_id=ADSET_ID, publisher_platforms=["facebook"],
                                           instagram_positions=["story"])


def test_a_placement_apply_refuses_when_other_targeting_moved(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_placements"](ad_set_id=ADSET_ID, publisher_platforms=["facebook"])
    s.objects[ADSET_ID]["targeting"] = {"geo_locations": {"countries": ["US"]}}
    with pytest.raises(Exception) as caught:
        tools["apply_ad_set_placements"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)


def test_url_tags_make_a_new_creative_and_verify_it(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "url_tags", s, ad_id=AD_ID, url_tags="utm_source=meta&utm_campaign={{campaign.name}}")
    assert preview.previews
    result = tools["apply_url_tags"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "adcreatives")["payload"]
    assert created["url_tags"] == "utm_source=meta&utm_campaign={{campaign.name}}"
    assert result.verification["passed"] is True
    tools["revert_change"](entry_id=result.entry_id)
    assert json.loads(s.objects[AD_ID]["creative"]) == {"creative_id": CREATIVE_ID}


@pytest.mark.parametrize("bad", ["?utm_source=x", "utm_source", "a=b&&c=d", ""])
def test_malformed_url_tags_are_refused(monkeypatch, bad):
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception):
        tools["preview_url_tags"](ad_id=AD_ID, url_tags=bad)


def test_advantage_options_carry_every_existing_opt_out(monkeypatch):
    s = sim()
    s.objects[CREATIVE_ID]["degrees_of_freedom_spec"] = {"creative_features_spec": {
        "image_touchups": {"enroll_status": "OPT_OUT"}}}
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "advantage_creative_options", s, ad_id=AD_ID, opt_out=["music"])
    assert "image_touchups" in preview.after and "music" in preview.after
    tools["apply_advantage_creative_options"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "adcreatives")["payload"]
    features = json.loads(created["degrees_of_freedom_spec"])["creative_features_spec"]
    assert features == {"image_touchups": {"enroll_status": "OPT_OUT"},
                        "music": {"enroll_status": "OPT_OUT"}}


def test_a_lead_form_on_another_page_or_wrong_ad_set_fails(monkeypatch):
    s = sim()
    s.objects["FORM1"] = {"id": "FORM1", "name": "Signup", "status": "ACTIVE", "page": {"id": "OTHER"}}
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_attach_lead_form"](ad_id=AD_ID, form_id="FORM1")
    assert failed(preview, "lead_form_page") and failed(preview, "ad_set_instant_form")


def test_a_lead_form_attaches_through_the_cta(monkeypatch):
    s = sim()
    s.objects["FORM1"] = {"id": "FORM1", "name": "Signup", "status": "ACTIVE", "page": {"id": "PAGE"}}
    s.objects[ADSET_ID].update(destination_type="ON_AD", promoted_object={"page_id": "PAGE"})
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "attach_lead_form", s, ad_id=AD_ID, form_id="FORM1")
    tools["apply_attach_lead_form"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "adcreatives")["payload"]
    cta = json.loads(created["object_story_spec"])["link_data"]["call_to_action"]
    assert cta["value"]["lead_gen_form_id"] == "FORM1"


def test_replace_ad_creative_points_at_an_existing_one_and_back(monkeypatch):
    s = sim()
    s.objects["CR2"] = {**s.objects[CREATIVE_ID], "id": "CR2"}
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "replace_ad_creative", s, ad_id=AD_ID, creative_id="CR2")
    assert preview.previews and "CR2" in preview.previews[0]["iframe_url"]
    result = tools["apply_replace_ad_creative"](preview_token=preview.preview_token)
    assert json.loads(s.objects[AD_ID]["creative"]) == {"creative_id": "CR2"}
    assert s.created == [], "no new creative for a swap"
    tools["revert_change"](entry_id=result.entry_id)
    assert json.loads(s.objects[AD_ID]["creative"]) == {"creative_id": CREATIVE_ID}
