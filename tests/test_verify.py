"""Ad verification: previews and checks before an ad change, a diff after.

Driven on the simulator. Link checks run on stubbed sessions and resolvers --
nothing here reaches a real website.
"""

from __future__ import annotations

import json
import struct

import pytest

from metaads_mcp import ledger, verify
from metaads_mcp.changes import all_kinds
from metaads_reporting import linkcheck
from metaads_reporting.write.media import image_dimensions
from tests.fakes import FakeResponse
from tests.harness import build_tools
from tests.test_mcp_creative_path import AD_ID, CREATIVE_ID, make_sim, png_bytes


@pytest.fixture
def tools(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, state, sim


#: Kinds that create or change an ad or a creative. A new one belongs here AND
#: must declare verifies_ad -- this is the test that fails if it does not.
AD_CHANGING = {
    "duplicate_ad", "update_ad_copy", "create_ad", "create_creative",
    "replace_ad_creative", "url_tags", "attach_lead_form",
    "advantage_creative_options", "placement_asset_customization",
}


def test_every_ad_changing_kind_declares_verification():
    for kind in all_kinds():
        if kind.name in AD_CHANGING:
            assert kind.verifies_ad, f"{kind.name} changes an ad but skips verification"


# --- before apply -----------------------------------------------------------


def test_a_creative_preview_carries_preview_links_per_placement(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    assert preview.previews, "no previews"
    assert len(preview.previews) <= state.limits.max_preview_formats
    first = preview.previews[0]
    assert first["iframe_url"].startswith("https://business.facebook.com/")
    assert "&amp;" not in first["iframe_url"]
    assert first["ad_format"] == "MOBILE_FEED_STANDARD"


def test_the_ad_sets_own_placements_decide_what_is_previewed(monkeypatch):
    sim = make_sim()
    sim.objects["120200000000002"]["targeting"] = {
        "publisher_platforms": ["instagram"], "instagram_positions": ["story", "reels"],
    }
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    assert [p["ad_format"] for p in preview.previews] == ["INSTAGRAM_STORY", "INSTAGRAM_REELS"]


def test_a_refused_format_is_reported_not_fatal(tools):
    tool, state, sim = tools
    sim.refuse_formats = {"INSTAGRAM_STORY"}
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    story = next(p for p in preview.previews if p["ad_format"] == "INSTAGRAM_STORY")
    assert "refused" in story["unavailable"]
    assert preview.blocked is False


def test_a_pending_upload_has_no_preview_yet(tools, tmp_path):
    tool, state, sim = tools
    image = tmp_path / "c.png"
    image.write_bytes(png_bytes(1080, 1080))
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, image_path=str(image))
    assert all(p.get("unavailable") == "image not uploaded until apply" for p in preview.previews)


def test_long_primary_text_warns_where_it_is_cut(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, primary_text="x" * 140)
    line = next(c for c in preview.checks if "ad_text_primary_text" in c)
    assert line.startswith("WARN") and "Facebook feed" in line
    assert preview.blocked is False


def test_a_square_image_warns_in_stories_from_its_header(tools, tmp_path):
    tool, state, sim = tools
    image = tmp_path / "square.png"
    image.write_bytes(png_bytes(1080, 1080))
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, image_path=str(image))
    line = next(c for c in preview.checks if "ad_image_shape" in c)
    assert line.startswith("WARN") and "Stories" in line


def test_a_tiny_image_fails(tools, tmp_path):
    tool, state, sim = tools
    image = tmp_path / "tiny.png"
    image.write_bytes(png_bytes(120, 120))
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, image_path=str(image))
    assert preview.blocked is True
    assert any(c.startswith("FAIL ad_image_width") for c in preview.checks)


def test_a_video_longer_than_a_placement_plays_warns():
    checks = verify.video_checks(200.0, ["FACEBOOK_REELS_MOBILE", "MOBILE_FEED_STANDARD"])
    assert checks[0].warn and "Facebook Reels" in checks[0].detail


def test_a_lead_objective_without_a_cta_fails():
    assert verify.cta_check(None, "OUTCOME_LEADS").ok is False
    assert verify.cta_check(None, "OUTCOME_AWARENESS").warn is True
    assert verify.cta_check("SIGN_UP", "OUTCOME_LEADS").ok is True


def test_a_broken_destination_blocks_the_preview(tools, monkeypatch):
    tool, state, sim = tools

    class NotFound:
        def request(self, method, url, **kw):
            return FakeResponse({}, status=404)

        def close(self):
            pass

    monkeypatch.setattr("metaads_reporting.linkcheck._new_session", lambda: NotFound())
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    assert preview.blocked is True
    assert any(c.startswith("FAIL ad_destination") and "404" in c for c in preview.checks)


# --- after apply --------------------------------------------------------------


def test_an_apply_returns_a_passing_verification(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new headline")
    result = tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    check = result.verification
    assert check is not None and check["passed"] is True, check
    headline = next(f for f in check["fields"] if f["field"] == "headline")
    assert headline == {"field": "headline", "approved": "new headline",
                        "live": "new headline", "ok": True}
    assert check["previews"] and check["previews"][0]["iframe_url"]


def test_a_planted_mismatch_is_flagged_and_nothing_is_reverted(tools):
    tool, state, sim = tools
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="approved headline")
    real_create = sim._create

    def tampering(edge, body):
        if edge == "adcreatives" and "validate_only" not in str(body.get("execution_options")):
            spec = json.loads(body["object_story_spec"])
            spec["link_data"]["name"] = "something else"
            body = {**body, "object_story_spec": json.dumps(spec), "url_tags": "utm_x=1"}
        return real_create(edge, body)

    sim._create = tampering
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    check = result.verification
    assert check["passed"] is False
    bad = {f["field"] for f in check["fields"] if not f["ok"]}
    assert "headline" in bad
    assert "headline" in check["note"]
    assert result.applied is True  # recorded, not rolled back


@pytest.mark.parametrize(
    "ad, state",
    [
        ({"effective_status": "PENDING_REVIEW"}, "pending"),
        ({"effective_status": "IN_PROCESS"}, "pending"),
        ({"effective_status": "DISAPPROVED",
          "ad_review_feedback": {"global": {"Text": "policy"}}}, "disapproved"),
        ({"effective_status": "WITH_ISSUES",
          "issues_info": [{"error_summary": "bad"}]}, "with_issues"),
        ({"effective_status": "PAUSED"}, "not_flagged"),
    ],
)
def test_review_states_map(ad, state):
    review = verify.review_state(ad)
    assert review["state"] == state
    if "ad_review_feedback" in ad:
        assert review["ad_review_feedback"]
    if "issues_info" in ad:
        assert review["issues_info"]


def test_a_disapproved_ad_fails_verification(tools):
    tool, state, sim = tools
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new")
    sim.objects[AD_ID]["ad_review_feedback"] = {"global": {"Text": "policy"}}
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    assert result.verification["review"]["state"] == "disapproved"
    assert result.verification["passed"] is False


def test_preview_urls_never_reach_the_ledger(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    result = tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    entry = ledger.find_entry(result.entry_id)
    assert entry["verification"]["previews"], "placements should be kept"
    from metaads_reporting import logdb

    dump = json.dumps([dict(r) for r in logdb.connect().execute("SELECT * FROM changes")])
    assert "preview_iframe" not in dump and "iframe_url" not in dump


# --- the link checker, offline ------------------------------------------------


class Scripted:
    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def request(self, method, url, **kw):
        self.seen.append((method, url))
        status, location = self.answers.pop(0)
        return FakeResponse({}, status=status, headers={"Location": location} if location else {})

    def close(self):
        pass


PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def test_a_redirect_chain_is_followed_to_the_end():
    session = Scripted([(301, "/b"), (302, "https://example.com/c"), (200, None)])
    result = linkcheck.check_link("https://example.com/a", session=session, resolver=PUBLIC)
    assert result.ok and result.final_url == "https://example.com/c"
    assert len(result.hops) == 2
    assert {m for m, _ in session.seen} == {"HEAD"}


def test_a_404_is_not_ok():
    result = linkcheck.check_link(
        "https://example.com/gone", session=Scripted([(404, None)]), resolver=PUBLIC
    )
    assert not result.ok and result.status == 404


def test_a_redirect_to_another_host_is_noticed():
    session = Scripted([(301, "https://example.org/x"), (200, None)])
    result = linkcheck.check_link("https://example.com/a", session=session, resolver=PUBLIC)
    assert result.changed_host


@pytest.mark.parametrize(
    "url, address",
    [
        ("http://127.0.0.1/admin", "127.0.0.1"),
        ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
        ("http://intranet.internal/", "10.1.2.3"),
    ],
)
def test_private_addresses_are_refused_before_any_request(url, address):
    session = Scripted([])
    result = linkcheck.check_link(url, session=session, resolver=lambda h: [address])
    assert result.refused and session.seen == []


def test_a_redirect_into_a_private_address_is_refused():
    session = Scripted([(302, "http://169.254.169.254/")])

    def resolver(host):
        return ["169.254.169.254"] if host == "169.254.169.254" else ["93.184.216.34"]

    result = linkcheck.check_link("https://example.com/a", session=session, resolver=resolver)
    assert result.refused and len(session.seen) == 1


def test_file_urls_are_refused():
    result = linkcheck.check_link("file:///etc/passwd", session=Scripted([]), resolver=PUBLIC)
    assert "http(s)" in result.refused


def test_the_link_checker_names_no_write_method():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(linkcheck))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not strings & {"POST", "PUT", "PATCH", "DELETE"}


# --- image headers ---------------------------------------------------------------


def _jpeg(w, h):
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, h, w) + b"\x00" * 12)


def _webp_vp8x(w, h):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 8
            + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


@pytest.mark.parametrize(
    "data, expected",
    [
        (png_bytes(1080, 1350), (1080, 1350)),
        (b"GIF89a" + struct.pack("<HH", 600, 314), (600, 314)),
        (_jpeg(1080, 1920), (1080, 1920)),
        (_webp_vp8x(1200, 628), (1200, 628)),
    ],
)
def test_image_dimensions_from_the_header(data, expected):
    assert image_dimensions(data) == expected


def test_the_creative_fixture_is_still_untouched(tools):
    """Sanity: a preview reads the creative and leaves it alone."""
    tool, state, sim = tools
    before = json.dumps(sim.objects[CREATIVE_ID], sort_keys=True)
    tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new")
    assert json.dumps(sim.objects[CREATIVE_ID], sort_keys=True) == before


# --- the SSRF guard: pinned connections, shared address space ------------------


class Recording(Scripted):
    def request(self, method, url, **kw):
        self.headers = getattr(self, "headers", []) + [dict(kw.get("headers") or {})]
        return super().request(method, url, **kw)


@pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254", "::ffff:10.0.0.1",
                                     "64:ff9b::7f00:1", "2002:7f00:1::1", "192.0.0.8",
                                     "198.18.0.1", "0.0.0.0"])
def test_every_non_global_address_is_refused(address):
    session = Scripted([])
    result = linkcheck.check_link("http://example.com/", session=session, resolver=lambda h: [address])
    assert result.refused and session.seen == [], address


def test_the_request_goes_to_the_address_that_was_checked():
    """DNS rebinding: a second lookup would answer differently. There is none."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]
    calls = []

    def rebinding(host):
        calls.append(host)
        return answers[min(len(calls) - 1, 1)]

    session = Recording([(200, None)])
    result = linkcheck.check_link("https://example.com:8443/a?b=1", session=session, resolver=rebinding)
    assert result.ok and calls == ["example.com"], "resolved exactly once"
    method, url = session.seen[0]
    assert url == "https://93.184.216.34:8443/a?b=1", url
    assert session.headers[0]["Host"] == "example.com:8443"
    assert result.final_url == "https://example.com:8443/a?b=1"


def test_every_redirect_hop_is_resolved_once_and_pinned():
    answers = {"example.com": [["93.184.216.34"]], "example.org": [["93.184.216.35"], ["127.0.0.1"]]}

    def resolver(host):
        return answers[host].pop(0)

    session = Recording([(302, "https://example.org/x"), (200, None)])
    result = linkcheck.check_link("https://example.com/a", session=session, resolver=resolver)
    assert result.ok
    assert [u for _, u in session.seen] == ["https://93.184.216.34/a", "https://93.184.216.35/x"]
    assert session.headers[1]["Host"] == "example.org"


def test_a_redirect_into_shared_address_space_is_refused():
    session = Scripted([(302, "http://cgnat.example/")])

    def resolver(host):
        return ["100.64.1.1"] if host == "cgnat.example" else ["93.184.216.34"]

    result = linkcheck.check_link("https://example.com/a", session=session, resolver=resolver)
    assert result.refused and len(session.seen) == 1


def test_the_tls_adapter_verifies_and_sends_sni_for_the_hostname():
    adapter = linkcheck._PinnedTLSAdapter("example.com")
    kw = adapter.poolmanager.connection_pool_kw
    assert kw["server_hostname"] == "example.com" and kw["assert_hostname"] == "example.com"


def test_a_pinned_request_really_connects_to_the_pinned_address():
    """End to end over a real socket: the host name does not resolve at all,
    so the request can only have reached the server through the pinned IP."""
    import http.server
    import threading

    import requests

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen["host"] = self.headers.get("Host")
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with requests.Session() as session:
            session.trust_env = False
            response = linkcheck._request_pinned(
                session, "HEAD", f"http://unresolvable.invalid:{port}/p", "127.0.0.1")
        assert response.status_code == 204
        assert seen["host"] == f"unresolvable.invalid:{port}"
    finally:
        server.server_close()
