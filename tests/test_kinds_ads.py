"""create_ad, create_creative, multi_ad_status."""

from __future__ import annotations

import json

import pytest

from tests.fakes import ad
from tests.kindkit import preview_ok, rig
from tests.test_mcp_creative_path import AD_ID, ADSET_ID, CREATIVE_ID, make_sim, png_bytes


def sim():
    s = make_sim()
    s.objects[ADSET_ID]["targeting"] = {"geo_locations": {"countries": ["CA"]}}
    s.edges["act_1234567890"]["adcreatives"] = [CREATIVE_ID]
    s.edges[ADSET_ID] = {"ads": [AD_ID]}
    return s


def test_create_ad_is_paused_weighs_three_and_reverts_to_paused(monkeypatch):
    s = sim()
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_ad", s, ad_set_id=ADSET_ID, creative_id=CREATIVE_ID, name="New ad")
    assert preview.apply_weight == 3 and preview.previews
    result = tools["apply_create_ad"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "ads")["payload"]
    assert created["status"] == "PAUSED"
    assert state.counters.applies == 3
    assert result.verification is not None
    new_ad = result.entity_id
    s.objects[new_ad]["status"] = "ACTIVE"
    tools["revert_change"](entry_id=result.entry_id)
    assert s.objects[new_ad]["status"] == "PAUSED"


def test_create_creative_defaults_identity_and_uploads_at_apply(monkeypatch, tmp_path):
    s = sim()
    image = tmp_path / "c.png"
    image.write_bytes(png_bytes(1080, 1080))
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_creative"](
        name="Spring", link="https://example.com/spring", headline="Fresh", image_path=str(image),
        call_to_action="SHOP_NOW", ad_set_id=ADSET_ID)
    assert not preview.blocked, preview.checks
    assert "page: 'PAGE'" in preview.after and "instagram: 'IG'" in preview.after
    assert not any(c["edge"] == "adimages" for c in s.created)
    result = tools["apply_create_creative"](preview_token=preview.preview_token)
    assert [c["edge"] for c in s.created] == ["adimages", "adcreatives"]
    spec = json.loads(s.created[-1]["payload"]["object_story_spec"])
    assert spec["link_data"]["image_hash"] == "hash1" and spec["page_id"] == "PAGE"
    assert result.verification["passed"] is True
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "unattached creative" in str(caught.value)


def test_create_creative_wants_one_media(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception):
        tools["preview_create_creative"](name="x", link="https://example.com")


def test_multi_ad_status_is_a_batch_revertible_by_batch_id(monkeypatch):
    s = sim()
    for i in (2, 3):
        s.objects[f"AD{i}"] = ad(f"AD{i}", name=f"Ad {i}")
        s.edges[ADSET_ID]["ads"].append(f"AD{i}")
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "multi_ad_status", s, ad_set_id=ADSET_ID,
                         ad_ids=[AD_ID, "AD2", "AD3"], new_status="PAUSED")
    assert len(preview.items) == 3
    assert any("entities_per_call" in c for c in preview.checks)
    result = tools["apply_multi_ad_status"](preview_token=preview.preview_token)
    assert result.batch_id and len(result.entry_ids) == 3
    assert {s.objects[a]["status"] for a in (AD_ID, "AD2", "AD3")} == {"PAUSED"}
    assert state.counters.applies == 1, "a batch costs its weight once"
    tools["revert_change"](entry_id=result.batch_id)
    assert {s.objects[a]["status"] for a in (AD_ID, "AD2", "AD3")} == {"ACTIVE"}


def test_multi_ad_status_respects_max_entities_per_call(monkeypatch):
    s = sim()
    s.objects["AD2"] = ad("AD2")
    tools, _ = rig(monkeypatch, s, max_entities_per_call=1)
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=[AD_ID, "AD2"],
                                               new_status="PAUSED")
    assert preview.blocked and any(c.startswith("FAIL entities_per_call") for c in preview.checks)


def test_multi_ad_status_refuses_an_ad_from_another_ad_set(monkeypatch):
    s = sim()
    s.objects["AD9"] = ad("AD9", adset_id="OTHER")
    tools, _ = rig(monkeypatch, s)
    with pytest.raises(Exception):
        tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=["AD9"], new_status="PAUSED")


def test_a_batch_failure_part_way_says_what_landed(monkeypatch):
    from tests.fakes import error_response

    s = sim()
    s.objects["AD2"] = ad("AD2")
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=[AD_ID, "AD2"],
                                               new_status="PAUSED")
    original = s.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST" and url.endswith("/AD2"):
            return error_response(100, message="Invalid parameter")
        return original(method, url, params, data, files, timeout)

    s.request = router
    with pytest.raises(Exception) as caught:
        tools["apply_multi_ad_status"](preview_token=preview.preview_token)
    assert "1 of 2 applied" in str(caught.value)
    rows = tools["list_my_changes"]()
    assert [r["applied"] for r in rows] == [True, False]
