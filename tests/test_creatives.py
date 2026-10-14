"""Creative shapes, and editing text without destroying the rest.

The fixtures here are the REAL shapes found on a live account on 2026-10-14,
including the detail that made the design what it is: an asset feed's lists are
tied together by `adlabels`, NOT by position -- the titles came back [FR, EN]
while the descriptions came back [EN, FR] on the same creative.
"""

from __future__ import annotations

import pytest

from metaads_reporting.write import creatives
from metaads_reporting.write.client import MutationError


def link_creative(**extra):
    raw = {
        "id": "100",
        "name": "Link ad",
        "object_story_spec": {
            "page_id": "PAGE",
            "instagram_user_id": "IG",
            "link_data": {
                "message": "primary text here",
                "name": "the headline",
                "description": "the description",
                "link": "https://example.com/a",
                "image_hash": "abc123",
                "call_to_action": {"type": "SIGN_UP"},
            },
        },
    }
    raw.update(extra)
    return creatives.Creative("100", "Link ad", creatives.classify(raw), raw)


def video_creative(**extra):
    raw = {
        "id": "200",
        "name": "Video ad",
        "object_story_spec": {
            "page_id": "PAGE",
            "video_data": {
                "video_id": "VID",
                "message": "primary text here",
                "title": "the headline",
                "link_description": "the description",
                "image_hash": "thumb123",
                "call_to_action": {"type": "LEARN_MORE"},
            },
        },
    }
    raw.update(extra)
    return creatives.Creative("200", "Video ad", creatives.classify(raw), raw)


def feed_creative(**extra):
    """Two locales, with the lists in DIFFERENT orders -- as seen live."""
    raw = {
        "id": "300",
        "name": "Feed ad",
        "asset_feed_spec": {
            "titles": [
                {"text": "FR titre", "adlabels": [{"name": "fr_title", "id": "1"}]},
                {"text": "EN title", "adlabels": [{"name": "en_title", "id": "2"}]},
            ],
            "descriptions": [
                {"text": "EN desc", "adlabels": [{"name": "en_desc", "id": "3"}]},
                {"text": "FR desc", "adlabels": [{"name": "fr_desc", "id": "4"}]},
            ],
            "bodies": [
                {"text": "FR corps", "adlabels": [{"name": "fr_body", "id": "5"}]},
                {"text": "EN body", "adlabels": [{"name": "en_body", "id": "6"}]},
            ],
            "link_urls": [
                {"website_url": "https://example.com/fr", "adlabels": [{"name": "fr_url", "id": "7"}]},
                {"website_url": "https://example.com/en", "adlabels": [{"name": "en_url", "id": "8"}]},
            ],
            "images": [{"hash": "img1", "adlabels": [{"name": "img", "id": "9"}]}],
            "call_to_action_types": ["SIGN_UP"],
            "asset_customization_rules": [{"customization_spec": {"locales": [6]}}],
            "optimization_type": "DEGREES_OF_FREEDOM",
        },
    }
    raw.update(extra)
    return creatives.Creative("300", "Feed ad", creatives.classify(raw), raw)


def catalog_creative():
    raw = {
        "id": "400",
        "name": "{{product.name}} catalog",
        "object_story_spec": {"page_id": "PAGE", "instagram_user_id": "IG"},
    }
    return creatives.Creative("400", raw["name"], creatives.classify(raw), raw)


# --- classification ---------------------------------------------------------


def test_every_shape_is_recognised():
    assert link_creative().shape == creatives.LINK
    assert video_creative().shape == creatives.VIDEO
    assert feed_creative().shape == creatives.ASSET_FEED
    assert catalog_creative().shape == creatives.PAGE_ONLY


def test_a_catalog_creative_has_no_text_to_edit():
    """Its copy comes from the product feed, not the creative."""
    assert catalog_creative().editable_text is False
    assert catalog_creative().current_text() == {}


# --- the UI-name to API-name mapping ----------------------------------------


def test_headline_means_three_different_fields():
    """`name` on a link, `title` on a video, `titles[]` on a feed. Getting this
    wrong writes the headline into the body."""
    assert creatives.TEXT_FIELDS[creatives.LINK]["headline"] == "name"
    assert creatives.TEXT_FIELDS[creatives.VIDEO]["headline"] == "title"
    assert creatives.TEXT_FIELDS[creatives.ASSET_FEED]["headline"] == "titles"


def test_current_text_speaks_the_ui_vocabulary():
    for creative in (link_creative(), video_creative()):
        text = creative.current_text()
        assert text["primary_text"] == "primary text here"
        assert text["headline"] == "the headline"
        assert text["description"] == "the description"


def test_a_feed_reports_every_locale_not_just_the_first():
    text = feed_creative().current_text()
    assert text["headline"] == ["FR titre", "EN title"]
    assert text["description"] == ["EN desc", "FR desc"]
    assert text["link"] == ["https://example.com/fr", "https://example.com/en"]
    assert feed_creative().describe()["locales"] == 2


# --- editing keeps the shape ------------------------------------------------


def test_editing_a_link_keeps_its_image_and_cta():
    spec = creatives.build_spec(link_creative(), {"headline": "new"})
    block = spec["object_story_spec"]["link_data"]
    assert block["name"] == "new"
    assert block["message"] == "primary text here", "untouched text was lost"
    assert block["image_hash"] == "abc123", "the image was lost"
    assert block["call_to_action"] == {"type": "SIGN_UP"}


def test_editing_a_video_keeps_its_video():
    """A video ad must not silently become an image ad."""
    spec = creatives.build_spec(video_creative(), {"primary_text": "new body"})
    block = spec["object_story_spec"]["video_data"]
    assert block["video_id"] == "VID"
    assert block["message"] == "new body"
    assert block["title"] == "the headline"


def test_editing_keeps_the_page_and_instagram_account():
    spec = creatives.build_spec(link_creative(), {"headline": "new"})
    assert spec["object_story_spec"]["page_id"] == "PAGE"
    assert spec["object_story_spec"]["instagram_user_id"] == "IG"


# --- the asset feed, where the damage would be worst ------------------------


def test_a_single_string_edits_every_locale_and_keeps_every_adlabel():
    """asset_customization_rules reference entries BY ADLABEL. Replacing the
    list with a bare string breaks the rules that point at it, and Meta rejects
    the creative with a message that names none of this."""
    before = feed_creative().raw["asset_feed_spec"]["titles"]
    spec = creatives.build_spec(feed_creative(), {"headline": "NEW"})
    after = spec["asset_feed_spec"]["titles"]

    assert [e["text"] for e in after] == ["NEW", "NEW"]
    assert [e["adlabels"] for e in after] == [e["adlabels"] for e in before]


def test_a_per_locale_list_maps_position_for_position():
    spec = creatives.build_spec(feed_creative(), {"headline": ["FR new", "EN new"]})
    after = spec["asset_feed_spec"]["titles"]
    assert [e["text"] for e in after] == ["FR new", "EN new"]
    assert after[0]["adlabels"][0]["name"] == "fr_title"


def test_a_wrong_length_list_is_refused_with_the_right_number():
    with pytest.raises(MutationError) as caught:
        creatives.build_spec(feed_creative(), {"headline": ["only one"]})
    assert "2 entries" in str(caught.value)


def test_editing_a_feed_carries_the_customization_rules_and_media():
    spec = creatives.build_spec(feed_creative(), {"headline": "NEW"})
    feed = spec["asset_feed_spec"]
    assert feed["asset_customization_rules"], "the rules were dropped"
    assert feed["images"], "the image was dropped"
    assert feed["call_to_action_types"] == ["SIGN_UP"]
    assert feed["optimization_type"] == "DEGREES_OF_FREEDOM"


def test_a_link_edit_on_a_feed_writes_website_url_not_text():
    spec = creatives.build_spec(feed_creative(), {"link": "https://example.com/new"})
    entry = spec["asset_feed_spec"]["link_urls"][0]
    assert entry["website_url"] == "https://example.com/new"
    assert "text" not in entry


# --- refusals ---------------------------------------------------------------


def test_an_unknown_edit_is_refused_rather_than_dropped():
    """A silently dropped edit reads as 'Meta rejected my copy'."""
    with pytest.raises(MutationError) as caught:
        creatives.build_spec(link_creative(), {"call_to_action": "SHOP_NOW"})
    assert "call_to_action" in str(caught.value)


def test_editing_a_catalog_creative_is_refused_with_the_reason():
    with pytest.raises(MutationError) as caught:
        creatives.build_spec(catalog_creative(), {"headline": "new"})
    assert "product catalog" in str(caught.value)


def test_no_edits_at_all_is_refused():
    with pytest.raises(MutationError):
        creatives.build_spec(link_creative(), {})


# --- the new-creative payload -----------------------------------------------


def test_the_payload_carries_tracking_and_the_advantage_opt_outs():
    """Dropping degrees_of_freedom_spec silently OPTS THE NEW CREATIVE INTO
    every automatic enhancement the old one had turned off."""
    dof = {"creative_features_spec": {"standard_enhancements": {"enroll_status": "OPT_OUT"}}}
    creative = link_creative(url_tags="utm_source=facebook", degrees_of_freedom_spec=dof)
    payload = creatives.plan_payload(creative, {"headline": "new"})
    assert payload["url_tags"] == "utm_source=facebook"
    assert payload["degrees_of_freedom_spec"] == dof
    assert payload["name"]
