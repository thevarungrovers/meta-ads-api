"""find_*: the whole account is searched, and a cut-off says so."""

from __future__ import annotations

import json

from tests.fakes import campaign
from tests.harness import build_tools
from tests.test_mcp_write_path import make_sim


def many(n):
    sim = make_sim()
    ids = []
    for i in range(n):
        cid = f"C{i:03d}"
        sim.objects[cid] = campaign(cid, name=f"Filler {i}")
        ids.append(cid)
    sim.objects["CBRAND"] = campaign("CBRAND", name="Example Brand Search")
    sim.edges["act_1234567890"]["campaigns"] = ids + ["CBRAND"]
    return sim


def test_a_match_past_the_limit_is_still_found(monkeypatch):
    """The search used to fetch `limit` rows and THEN filter by name."""
    registrar, _ = build_tools(many(60), monkeypatch=monkeypatch)
    out = registrar.tools["find_campaigns"](name_contains_text="brand", limit=5)
    assert [r["campaign_id"] for r in out["rows"]] == ["CBRAND"]
    assert out["truncated"] is False


def test_the_name_goes_to_meta_as_a_contain_filter(monkeypatch):
    sim = many(3)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    registrar.tools["find_campaigns"](name_contains_text="Brand")
    sent = [c["payload"].get("filtering") for c in sim.calls if "campaigns" in c["url"]]
    assert any(f and json.loads(f)[0]["operator"] == "CONTAIN" for f in sent)


def test_truncated_is_true_when_more_matched(monkeypatch):
    registrar, _ = build_tools(many(30), monkeypatch=monkeypatch)
    out = registrar.tools["find_campaigns"](limit=10)
    assert out["count"] == 10 and out["truncated"] is True


def test_status_is_an_effective_status_filter(monkeypatch):
    sim = many(2)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    registrar.tools["find_campaigns"](status="CAMPAIGN_PAUSED")
    sent = [c["payload"].get("effective_status") for c in sim.calls if "campaigns" in c["url"]]
    assert json.loads(sent[-1]) == ["CAMPAIGN_PAUSED"]
