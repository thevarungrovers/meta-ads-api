"""Ad account ID normalization: the `act_` prefix lives in exactly one place."""

from __future__ import annotations

import pytest

from metaads_reporting.account_id import (
    AccountIdError,
    account_path,
    normalize_account_id,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("act_1234567890", "1234567890"),
        ("1234567890", "1234567890"),
        (1234567890, "1234567890"),
        ("  act_1234567890  ", "1234567890"),
        ("/v23.0/act_1234567890/campaigns", "1234567890"),
        ("https://graph.facebook.com/v23.0/act_1234567890", "1234567890"),
    ],
)
def test_every_spelling_normalizes_to_bare_digits(value, expected):
    assert normalize_account_id(value) == expected


def test_account_path_adds_the_prefix_exactly_once():
    assert account_path("1234567890") == "act_1234567890"
    assert account_path("act_1234567890") == "act_1234567890"
    assert account_path(account_path("1234567890")) == "act_1234567890"


def test_a_bare_id_is_not_silently_accepted_as_a_path():
    """`GET /1234567890/campaigns` is not an error -- it is a DIFFERENT object.

    Which is why the prefix is added by account_path and never assumed to be
    already there.
    """
    assert account_path("1234567890").startswith("act_")


@pytest.mark.parametrize("value", [None, True, False, "", "   ", "abc", [], {}])
def test_rubbish_is_refused(value):
    with pytest.raises(AccountIdError):
        normalize_account_id(value)


def test_an_implausibly_short_id_is_refused():
    with pytest.raises(AccountIdError) as caught:
        normalize_account_id("123")
    assert "too short" in str(caught.value)


def test_separators_people_actually_paste_are_stripped():
    assert normalize_account_id("123 456 7890") == "1234567890"
    assert normalize_account_id("123-456-7890") == "1234567890"
