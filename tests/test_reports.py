"""Report definitions, checked without a single API call."""

from __future__ import annotations

import pytest

from metaads_reporting.fields import unknown_money_fields
from metaads_reporting.insights import InsightsError, LEVELS
from metaads_reporting.reports import REGISTRY, Report, get_report, report_names


def test_every_report_is_registered_under_its_own_name():
    for name, report in REGISTRY.items():
        assert report.name == name


def test_every_report_has_a_valid_level():
    for report in REGISTRY.values():
        assert report.level in LEVELS, report.name


def test_no_report_selects_a_field_with_no_unit_ruling():
    """The check that keeps a column from being off by a hundred."""
    for report in REGISTRY.values():
        assert unknown_money_fields(report.fields) == (), report.name


def test_a_report_selecting_an_unruled_money_field_refuses_to_exist():
    with pytest.raises(InsightsError) as caught:
        Report(
            name="bad",
            description="",
            level="campaign",
            fields=("cost_per_wombat",),
        )
    assert "unit ruling" in str(caught.value)


def test_a_report_with_no_fields_refuses_to_exist():
    with pytest.raises(InsightsError):
        Report(name="empty", description="", level="campaign", fields=())


def test_columns_include_the_two_meta_adds_to_every_row():
    """date_start/date_stop are the only way to tell what a row covers once
    time_increment is in play."""
    for report in REGISTRY.values():
        assert report.columns[-2:] == ("date_start", "date_stop")


def test_build_produces_a_usable_query():
    query = get_report("campaigns").build()
    params = query.to_params()
    assert params["level"] == "campaign"
    assert "spend" in params["fields"]


def test_build_accepts_an_explicit_range():
    query = get_report("campaigns").build(since="2026-09-01", until="2026-09-30")
    assert query.since == "2026-09-01"
    assert "time_range" in query.to_params()


def test_build_refuses_a_preset_and_a_range_together():
    with pytest.raises(InsightsError):
        get_report("campaigns").build(
            preset="last_7d", since="2026-09-01", until="2026-09-30"
        )


def test_time_increment_minus_one_means_use_the_reports_own():
    """None has to stay available as 'one summary row', which is a real answer."""
    campaigns = get_report("campaigns")
    assert campaigns.build().time_increment == campaigns.time_increment
    assert campaigns.build(time_increment=None).time_increment is None
    assert campaigns.build(time_increment=7).time_increment == 7


def test_creative_fatigue_is_not_daily_and_that_is_the_point():
    """Frequency is impressions over REACH, and reach cannot be summed across
    days -- so a daily frequency column looks flat and reassuring on exactly
    the ad that is burning out."""
    report = get_report("creative_fatigue")
    assert report.time_increment is None
    assert "frequency" in report.fields
    assert "reach" in report.fields


def test_the_daily_reports_are_daily():
    for name in ("account", "campaigns", "adsets", "ads"):
        assert get_report(name).time_increment == 1, name


def test_an_unknown_report_lists_the_known_ones():
    with pytest.raises(KeyError) as caught:
        get_report("nope")
    for name in report_names():
        assert name in str(caught.value)


def test_entity_reports_carry_the_id_and_the_name():
    """An id alone is unreadable in a report a human opens; Meta does not join
    the name on unless it is asked for."""
    for name, key in (
        ("campaigns", "campaign"),
        ("adsets", "adset"),
        ("ads", "ad"),
    ):
        fields = get_report(name).fields
        assert f"{key}_id" in fields, name
        assert f"{key}_name" in fields, name
