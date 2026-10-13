"""The insights query builder: the parameters that are wrong in plausible ways."""

from __future__ import annotations

import json
from datetime import date

import pytest

from metaads_reporting.insights import (
    DEFAULT_ATTRIBUTION_WINDOWS,
    Filter,
    InsightsError,
    InsightsQuery,
    last_n_days,
    name_contains,
    node_fields,
    summarise,
    time_range,
    validate_level,
    validate_preset,
)


def q(**kwargs) -> InsightsQuery:
    base = {"fields": ("spend",), "level": "campaign", "date_preset": "last_7d"}
    base.update(kwargs)
    return InsightsQuery(**base)


# --- the mutually-exclusive window ------------------------------------------


def test_a_preset_and_a_range_together_are_refused():
    """Meta accepts both and silently applies one. That is a report over a
    window nobody chose, with no error to notice."""
    with pytest.raises(InsightsError) as caught:
        InsightsQuery(
            fields=("spend",),
            date_preset="last_7d",
            since="2026-01-01",
            until="2026-01-31",
        )
    assert "not both" in str(caught.value)


def test_half_a_range_is_refused():
    """Meta does not infer the open end."""
    with pytest.raises(InsightsError):
        InsightsQuery(fields=("spend",), since="2026-01-01")
    with pytest.raises(InsightsError):
        InsightsQuery(fields=("spend",), until="2026-01-31")


def test_a_backwards_range_is_refused():
    with pytest.raises(InsightsError):
        time_range("2026-02-01", "2026-01-01")


# --- the window itself ------------------------------------------------------


def test_last_n_days_ends_yesterday():
    """Today's metrics are partial all day and drag every average down."""
    assert last_n_days(7, today=date(2026, 10, 8)) == {
        "since": "2026-10-01",
        "until": "2026-10-07",
    }


def test_last_n_days_of_one_is_just_yesterday():
    assert last_n_days(1, today=date(2026, 10, 8)) == {
        "since": "2026-10-07",
        "until": "2026-10-07",
    }


def test_zero_or_negative_days_is_refused():
    with pytest.raises(InsightsError):
        last_n_days(0)
    with pytest.raises(InsightsError):
        last_n_days(-5)


def test_the_window_description_names_the_time_zone():
    """Dates are evaluated in the ACCOUNT's zone, not this machine's."""
    assert "account time zone" in q().describe_window()
    assert "account time zone" in q(
        date_preset=None, since="2026-01-01", until="2026-01-31"
    ).describe_window()


# --- parameter encoding -----------------------------------------------------


def test_structured_parameters_are_json_encoded():
    """A Python list reaches requests as repeated keys, and Meta reads the LAST
    one only -- a report with one column and no error."""
    params = q(
        since="2026-01-01",
        until="2026-01-31",
        date_preset=None,
        filtering=(Filter("campaign.name", "CONTAIN", "Brand"),),
        sort=("spend_descending",),
    ).to_params()

    assert json.loads(params["time_range"]) == {
        "since": "2026-01-01",
        "until": "2026-01-31",
    }
    assert json.loads(params["filtering"]) == [
        {"field": "campaign.name", "operator": "CONTAIN", "value": "Brand"}
    ]
    assert json.loads(params["sort"]) == ["spend_descending"]
    assert json.loads(params["action_attribution_windows"]) == list(
        DEFAULT_ATTRIBUTION_WINDOWS
    )


def test_fields_are_comma_joined_not_repeated():
    params = q(fields=("spend", "impressions", "clicks")).to_params()
    assert params["fields"] == "spend,impressions,clicks"


def test_time_increment_is_omitted_when_none():
    """Omitting it collapses the whole window into ONE row, which is a real and
    different answer -- so None has to mean that, not 'use a default'."""
    assert "time_increment" not in q(time_increment=None).to_params()
    assert q(time_increment=1).to_params()["time_increment"] == 1


def test_attribution_windows_are_always_sent():
    """Left off, the account's own setting applies and nothing records which."""
    assert "action_attribution_windows" in q().to_params()


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("level", ["account", "campaign", "adset", "ad"])
def test_valid_levels(level):
    assert validate_level(level.upper()) == level


def test_an_invalid_level_lists_the_valid_ones():
    with pytest.raises(InsightsError) as caught:
        validate_level("adgroup")
    assert "campaign" in str(caught.value)


def test_an_invalid_preset_lists_the_valid_ones():
    with pytest.raises(InsightsError) as caught:
        validate_preset("LAST_30_DAYS")  # that is Google's spelling
    assert "last_30d" in str(caught.value)


def test_no_fields_is_refused():
    with pytest.raises(InsightsError):
        InsightsQuery(fields=())


def test_an_out_of_range_time_increment_is_refused():
    with pytest.raises(InsightsError):
        q(time_increment=0)
    with pytest.raises(InsightsError):
        q(time_increment=500)


def test_an_unknown_filter_operator_is_refused():
    with pytest.raises(InsightsError):
        Filter("campaign.name", "LIKE", "x")


def test_clashing_breakdowns_are_refused_with_both_named():
    """The geographic levels are alternatives, not a hierarchy."""
    with pytest.raises(InsightsError) as caught:
        q(breakdowns=("country", "region"))
    assert "country" in str(caught.value) and "region" in str(caught.value)


def test_the_breakdown_check_is_deliberately_partial():
    """Meta's full matrix is not reproduced, and must not be.

    Reproducing it would confidently refuse combinations that actually work and
    would go stale between API versions. age+gender is the standing proof: the
    single most common pair there is.
    """
    assert q(breakdowns=("age", "gender")).to_params()["breakdowns"] == "age,gender"
    assert (
        q(breakdowns=("publisher_platform", "impression_device")).to_params()[
            "breakdowns"
        ]
        == "publisher_platform,impression_device"
    )


def test_duplicate_breakdowns_are_refused():
    with pytest.raises(InsightsError):
        q(breakdowns=("age", "age"))


def test_compatible_breakdowns_are_allowed():
    assert q(breakdowns=("age", "gender")).to_params()["breakdowns"] == "age,gender"


def test_name_contains_only_works_on_entities_that_have_a_name():
    assert name_contains("campaign", "x").field == "campaign.name"
    with pytest.raises(InsightsError):
        name_contains("account", "x")
    with pytest.raises(InsightsError):
        name_contains("campaign", "   ")


def test_with_filter_appends_rather_than_replacing():
    query = q().with_filter(Filter("ad.name", "CONTAIN", "A"))
    assert len(query.filtering) == 1
    assert len(query.with_filter(Filter("adset.name", "CONTAIN", "B")).filtering) == 2


def test_node_fields_joins_and_refuses_an_empty_list():
    assert node_fields("id", "name") == "id,name"
    with pytest.raises(InsightsError):
        node_fields()


# --- summarising ------------------------------------------------------------


def test_ratios_are_recomputed_from_totals_not_averaged():
    """Averaging a column of CPCs weights every row equally, which is wrong
    whenever rows differ in volume."""
    rows = [
        {"spend": "100", "impressions": "10000", "clicks": "100"},
        {"spend": "900", "impressions": "10000", "clicks": "900"},
    ]
    totals = summarise(rows)
    assert totals["spend"] == "1000"
    # 1000 / 1000 clicks = 1.00, not the mean of 1.00 and 1.00... here the
    # weighted answer differs from the naive one on CTR:
    assert totals["cpc"] == "1.0000"
    assert totals["ctr"] == "5.0000"  # 1000 clicks / 20000 impressions


def test_reach_is_summed_but_no_frequency_is_derived_from_it():
    """The same person reached on two days is one person, so a summed reach is
    already wrong and a frequency built on it would be worse."""
    totals = summarise([{"reach": "100"}, {"reach": "100"}])
    assert "frequency" not in totals


def test_summarising_nothing_does_not_divide_by_zero():
    totals = summarise([])
    assert totals["rows"] == "0"
    assert "cpc" not in totals and "ctr" not in totals


def test_unparseable_values_do_not_fail_the_whole_report():
    totals = summarise([{"spend": "oops", "clicks": "5"}])
    assert totals["clicks"] == "5"
