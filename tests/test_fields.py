"""The unit ruling, and the status/effective_status distinction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_reporting.fields import (
    MAJOR_UNIT_FIELDS,
    MINOR_UNIT_FIELDS,
    FieldPathError,
    action_value,
    as_decimal,
    get_path,
    is_major_unit_field,
    is_minor_unit_field,
    money_value,
    object_to_row,
    unknown_money_fields,
)


# --- the two directions -----------------------------------------------------


def test_node_budget_fields_are_minor_units():
    for name in ("daily_budget", "lifetime_budget", "bid_amount", "spend_cap"):
        assert is_minor_unit_field(name), name
        assert not is_major_unit_field(name), name


def test_insights_spend_fields_are_already_major_units():
    """The opposite direction, and dividing one is a second hundredfold error."""
    for name in ("spend", "cpc", "cpm", "cpp"):
        assert is_major_unit_field(name), name
        assert not is_minor_unit_field(name), name


def test_money_value_converts_a_budget_and_leaves_spend_alone():
    assert money_value({"daily_budget": "5000"}, "daily_budget", "CAD") == Decimal("50.00")
    assert money_value({"spend": "50.00"}, "spend", "CAD") == Decimal("50.00")


def test_the_two_sets_do_not_overlap():
    """A field in both would have its unit decided by check order."""
    assert not MINOR_UNIT_FIELDS & MAJOR_UNIT_FIELDS


def test_a_dotted_path_resolves_the_leaf_for_the_ruling():
    assert is_minor_unit_field("adset.daily_budget")
    assert is_major_unit_field("insights.spend")


# --- refusing what we have no ruling on -------------------------------------


def test_an_unruled_money_field_is_flagged():
    """Better a report that will not load than a column off by a hundred."""
    assert unknown_money_fields(["cost_per_wombat"]) == ("cost_per_wombat",)


def test_known_fields_are_not_flagged():
    assert unknown_money_fields(["spend", "daily_budget", "impressions"]) == ()


def test_fields_that_merely_contain_a_money_word_are_not_flagged():
    """Without this, every report refuses to load over bid_strategy."""
    assert unknown_money_fields(
        ["bid_strategy", "budget_rebalance_flag", "bid_constraints"]
    ) == ()


# --- path access ------------------------------------------------------------


def test_nested_expansion_is_reachable_by_dotted_path():
    assert get_path({"campaign": {"name": "Brand"}}, "campaign.name") == "Brand"


def test_a_missing_path_raises_and_says_why():
    """The Graph API returns ONLY the fields named, so absent usually means
    'you did not ask for it', not 'it has no value'."""
    with pytest.raises(FieldPathError) as caught:
        get_path({"id": "1"}, "name")
    assert "`fields`" in str(caught.value)


def test_a_default_suppresses_the_raise():
    assert get_path({"id": "1"}, "name", default=None) is None


# --- parsing ----------------------------------------------------------------


def test_insight_numbers_arrive_as_strings():
    assert as_decimal("12.34") == Decimal("12.34")
    assert as_decimal(0) == Decimal("0")


def test_unparseable_and_empty_values_become_none_not_zero():
    assert as_decimal(None) is None
    assert as_decimal("") is None
    assert as_decimal("n/a") is None


# --- rows -------------------------------------------------------------------


def test_object_to_row_keeps_column_order_and_converts_money():
    row = object_to_row(
        {"id": "1", "name": "X", "daily_budget": "5000", "spend": "12.34"},
        ("id", "name", "daily_budget", "spend"),
        "CAD",
    )
    assert list(row) == ["id", "name", "daily_budget", "spend"]
    assert row["daily_budget"] == "50.00"
    assert row["spend"] == "12.34"


def test_a_missing_money_field_stays_none():
    row = object_to_row({"id": "1"}, ("id", "daily_budget"), "CAD")
    assert row["daily_budget"] is None


def test_a_zero_decimal_currency_changes_the_rendering():
    row = object_to_row({"daily_budget": "5000"}, ("daily_budget",), "JPY")
    assert row["daily_budget"] == "5000"


# --- actions ----------------------------------------------------------------


def test_an_action_type_is_pulled_out_of_the_list():
    actions = [
        {"action_type": "link_click", "value": "40"},
        {"action_type": "purchase", "value": "3"},
    ]
    assert action_value(actions, "purchase") == Decimal("3")


def test_an_absent_action_type_is_none_not_zero():
    """"nobody purchased" and "Meta did not report purchases" are different."""
    assert action_value([{"action_type": "link_click", "value": "40"}], "purchase") is None
    assert action_value([], "purchase") is None
    assert action_value(None, "purchase") is None
