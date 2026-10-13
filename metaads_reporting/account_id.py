"""Ad account ID normalization.

Meta writes an ad account ID three different ways and means the same thing by
all of them: ``act_1234567890`` in the Graph API path, ``1234567890`` in the
Ads Manager URL's ``act=`` parameter, and ``1234567890`` again in the account
list. Every ID entering this package -- from ``.env``, from a CLI flag, from an
API response -- goes through :func:`normalize_account_id` first, so exactly one
form reaches a request.

The ``act_`` prefix is the one that matters. ``GET /v23.0/1234567890/campaigns``
is not an error: ``1234567890`` is a perfectly good Graph object ID, it is just
not *this* account, so the API answers about something else or 404s on a
permission boundary. Prefixing is not cosmetic.
"""

from __future__ import annotations

import re

#: Characters people legitimately paste around an ID: spaces, underscores,
#: dashes, and non-breaking spaces from rich-text copy/paste.
_SEPARATORS = re.compile(r"[-\s ​]+")

_ACT_PREFIX = "act_"


class AccountIdError(ValueError):
    """Raised when a value cannot be read as a Meta ad account ID."""


def normalize_account_id(value: object) -> str:
    """Return ``value`` as a bare numeric ad account ID, with no ``act_``.

    Accepts the prefixed form, the bare form, an ``int``, and a full Graph
    path. The ``act_`` prefix is added back by :func:`account_path`, so it
    lives in exactly one place rather than being half-present in config.

    >>> normalize_account_id("act_1234567890")
    '1234567890'
    >>> normalize_account_id(1234567890)
    '1234567890'
    >>> normalize_account_id("/v23.0/act_1234567890/campaigns")
    '1234567890'
    """
    if value is None or isinstance(value, bool):
        raise AccountIdError(f"Not an ad account ID: {value!r}")

    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise AccountIdError(f"Not an ad account ID: {value!r}")

    if not text:
        raise AccountIdError("Ad account ID is empty.")

    # A full or partial Graph path: take the act_<digits> segment out of it.
    match = re.search(r"act_(\d+)", text)
    if match:
        text = match.group(1)

    digits = _SEPARATORS.sub("", text)
    if digits.startswith(_ACT_PREFIX):
        digits = digits[len(_ACT_PREFIX):]

    if not digits.isdigit():
        raise AccountIdError(
            f"Ad account ID must be digits, optionally prefixed with 'act_', "
            f"got {text!r}."
        )
    # Meta has never published a fixed length and account ids have grown over
    # the years, so this checks for plausibility rather than an exact count --
    # a hard length rule here would reject accounts opened after it was written.
    if len(digits) < 6:
        raise AccountIdError(
            f"Ad account ID {digits!r} is too short to be real "
            f"(got {len(digits)} digits)."
        )
    return digits


def account_path(value: object) -> str:
    """Return the ``act_``-prefixed form the Graph API path needs.

    The ONLY place the prefix is added. Everything else in this package stores
    and compares the bare digits, so there is never a question of whether a
    given string already carries it.

    >>> account_path("1234567890")
    'act_1234567890'
    >>> account_path("act_1234567890")
    'act_1234567890'
    """
    return _ACT_PREFIX + normalize_account_id(value)
