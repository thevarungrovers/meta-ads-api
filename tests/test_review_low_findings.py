"""Small fixes: the MutationResult string, a create_ad diff against what was
approved, A/B cells in scope, DST-day midnight, and extending a daily flight."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import types
from decimal import Decimal

import pytest

from metaads_reporting.write.client import MutationResult
from tests.fakes import ad_set
from tests.kindkit import failed, preview_ok, rig


# --- (a) MutationResult reads as a sentence ------------------------------------


def test_a_mutation_result_prints_as_a_sentence():
    assert str(MutationResult(validated_only=True, node_id="42")) == "validated: 42"
    assert str(MutationResult(validated_only=False)) == "APPLIED: (new)"


# --- (b) create_ad diffs the live ad against the PREVIEWED creative ------------


def test_create_ad_compares_against_the_creative_as_previewed(monkeypatch):
    from tests.test_kinds_ads import sim
    from tests.test_mcp_creative_path import ADSET_ID, CREATIVE_ID

    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_ad", s, ad_set_id=ADSET_ID, creative_id=CREATIVE_ID,
                         name="New ad")
    story = s.objects[CREATIVE_ID]["object_story_spec"]
    story = json.loads(story) if isinstance(story, str) else json.loads(json.dumps(story))
    story["link_data"]["message"] = "changed after the preview"
    s.objects[CREATIVE_ID]["object_story_spec"] = story
    result = tools["apply_create_ad"](preview_token=preview.preview_token)
    differs = [f["field"] for f in result.verification["fields"] if not f["ok"]]
    assert "primary_text" in differs, result.verification["fields"]


# --- (c) every A/B cell is in an allowed account ---------------------------------


def test_an_ab_test_cell_in_another_account_fails(monkeypatch):
    from tests.test_kinds_studies import CELLS, future
    from tests.test_kinds_studies import sim as study_sim

    s = study_sim()
    s.objects["ADSET_B"]["account_id"] = "9999999999"
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_ab_test"](name="t", variable="audience", cells=CELLS,
                                              start_time=future(1), end_time=future(15))
    assert preview.blocked
    assert any(c.startswith("FAIL") and "9999999999" in c for c in preview.checks), preview.checks


# --- (d) local midnight on a DST day ---------------------------------------------


@pytest.fixture
def toronto():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_local_midnight_uses_the_offset_at_midnight_on_a_dst_day(monkeypatch, toronto):
    from metaads_mcp import guardrails

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            noon = dt.datetime(2026, 3, 8, 12, 0)  # EDT (-4); midnight was EST (-5)
            return noon if tz is None else noon.astimezone(tz)

    monkeypatch.setattr(guardrails, "dt", types.SimpleNamespace(
        datetime=Frozen, timezone=dt.timezone, timedelta=dt.timedelta))
    assert guardrails.local_midnight_utc() == "2026-03-08T05:00:00+00:00"


def test_local_midnight_with_a_zone_aware_now():
    from zoneinfo import ZoneInfo

    from metaads_mcp.guardrails import local_midnight_utc

    now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("America/Toronto"))  # EST; midnight EDT
    assert local_midnight_utc(now) == "2026-11-01T04:00:00+00:00"


# --- (e) extending a daily-budget flight projects the added days ----------------


def test_extending_a_daily_budget_flight_projects_its_daily_budget(monkeypatch):
    from tests.test_kinds_bidding import abo_sim
    from tests.test_mcp_write_path import ADSET_ID

    sim = abo_sim(start_time="2026-01-01T00:00:00+0000",
                  end_time=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat())
    tools, _ = rig(monkeypatch, sim)
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).replace(microsecond=0).isoformat()
    preview = tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, end_time=later)
    # Each added day spends the whole 50.00 daily budget, not the headroom.
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("50.00")
    assert any("projected_delta_per_day" in c and "SKIP" in c for c in preview.checks), preview.checks
