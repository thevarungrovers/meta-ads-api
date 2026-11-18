"""create_ab_test: paused cells, one variable, ended early on revert."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.fakes import ad_set
from tests.kindkit import failed, preview_ok, rig
from tests.test_mcp_write_path import ADSET_ID, make_sim


def future(days):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(microsecond=0).isoformat()


def sim(second_targeting=None, second_status="PAUSED"):
    s = make_sim()
    s.objects[ADSET_ID].update(status="PAUSED", targeting={"geo_locations": {"countries": ["CA"]}})
    s.objects["ADSET_B"] = ad_set("ADSET_B", status=second_status,
                                  targeting=second_targeting or {"geo_locations": {"countries": ["US"]}})
    return s


CELLS = [{"name": "A", "ad_set_ids": [ADSET_ID], "treatment_percentage": 50},
         {"name": "B", "ad_set_ids": ["ADSET_B"], "treatment_percentage": 50}]


def test_a_split_test_between_paused_ad_sets_and_its_revert_ends_it(monkeypatch):
    s = sim()
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_ab_test", s, name="Audience test", variable="audience",
                         cells=CELLS, start_time=future(1), end_time=future(15))
    assert not preview.warnings[:-1], preview.warnings
    result = tools["apply_create_ab_test"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "ad_studies")["payload"]
    assert created["type"] == "SPLIT_TEST"
    tools["revert_change"](entry_id=result.entry_id)
    assert int(s.objects[result.entity_id]["end_time"]) > 0
    assert "DELETE" not in s.methods


def test_cells_that_differ_in_another_variable_warn(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    preview = tools["preview_create_ab_test"](name="x", variable="creative", cells=CELLS,
                                              start_time=future(1), end_time=future(10))
    assert any("not only in creative" in w for w in preview.warnings)


def test_an_active_cell_and_a_bad_split_fail(monkeypatch):
    tools, _ = rig(monkeypatch, sim(second_status="ACTIVE"))
    cells = [dict(CELLS[0]), {**CELLS[1], "treatment_percentage": 40}]
    preview = tools["preview_create_ab_test"](name="x", variable="audience", cells=cells,
                                              start_time=future(1), end_time=future(10))
    assert failed(preview, "cell_paused[ADSET_B]") and failed(preview, "cell_split")


@pytest.mark.parametrize("cells", [CELLS[:1], CELLS * 3])
def test_two_to_five_cells(monkeypatch, cells):
    tools, _ = rig(monkeypatch, sim())
    with pytest.raises(Exception):
        tools["preview_create_ab_test"](name="x", variable="audience", cells=cells,
                                        start_time=future(1), end_time=future(10))
