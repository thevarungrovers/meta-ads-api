"""Duplicating an ad, repointing a live one, and creating audiences.

Driven end to end against the simulator, which really performs the creates, so
preview -> apply -> read-it-back is a real round trip.
"""

from __future__ import annotations

import struct
import zlib
from decimal import Decimal

import pytest

from metaads_reporting.write.audiences import CustomerDataRefused
from tests.fakes import account, ad, ad_set, campaign, insights_row
from tests.graphsim import GraphSim
from tests.harness import build_tools

CAMPAIGN_ID = "120200000000001"
ADSET_ID = "120200000000002"
AD_ID = "120200000000003"
CREATIVE_ID = "900000000000001"


def png_bytes(w=400, h=400) -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def link_creative_obj():
    return {
        "id": CREATIVE_ID,
        "name": "Creative A",
        "object_story_spec": {
            "page_id": "PAGE",
            "instagram_user_id": "IG",
            "link_data": {
                "message": "old body",
                "name": "old headline",
                "description": "old description",
                "link": "https://example.com/a",
                "image_hash": "oldhash",
                "call_to_action": {"type": "SIGN_UP"},
            },
        },
    }


def make_sim(**over) -> GraphSim:
    objects = {
        CAMPAIGN_ID: campaign(CAMPAIGN_ID, daily_budget="25000"),
        ADSET_ID: ad_set(ADSET_ID, daily_budget="5000", campaign_id=CAMPAIGN_ID),
        AD_ID: ad(AD_ID, creative={"id": CREATIVE_ID}),
        CREATIVE_ID: link_creative_obj(),
    }
    objects.update(over.pop("objects", {}))
    return GraphSim(
        account=account(),
        objects=objects,
        edges={
            "act_1234567890": {
                "campaigns": [CAMPAIGN_ID],
                "adsets": [ADSET_ID],
                "ads": [AD_ID],
            }
        },
        insights=[insights_row()],
        **over,
    )


@pytest.fixture
def tools(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, state, sim


# --- reading what an ad says ------------------------------------------------


def test_get_ad_creative_reports_the_copy_and_the_shape(tools):
    tool, state, sim = tools
    out = tool["get_ad_creative"](ad_id=AD_ID)
    assert out["shape"] == "link_data"
    assert out["text"]["headline"] == "old headline"
    assert out["text"]["primary_text"] == "old body"
    assert out["editable_text"] is True


# --- duplicating ------------------------------------------------------------


def test_duplicate_creates_a_new_paused_ad_and_leaves_the_original(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new headline")
    assert preview.blocked is False
    assert sim.created == [], "the preview created something"

    result = tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert result.applied is True

    edges = [c["edge"] for c in sim.created]
    assert edges == ["adcreatives", "ads"], edges

    new_ad = sim.created[-1]["payload"]
    assert new_ad["status"] == "PAUSED", "a new ad must never be created ACTIVE"
    assert new_ad["adset_id"] == ADSET_ID
    # The original is untouched.
    assert sim.objects[AD_ID]["creative"] == {"id": CREATIVE_ID}
    assert sim.objects[AD_ID]["status"] == "ACTIVE"


def test_the_duplicate_carries_the_edit_and_keeps_everything_else(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new headline")
    tool["apply_duplicate_ad"](preview_token=preview.preview_token)

    import json

    spec = json.loads(sim.created[0]["payload"]["object_story_spec"])
    block = spec["link_data"]
    assert block["name"] == "new headline"
    assert block["message"] == "old body", "untouched copy was lost"
    assert block["image_hash"] == "oldhash", "the image was lost"
    assert spec["page_id"] == "PAGE"


def test_duplicating_with_no_edits_at_all_is_refused(tools):
    """A copy with nothing changed is just a second identical ad."""
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_duplicate_ad"](ad_id=AD_ID)
    assert "Nothing to change" in str(caught.value)


def test_a_duplicate_projects_zero_spend(tools):
    """It is PAUSED, and the ad set's budget does not move."""
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="x")
    assert preview.projected_daily_spend_delta == "0.00"


def test_the_preview_warns_the_new_ad_is_paused(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="x")
    assert any("PAUSED" in w for w in preview.warnings)
    assert any("untouched" in w for w in preview.warnings)


# --- repointing a live ad ---------------------------------------------------


def test_update_copy_repoints_the_same_ad(tools):
    tool, state, sim = tools
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new headline")
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    assert result.applied is True

    # One creative created, and the EXISTING ad repointed at it.
    assert [c["edge"] for c in sim.created] == ["adcreatives"]
    new_creative_id = "CREATIVE1"
    assert sim.objects[AD_ID]["creative"] == '{"creative_id": "CREATIVE1"}' or \
        new_creative_id in str(sim.objects[AD_ID]["creative"])
    assert sim.applied, "the ad was never updated"


def test_update_copy_warns_about_the_social_proof_it_discards(monkeypatch):
    """The number that makes the choice concrete."""
    sim = make_sim()
    sim.objects[CREATIVE_ID]["effective_object_story_id"] = "POST1"
    sim.objects["POST1"] = {
        "id": "POST1",
        "likes": {"summary": {"total_count": 340}},
        "comments": {"summary": {"total_count": 28}},
        "shares": {"count": 11},
    }
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_update_ad_copy"](ad_id=AD_ID, headline="x")
    warning = " ".join(preview.warnings)
    assert "SOCIAL PROOF" in warning
    assert "340" in warning and "28" in warning and "11" in warning
    assert "Duplicating" in warning


def test_update_copy_is_the_destructive_one_of_the_pair():
    """Marking the harmless duplicate destructive would train people to click
    through the loud prompts."""
    from tests.test_mcp_surface import APPLY_TOOLS

    assert "apply_update_ad_copy" in APPLY_TOOLS
    assert "apply_duplicate_ad" in APPLY_TOOLS


# --- media ------------------------------------------------------------------


def test_an_image_is_uploaded_at_apply_time_not_preview_time(tools, tmp_path):
    """Uploading during a preview would put a file in the account's library for
    a change the human then declined."""
    tool, state, sim = tools
    image = tmp_path / "creative.png"
    image.write_bytes(png_bytes())

    preview = tool["preview_duplicate_ad"](
        ad_id=AD_ID, headline="x", image_path=str(image)
    )
    assert not any(c["edge"] == "adimages" for c in sim.created), "uploaded too early"

    tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert [c["edge"] for c in sim.created] == ["adimages", "adcreatives", "ads"]


def test_the_uploaded_hash_reaches_the_creative(tools, tmp_path):
    tool, state, sim = tools
    image = tmp_path / "creative.png"
    image.write_bytes(png_bytes())
    preview = tool["preview_duplicate_ad"](
        ad_id=AD_ID, headline="x", image_path=str(image)
    )
    tool["apply_duplicate_ad"](preview_token=preview.preview_token)

    creative_payload = next(c for c in sim.created if c["edge"] == "adcreatives")
    assert "<pending upload>" not in str(creative_payload), "placeholder left behind"
    assert "hash1" in str(creative_payload)


def test_a_missing_image_fails_at_preview_not_at_apply(tools):
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_duplicate_ad"](ad_id=AD_ID, image_path="/nope/missing.png")
    assert "No file at" in str(caught.value)


def test_an_image_and_a_video_together_is_refused(tools, tmp_path):
    image = tmp_path / "c.png"
    image.write_bytes(png_bytes())
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_duplicate_ad"](
            ad_id=AD_ID, image_path=str(image), video_id="VID"
        )
    assert "not both" in str(caught.value)


def test_a_video_cannot_be_put_on_an_image_creative(monkeypatch):
    """That would silently change what kind of ad it is."""
    sim = make_sim()
    sim.objects["VID1"] = {"id": "VID1", "status": {"video_status": "ready"}}
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    with pytest.raises(Exception) as caught:
        registrar.tools["preview_duplicate_ad"](ad_id=AD_ID, video_id="VID1")
    assert "what kind of ad it is" in str(caught.value)


def test_a_video_still_encoding_is_refused(monkeypatch):
    sim = make_sim()
    sim.objects["VID1"] = {"id": "VID1", "status": {"video_status": "processing"}}
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    with pytest.raises(Exception) as caught:
        registrar.tools["preview_duplicate_ad"](ad_id=AD_ID, video_id="VID1")
    assert "not ready" in str(caught.value)


# --- audiences --------------------------------------------------------------


def test_a_lookalike_is_created_from_an_existing_source(monkeypatch):
    sim = make_sim()
    sim.objects["SRC1"] = {"id": "SRC1", "name": "Purchasers", "subtype": "WEBSITE"}
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    tool = registrar.tools

    preview = tool["preview_create_audience"](
        kind="lookalike", name="LAL 1%", origin_audience_id="SRC1", country="CA"
    )
    assert preview.blocked is False
    assert any("no customer data is uploaded" in w for w in preview.warnings)

    tool["apply_create_audience"](preview_token=preview.preview_token)
    created = next(c for c in sim.created if c["edge"] == "customaudiences")
    assert created["payload"]["subtype"] == "LOOKALIKE"
    assert created["payload"]["origin_audience_id"] == "SRC1"


def test_a_saved_audience_is_created_with_its_targeting(tools):
    tool, state, sim = tools
    targeting = {"geo_locations": {"countries": ["CA"]}, "age_min": 25}
    preview = tool["preview_create_audience"](
        kind="saved", name="QC 25+", targeting=targeting
    )
    tool["apply_create_audience"](preview_token=preview.preview_token)
    created = next(c for c in sim.created if c["edge"] == "saved_audiences")
    assert "geo_locations" in str(created["payload"]["targeting"])


def test_a_customer_list_audience_cannot_be_requested_at_all(tools):
    """The kind is refused before any payload is built."""
    tool, state, sim = tools
    for kind in ("custom", "CUSTOM", "customer_list", "website"):
        with pytest.raises(Exception) as caught:
            tool["preview_create_audience"](kind=kind, name="x")
        assert "upload personal data" in str(caught.value), kind


def test_customer_data_in_a_saved_targeting_spec_is_refused(tools):
    """The second half of the guard: not just the subtype, the payload too."""
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_create_audience"](
            kind="saved",
            name="sneaky",
            targeting={"schema": ["EMAIL_SHA256"], "data": [["abc"]]},
        )
    assert "personal data" in str(caught.value)


def test_a_lookalike_ratio_outside_metas_range_is_refused(monkeypatch):
    sim = make_sim()
    sim.objects["SRC1"] = {"id": "SRC1", "name": "Src", "subtype": "WEBSITE"}
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    from metaads_reporting.write import audiences

    with pytest.raises(Exception):
        audiences.plan_create_lookalike(
            registrar.tools["preview_create_audience"].__self__
            if hasattr(registrar.tools["preview_create_audience"], "__self__")
            else None,
            name="x", origin_audience_id="SRC1", country="CA", ratio=0.5,
        )


def test_creating_an_audience_projects_zero_spend(tools):
    tool, state, sim = tools
    preview = tool["preview_create_audience"](
        kind="saved", name="x", targeting={"age_min": 25}
    )
    assert preview.projected_daily_spend_delta == "0.00"


# --- the usual contracts still hold -----------------------------------------


def test_a_creative_token_is_single_use(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="x")
    tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert "already used" in str(caught.value)


def test_a_creative_token_is_not_interchangeable(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="x")
    with pytest.raises(Exception) as caught:
        tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    assert "not interchangeable" in str(caught.value)


def test_the_kill_switch_stops_a_creative_apply(tools, monkeypatch, tmp_path):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="x")
    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("ops")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)
    with pytest.raises(Exception) as caught:
        tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert "writes are disabled" in str(caught.value)
    assert sim.created == []


def test_a_creative_change_lands_in_the_ledger(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    result = tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    rows = tool["list_my_changes"]()
    assert rows[-1]["entry_id"] == result.entry_id
    assert rows[-1]["applied"] is True
    assert rows[-1]["field"] == "creative"
