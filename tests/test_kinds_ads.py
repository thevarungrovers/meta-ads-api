"""create_ad, create_creative, multi_ad_status."""

from __future__ import annotations

import json

import pytest

from tests.fakes import ad
from tests.kindkit import preview_ok, rig
from tests.test_mcp_creative_path import AD_ID, ADSET_ID, CAMPAIGN_ID, CREATIVE_ID, make_sim, png_bytes


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
    assert any("1 local image(s) are uploaded at apply" in w for w in preview.warnings)
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


CAMPAIGN_TAGS = "utm_source=meta&utm_campaign=spring"
ACCOUNT_TAGS = "utm_source=meta&utm_campaign=other"


def _created_spec(tools, s, **kw):
    preview = tools["preview_create_creative"](
        name="Spring", link="https://example.com/spring", image_hash="abc", **kw)
    assert not preview.blocked, preview.checks
    tools["apply_create_creative"](preview_token=preview.preview_token)
    return preview, s.created[-1]["payload"]


def test_create_creative_takes_url_tags_from_its_campaigns_ads(monkeypatch):
    s = sim()
    s.objects[CREATIVE_ID]["url_tags"] = ACCOUNT_TAGS
    s.objects[AD_ID]["creative"] = {"id": CREATIVE_ID, "url_tags": CAMPAIGN_TAGS}
    s.edges[CAMPAIGN_ID] = {"ads": [AD_ID]}
    tools, _ = rig(monkeypatch, s)
    preview, payload = _created_spec(tools, s, ad_set_id=ADSET_ID)
    assert payload["url_tags"] == CAMPAIGN_TAGS
    assert any("from the ads in campaign" in w for w in preview.warnings)


def test_create_creative_falls_back_to_the_accounts_url_tags_and_says_so(monkeypatch):
    s = sim()
    s.objects[CREATIVE_ID]["url_tags"] = ACCOUNT_TAGS
    tools, _ = rig(monkeypatch, s)
    preview, payload = _created_spec(tools, s, ad_set_id=ADSET_ID)
    assert payload["url_tags"] == ACCOUNT_TAGS
    assert any("account's most common" in w and "utm_campaign" in w for w in preview.warnings)


def test_create_creative_url_tags_can_be_left_empty_on_purpose(monkeypatch):
    s = sim()
    s.objects[CREATIVE_ID]["url_tags"] = ACCOUNT_TAGS
    tools, _ = rig(monkeypatch, s)
    preview, payload = _created_spec(tools, s, url_tags="")
    assert "url_tags" not in payload
    assert any("on purpose" in w for w in preview.warnings)


def test_create_creative_warns_when_nothing_tags_the_clicks(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview, payload = _created_spec(tools, s)
    assert "url_tags" not in payload
    assert any(w.startswith("no url_tags") for w in preview.warnings)


def test_create_ad_warns_when_its_creative_has_no_url_tags(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_ad", s, ad_set_id=ADSET_ID, creative_id=CREATIVE_ID, name="New ad")
    assert any("has no url_tags" in w for w in preview.warnings)
    s.objects[CREATIVE_ID]["url_tags"] = CAMPAIGN_TAGS
    preview = preview_ok(tools, "create_ad", s, ad_set_id=ADSET_ID, creative_id=CREATIVE_ID, name="Other ad")
    assert not any("url_tags" in w for w in preview.warnings)


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


def test_multi_ad_status_says_which_ads_it_left_out(monkeypatch):
    # Live: two ads asked, one already PAUSED -- the preview listed one and
    # said nothing about the other.
    s = sim()
    s.objects["AD2"] = ad("AD2", status="PAUSED")
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_multi_ad_status"](ad_set_id=ADSET_ID, ad_ids=[AD_ID, "AD2"],
                                               new_status="PAUSED")
    assert any("AD2" in w and "already PAUSED" in w for w in preview.warnings)


FR = {"language": "fr", "primary_text": "Bonjour", "headline": "Titre",
      "link": "https://example.com/fr", "image_hash": "frfeed", "story_image_hash": "frstory"}
EN = {"language": "en", "primary_text": "Hello", "headline": "Title",
      "link": "https://example.com/en", "image_hash": "enfeed"}


def _feed(payload):
    feed = payload["asset_feed_spec"]
    return json.loads(feed) if isinstance(feed, str) else feed


def test_create_creative_builds_a_multi_language_creative(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_creative"](name="Spring", locales=[FR, EN], url_tags="utm_source=meta")
    assert not preview.blocked, preview.checks
    assert "fr (default):" in preview.after and "en:" in preview.after
    assert sum(c.split()[1].startswith("ad_destination") for c in preview.checks) == 2
    result = tools["apply_create_creative"](preview_token=preview.preview_token)
    payload = s.created[-1]["payload"]
    feed = _feed(payload)
    story = payload["object_story_spec"]
    story = json.loads(story) if isinstance(story, str) else story
    assert story == {"page_id": "PAGE", "instagram_user_id": "IG"}
    assert [b["text"] for b in feed["bodies"]] == ["Bonjour", "Hello"]
    # An ad on a language feed needs a description per language: empty ones are added.
    assert [(d["text"], d["adlabels"][0]["name"]) for d in feed["descriptions"]] == [
        ("", "fr_description"), ("", "en_description")]
    assert all(r["description_label"]["name"].endswith("_description") for r in feed["asset_customization_rules"])
    assert [i["hash"] for i in feed["images"]] == ["frfeed", "frstory", "enfeed"]
    rules = feed["asset_customization_rules"]
    assert [(r["image_label"]["name"], r["customization_spec"]["locales"], r["is_default"])
            for r in rules] == [("fr_story", [9, 44], False), ("fr_img", [9, 44], True),
                                ("en_img", [6, 24], False)]
    assert rules[0]["customization_spec"]["instagram_positions"] == ["story", "reels"]
    assert payload["url_tags"] == "utm_source=meta"
    assert result.verification["passed"] is True


def test_create_creative_default_language_picks_the_fallback_rule(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_creative"](name="Spring", locales=[FR, EN], default_language="en")
    tools["apply_create_creative"](preview_token=preview.preview_token)
    default = [r for r in _feed(s.created[-1]["payload"])["asset_customization_rules"] if r["is_default"]]
    assert [r["customization_spec"]["locales"] for r in default] == [[6, 24]]


@pytest.mark.parametrize("change, message", [
    ({"en": {**EN, "image_hash": None, "video_id": "V1"}}, "same kind of media"),
    ({"en": {**EN, "headline": None}}, "some locales and not others"),
    ({"en": {**EN, "language": "fr"}}, "its own language"),
    ({"en": {**EN, "language": "de"}}, "locale_ids"),
    ({"top": {"headline": "x"}}, "inside each locale"),
])
def test_create_creative_refuses_a_malformed_multi_language_creative(monkeypatch, change, message):
    _malformed(monkeypatch, change, message)


@pytest.mark.parametrize("field", ["primary_text", "headline"])
def test_a_multi_language_creative_needs_body_and_headline_in_every_language(monkeypatch, field):
    # Live: Meta takes the creative without them, then refuses every ad on it.
    s = sim()
    tools, _ = rig(monkeypatch, s)
    strip = lambda loc: {k: v for k, v in loc.items() if k != field}
    with pytest.raises(Exception) as caught:
        tools["preview_create_creative"](name="Spring", locales=[strip(FR), strip(EN)])
    assert f"Every locale needs {field}" in str(caught.value)


def _malformed(monkeypatch, change, message):
    s = sim()
    s.objects["V1"] = {"id": "V1", "status": {"video_status": "ready"}}
    tools, _ = rig(monkeypatch, s)
    en = {k: v for k, v in change.get("en", EN).items() if v is not None}
    with pytest.raises(Exception) as caught:
        tools["preview_create_creative"](name="Spring", locales=[FR, en], **change.get("top", {}))
    assert message in str(caught.value)


def _image(tmp_path, name="fr.png"):
    path = tmp_path / name
    path.write_bytes(png_bytes(1080, 1080))
    return path


def test_upload_image_uploads_only_at_apply_and_returns_metas_hash(monkeypatch, tmp_path):
    s = sim()
    path = _image(tmp_path)
    tools, state = rig(monkeypatch, s)
    preview = tools["preview_upload_image"](image_path=str(path))
    assert not preview.blocked and "1080x1080" in preview.after
    assert "the image_hash Meta returns" in preview.after
    assert not s.created and not s.validated, "a preview sends nothing"
    result = tools["apply_upload_image"](preview_token=preview.preview_token)
    assert [c["edge"] for c in s.created] == ["adimages"]
    assert result.entity_id == "hash1" and state.counters.applies == 1
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "nothing is deleted" in str(caught.value)


def test_upload_image_points_out_a_library_image_with_the_same_name(monkeypatch, tmp_path):
    s = sim()
    path = _image(tmp_path)
    s.objects["IMG1"] = {"id": "IMG1", "hash": "abc123", "name": "fr.png"}
    s.edges["act_1234567890"]["adimages"] = ["IMG1"]
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_upload_image"](image_path=str(path))
    assert not preview.blocked
    assert any("already has an image named fr.png: image_hash abc123" in w for w in preview.warnings)


def test_upload_image_refuses_a_file_changed_since_the_preview(monkeypatch, tmp_path):
    s = sim()
    path = _image(tmp_path)
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_upload_image"](image_path=str(path))
    path.write_bytes(png_bytes(1200, 628))
    with pytest.raises(Exception) as caught:
        tools["apply_upload_image"](preview_token=preview.preview_token)
    assert "changed on disk" in str(caught.value)
    assert not s.created


def test_a_multi_language_creative_uploads_its_local_images_at_apply(monkeypatch, tmp_path):
    s = sim()
    fr = _image(tmp_path, "fr.png")
    fr_story = tmp_path / "fr_story.png"
    fr_story.write_bytes(png_bytes(1080, 1920))
    tools, _ = rig(monkeypatch, s)
    locales = [{**FR, "image_hash": None, "image_path": str(fr),
                "story_image_hash": None, "story_image_path": str(fr_story)},
               {**EN, "image_path": str(fr), "image_hash": None}]
    locales = [{k: v for k, v in loc.items() if v is not None} for loc in locales]
    preview = tools["preview_create_creative"](name="Spring", locales=locales)
    assert not preview.blocked, preview.checks
    assert not s.created, "nothing is uploaded at preview"
    assert any("uploaded at apply" in w for w in preview.warnings)
    result = tools["apply_create_creative"](preview_token=preview.preview_token)
    # Two distinct files, each uploaded once, though fr.png serves both languages.
    assert [c["edge"] for c in s.created] == ["adimages", "adimages", "adcreatives"]
    feed = _feed(s.created[-1]["payload"])
    assert [i["hash"] for i in feed["images"]] == ["hash1", "hash2", "hash1"]
    assert result.verification["passed"] is True


def test_a_missing_local_image_is_refused_at_preview(monkeypatch, tmp_path):
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception) as caught:
        tools["preview_create_creative"](name="Spring", locales=[
            {**FR, "image_hash": None, "story_image_hash": None,
             "image_path": str(tmp_path / "nope.png")}, EN])
    assert "No file at" in str(caught.value)


def _video(tmp_path, name="clip.mp4", payload=b"x" * 10):
    import struct

    # ftyp + moov{mvhd v0: timescale 1000, duration 32500} + a little mdat.
    mvhd = struct.pack(">I4sB3x", 108, b"mvhd", 0) + struct.pack(">IIII", 0, 0, 1000, 32500) + b"\0" * 80
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    data = (struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0" + moov
            + struct.pack(">I4s", 8 + len(payload), b"mdat") + payload)
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_upload_video_sends_every_chunk_and_reports_it_ready(monkeypatch, tmp_path):
    s = sim()
    path = _video(tmp_path)
    tools, state = rig(monkeypatch, s)
    preview = tools["preview_upload_video"](video_path=str(path), wait_seconds=0)
    assert not preview.blocked and "mp4, 0:32" in preview.after
    assert not s.created, "a preview sends nothing"
    result = tools["apply_upload_video"](preview_token=preview.preview_token)
    size = path.stat().st_size
    assert s.video_chunks[0] == (0, 4) and s.video_chunks[-1][1] == size
    assert result.entity_id == "VIDEO1" and state.counters.applies == 1
    assert any("is processed and ready: use it as video_id" in w for w in result.warnings)
    assert s.objects["VIDEO1"]["title"] == "clip.mp4"


def test_upload_video_says_when_meta_is_still_processing(monkeypatch, tmp_path):
    s = sim()
    s.video_status_after_upload = "processing"
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_upload_video"](video_path=str(_video(tmp_path)), wait_seconds=0)
    result = tools["apply_upload_video"](preview_token=preview.preview_token)
    assert any("still processing after 0s" in w and "list_ad_videos" in w for w in result.warnings)


def test_upload_video_refuses_what_is_not_mp4_or_mov(monkeypatch, tmp_path):
    path = tmp_path / "clip.avi"
    path.write_bytes(b"RIFF\0\0\0\0AVI LIST")
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception) as caught:
        tools["preview_upload_video"](video_path=str(path))
    assert "not an MP4 or MOV" in str(caught.value)


def test_wait_until_ready_is_bounded():
    from types import SimpleNamespace

    from metaads_reporting.write import media

    reads = []
    client = SimpleNamespace(read=SimpleNamespace(node=lambda *_: reads.append(1) or
                                                  {"status": {"video_status": "processing"}}))
    now = [0.0]
    state = media.wait_until_ready(client, "V", timeout=12, interval=5,
                                   sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert state == "processing" and len(reads) == 4  # t=0, 5, 10, 12


def _ready_video(s, thumbs):
    s.objects["V1"] = {"id": "V1", "status": {"video_status": "ready"}, "length": 30,
                       "picture": "https://example.com/pic.jpg",
                       "thumbnails": {"data": thumbs}}


def test_a_video_creative_without_a_thumbnail_uses_metas_preferred_frame(monkeypatch):
    s = sim()
    _ready_video(s, [{"uri": "https://example.com/t1.jpg", "is_preferred": False},
                     {"uri": "https://example.com/t2.jpg", "is_preferred": True}])
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_creative"](name="Clip", link="https://example.com", video_id="V1")
    tools["apply_create_creative"](preview_token=preview.preview_token)
    story = s.created[-1]["payload"]["object_story_spec"]
    story = json.loads(story) if isinstance(story, str) else story
    assert story["video_data"]["image_url"] == "https://example.com/t2.jpg"


def test_a_multi_language_video_creative_gets_a_thumbnail_per_language(monkeypatch):
    s = sim()
    _ready_video(s, [])
    tools, _ = rig(monkeypatch, s)
    video = lambda loc: {**{k: v for k, v in loc.items() if k not in ("image_hash", "story_image_hash")},
                         "video_id": "V1"}
    preview = tools["preview_create_creative"](name="Clip", locales=[video(FR), video(EN)])
    tools["apply_create_creative"](preview_token=preview.preview_token)
    feed = _feed(s.created[-1]["payload"])
    assert [v["thumbnail_url"] for v in feed["videos"]] == ["https://example.com/pic.jpg"] * 2
