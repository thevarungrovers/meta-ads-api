"""Minor units: Meta's version of the money trap, and it is worse than micros.

**Every monetary field in the Marketing API is an integer in the account
currency's MINOR unit.** ``daily_budget: 5000`` on a USD account is fifty
dollars, not five thousand. So are ``lifetime_budget``, ``bid_amount``,
``budget_remaining``, ``amount_spent``, ``balance``, ``min_daily_budget`` and
the spend cap fields.

Two things make this nastier than Google's micros:

1. **There is no suffix.** ``metrics.cost_micros`` at least says ``_micros``.
   ``daily_budget`` says nothing at all, and the value is a plain integer that
   looks entirely reasonable either way -- 5000 is a believable budget and a
   believable number of cents.
2. **The divisor is not constant.** It is 100 for most currencies and **1** for
   the zero-decimal ones. On a JPY account ``daily_budget: 5000`` is five
   thousand yen, and dividing by 100 renders it as "50.00" -- a hundredfold
   understatement, in the text a human is about to approve.

And insights go the other way: ``spend``, ``cpc``, ``cpm`` and ``cpp`` on the
``/insights`` edge come back as decimal STRINGS already in major units
("12.34"), and must not be divided at all. Dividing those is a second,
independent hundredfold error in the opposite direction.

So the exponent is looked up, never assumed, and an unknown currency is
**refused** rather than guessed at -- see :func:`minor_unit_exponent`. A wrong
exponent is silent: no exception, no warning, just a number off by 100 in an
approval prompt.

The table is ISO 4217's own exponent for each currency Meta supports as an ad
account currency. It is a published standard, which is the only kind of source
worth hard-coding; Meta follows it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: ISO 4217 currencies with NO minor unit. One "cent" is one whole unit, so the
#: API integer is already the amount and the divisor is 1.
ZERO_DECIMAL = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)

#: ISO 4217 currencies with THREE decimal places. The API integer is thousandths.
#: None of these is common as a Meta ad account currency, which is exactly why
#: they are listed: an unlisted currency is refused, and being refused on a
#: real account is worse than carrying six extra rows here.
THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

#: Everything else Meta supports, all with two decimal places. Listed rather
#: than defaulted: an unknown code must raise, not quietly divide by 100.
TWO_DECIMAL = frozenset(
    {
        "AED", "ARS", "AUD", "BDT", "BOB", "BRL", "CAD", "CHF", "CNY", "COP",
        "CRC", "CZK", "DKK", "EGP", "EUR", "GBP", "GTQ", "HKD", "HNL", "HUF",
        "IDR", "ILS", "INR", "KES", "LKR", "MOP", "MXN", "MYR", "NGN", "NIO",
        "NOK", "NZD", "PEN", "PHP", "PKR", "PLN", "QAR", "RON", "RUB", "SAR",
        "SEK", "SGD", "THB", "TRY", "TWD", "TZS", "UAH", "USD", "UYU", "VEF",
        "ZAR",
    }
)


class CurrencyError(ValueError):
    """The currency is unknown, so no amount in it can be rendered safely."""


def minor_unit_exponent(currency: str) -> int:
    """How many decimal places ``currency`` has. Raises on an unknown code.

    Refusing beats guessing. The caller is about to render a number into an
    approval prompt or send one to a live account, and a default of 2 would be
    wrong by a factor of 100 on a JPY account without anything saying so.

    >>> minor_unit_exponent("USD")
    2
    >>> minor_unit_exponent("jpy")
    0
    >>> minor_unit_exponent("KWD")
    3
    """
    code = (currency or "").strip().upper()
    if not code:
        raise CurrencyError(
            "No currency given. Every amount here is in the account currency's "
            "minor unit, so the amount cannot be read without it."
        )
    if code in ZERO_DECIMAL:
        return 0
    if code in THREE_DECIMAL:
        return 3
    if code in TWO_DECIMAL:
        return 2
    raise CurrencyError(
        f"Unknown currency {code!r}. Amounts in the Marketing API are integers "
        f"in the currency's minor unit, and the divisor depends on the "
        f"currency -- 100 for USD, 1 for JPY. Refusing to guess. Add {code} to "
        f"metaads_reporting.currency with its ISO 4217 exponent."
    )


def divisor(currency: str) -> Decimal:
    """The number to divide an API integer by to get major units.

    >>> divisor("USD")
    Decimal('100')
    >>> divisor("JPY")
    Decimal('1')
    """
    return Decimal(10) ** minor_unit_exponent(currency)


def from_minor_units(value: object, currency: str) -> Decimal | None:
    """An API integer (cents) as a major-unit :class:`~decimal.Decimal`.

    ``None`` passes through, so an absent budget stays absent rather than
    becoming zero -- "this ad set has no budget of its own" and "this ad set
    has a budget of nothing" are different answers, and on Meta the first one
    is extremely common (it means the campaign holds the budget).

    >>> from_minor_units(5000, "USD")
    Decimal('50.00')
    >>> from_minor_units(5000, "JPY")
    Decimal('5000')
    >>> from_minor_units(None, "USD") is None
    True
    """
    if value is None or value == "":
        return None
    exponent = minor_unit_exponent(currency)
    amount = Decimal(str(value)) / (Decimal(10) ** exponent)
    return amount.quantize(Decimal(1).scaleb(-exponent))


def to_minor_units(value: Decimal | str | int | float, currency: str) -> int:
    """A major-unit amount as the integer the API wants.

    The inverse of :func:`from_minor_units`. Rounded half-up at the currency's
    own precision, then checked: a value with more precision than the currency
    has is refused rather than silently rounded, because "1.005" on a USD
    account is somebody meaning something this cannot express.

    >>> to_minor_units("50.00", "USD")
    5000
    >>> to_minor_units("5000", "JPY")
    5000
    >>> to_minor_units(Decimal("12.5"), "USD")
    1250
    """
    exponent = minor_unit_exponent(currency)
    amount = Decimal(str(value))
    quantum = Decimal(1).scaleb(-exponent)
    rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded != amount:
        raise CurrencyError(
            f"{amount} has more precision than {currency.upper()} has "
            f"({exponent} decimal place(s)). Pass {rounded} if that is what "
            f"you meant."
        )
    return int(rounded.scaleb(exponent))


def format_money(value: Decimal | None, currency: str) -> str:
    """An amount rendered for a human, with its currency code.

    >>> format_money(Decimal("50"), "USD")
    '50.00 USD'
    >>> format_money(Decimal("5000"), "JPY")
    '5000 JPY'
    >>> format_money(None, "USD")
    '(not set)'
    """
    if value is None:
        return "(not set)"
    exponent = minor_unit_exponent(currency)
    quantum = Decimal(1).scaleb(-exponent)
    return f"{value.quantize(quantum)} {currency.upper()}"
