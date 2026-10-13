"""The minor-unit rule. The single most dangerous thing in this repo.

Every monetary field on a Meta campaign, ad set or ad is an integer in the
account currency's MINOR unit, the divisor depends on the currency, and
insights fields go the other way entirely. Getting any of that backwards
produces a plausible number rather than an error.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_reporting.currency import (
    THREE_DECIMAL,
    TWO_DECIMAL,
    ZERO_DECIMAL,
    CurrencyError,
    divisor,
    format_money,
    from_minor_units,
    minor_unit_exponent,
    to_minor_units,
)


def test_two_decimal_currency_divides_by_a_hundred():
    assert from_minor_units(5000, "USD") == Decimal("50.00")
    assert to_minor_units("50.00", "USD") == 5000


def test_zero_decimal_currency_does_not_divide_at_all():
    """The whole reason the exponent is looked up rather than assumed.

    5000 JPY is five thousand yen. Dividing by 100 renders it as "50.00" --
    a hundredfold understatement, in the text a human is about to approve.
    """
    assert from_minor_units(5000, "JPY") == Decimal("5000")
    assert to_minor_units("5000", "JPY") == 5000
    assert divisor("JPY") == Decimal("1")


def test_three_decimal_currency():
    assert from_minor_units(50000, "KWD") == Decimal("50.000")
    assert to_minor_units("50.000", "KWD") == 50000


def test_an_unknown_currency_is_refused_not_guessed():
    """Refusing beats defaulting to 2: a wrong exponent is silent."""
    with pytest.raises(CurrencyError) as caught:
        minor_unit_exponent("XYZ")
    assert "Refusing to guess" in str(caught.value)


def test_a_blank_currency_is_refused():
    with pytest.raises(CurrencyError):
        minor_unit_exponent("")
    with pytest.raises(CurrencyError):
        minor_unit_exponent(None)


def test_currency_is_case_insensitive_and_trimmed():
    assert minor_unit_exponent("  jpy ") == 0
    assert minor_unit_exponent("usd") == 2


def test_none_passes_through_rather_than_becoming_zero():
    """An ad set with no budget of its own is extremely common on Meta.

    "the campaign holds the budget" and "the budget is zero" are different
    facts, and a zero would read as the second.
    """
    assert from_minor_units(None, "USD") is None
    assert from_minor_units("", "USD") is None


def test_round_trip_is_exact_for_every_supported_currency():
    for code in sorted(ZERO_DECIMAL | TWO_DECIMAL | THREE_DECIMAL):
        exponent = minor_unit_exponent(code)
        raw = 12345
        back = to_minor_units(from_minor_units(raw, code), code)
        assert back == raw, f"{code} (exponent {exponent}) did not round-trip"


def test_excess_precision_is_refused_rather_than_rounded():
    """"1.005" on a USD account is somebody meaning something USD cannot hold."""
    with pytest.raises(CurrencyError) as caught:
        to_minor_units("1.005", "USD")
    assert "more precision" in str(caught.value)
    # And the message says what to pass instead.
    assert "1.01" in str(caught.value) or "1.00" in str(caught.value)


def test_a_whole_number_on_a_zero_decimal_currency_is_fine():
    assert to_minor_units("5000", "JPY") == 5000
    with pytest.raises(CurrencyError):
        to_minor_units("5000.50", "JPY")


def test_format_money_uses_the_currencys_own_precision():
    assert format_money(Decimal("50"), "USD") == "50.00 USD"
    assert format_money(Decimal("5000"), "JPY") == "5000 JPY"
    assert format_money(Decimal("50"), "KWD") == "50.000 KWD"


def test_format_money_says_not_set_rather_than_zero():
    assert format_money(None, "USD") == "(not set)"


def test_the_three_sets_do_not_overlap():
    """A code in two sets would make its exponent depend on check order."""
    assert not ZERO_DECIMAL & TWO_DECIMAL
    assert not ZERO_DECIMAL & THREE_DECIMAL
    assert not TWO_DECIMAL & THREE_DECIMAL


@pytest.mark.parametrize("code", ["USD", "CAD", "EUR", "GBP", "AUD", "JPY", "MXN"])
def test_the_common_ad_currencies_are_all_known(code):
    """Being refused on a real account is worse than carrying extra rows."""
    assert minor_unit_exponent(code) in (0, 2, 3)
