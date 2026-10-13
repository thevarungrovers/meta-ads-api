#!/usr/bin/env python
"""Diagnostic ladder for the Meta Marketing API connection.

    ./.venv/bin/python scripts/test_connection.py

Each rung tests one thing and reports PASS, WARN or FAIL. The order matters:
every rung assumes the ones above it passed, so the FIRST failure names the
real problem. A dead token, a missing scope and an unassigned ad account look
identical from a single failing report, which is the thing this script exists
to separate:

  * ``/me`` needs no account and no ads permission at all, so rung 4 proves the
    token is alive without implicating anything else. **A Graph token is a
    bearer string with nothing to exchange up front, so this is the first
    moment it is tested -- unlike an OAuth flow, constructing the client proves
    nothing.**
  * ``/me/adaccounts`` needs ``business_management`` (or ``ads_read``) and
    proves the token can SEE accounts.
  * reading the target account proves it is assigned to this System User.
  * an insights call proves ``ads_read`` on that account specifically.

Nothing here prints the token. It is reported as presence and length only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaads_reporting.account_id import account_path  # noqa: E402
from metaads_reporting.client import (  # noqa: E402
    ACCOUNT_STATUS,
    MetaAdsReportingError,
    ReadOnlyMetaAdsClient,
)
from metaads_reporting.config import ENV_PATH, ConfigError, Settings  # noqa: E402
from metaads_reporting.currency import CurrencyError, minor_unit_exponent  # noqa: E402
from metaads_reporting.graph import GraphAPIError, ReadOnlyViolation  # noqa: E402
from metaads_reporting.insights import InsightsQuery, last_n_days  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def hint(text: str) -> None:
    """Print an explanation under the ladder.

    Deliberately stdout, not stderr: stdout is block-buffered when piped, so a
    stderr hint jumps ahead of the whole ladder and ends up above the rung it
    explains. One stream keeps the order honest. The exit code still carries
    the pass/fail signal.
    """
    print(f"\n  {text}")


class Ladder:
    """Collects rung results and prints them as it goes."""

    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def record(self, status: str, rung: str, detail: str = "") -> str:
        self.results.append((status, rung, detail))
        line = f"  [{status}] {rung}"
        print(line if not detail else f"{line}\n         {detail}")
        return status

    @property
    def failed(self) -> bool:
        return any(status == FAIL for status, _, _ in self.results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--account-id", help="account to test against (default: META_ADS_ACCOUNT_ID)"
    )
    args = parser.parse_args(argv)

    ladder = Ladder()
    print("Meta Marketing API connection check\n")

    # -- 1. the .env file -------------------------------------------------
    if ENV_PATH.exists():
        mode = oct(ENV_PATH.stat().st_mode & 0o777)
        detail = f"{ENV_PATH} (mode {mode})"
        if ENV_PATH.stat().st_mode & 0o077:
            ladder.record(
                WARN,
                "1. .env present",
                f"{detail} is group/world readable; chmod 600 it. A Meta token "
                "is a bearer credential with no second factor.",
            )
        else:
            ladder.record(PASS, "1. .env present", detail)
    else:
        ladder.record(
            FAIL, "1. .env present", f"{ENV_PATH} not found; cp .env.example .env"
        )
        return _finish(ladder)

    # -- 2. configuration -------------------------------------------------
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        ladder.record(FAIL, "2. configuration complete", str(exc))
        return _finish(ladder)

    ladder.record(PASS, "2. configuration complete")
    for key, value in settings.describe().items():
        print(f"         {key:20s} {value}")

    # -- 3. the read-only guard -------------------------------------------
    # Checked before any network call: if the guard is broken, stop before
    # touching a live account.
    client = ReadOnlyMetaAdsClient.from_env(settings=settings)
    from metaads_reporting.graph import NON_READ_METHODS, assert_read_method

    # Taken from the module rather than written out here, so this rung tests
    # the set the transport actually uses. A hardcoded copy would keep passing
    # after somebody added a fifth method to it and forgot this.
    refused = sorted(NON_READ_METHODS)
    for method in refused:
        try:
            assert_read_method(method)
        except ReadOnlyViolation:
            continue
        ladder.record(FAIL, "3. read-only guard", f"{method} was NOT refused")
        return _finish(ladder)
    ladder.record(PASS, "3. read-only guard", f"{', '.join(refused)} refused")

    # -- 4. the token is alive --------------------------------------------
    # /me needs no ads permission and no account, so a failure here is the
    # token itself and nothing else. This is the FIRST test of the credential:
    # a bearer token is not validated by constructing a client.
    try:
        me = client._get("me", fields="id,name")
    except MetaAdsReportingError as exc:
        ladder.record(FAIL, "4. access token is alive", _first_lines(exc))
        hint(
            "Nothing about the ad account is implicated yet -- /me carries no "
            "account at all. If the error says code 190 subcode 460, the token "
            "belonged to a PERSON who changed their password. Generate a "
            "SYSTEM USER token instead; it has no password to change.\n"
            "  See: ./.venv/bin/python scripts/token_info.py"
        )
        return _finish(ladder)
    except Exception as exc:  # noqa: BLE001
        ladder.record(FAIL, "4. access token is alive", f"{type(exc).__name__}: {exc}")
        return _finish(ladder)

    ladder.record(
        PASS,
        "4. access token is alive",
        f"identity {me.get('id')} ({me.get('name') or 'no name'}), "
        f"API {settings.api_version}",
    )

    # -- 5. which accounts it can see -------------------------------------
    try:
        accounts = client.list_ad_accounts()
    except MetaAdsReportingError as exc:
        ladder.record(WARN, "5. ad accounts visible", _first_lines(exc))
        hint(
            "The token works but cannot list accounts. That usually means the "
            "business_management scope is missing. It is not fatal -- the "
            "target account is read directly at rung 6 -- but you will not be "
            "able to discover account ids from here."
        )
        accounts = []
    else:
        ladder.record(
            PASS,
            "5. ad accounts visible",
            f"{len(accounts)} assigned to this token",
        )
        for row in accounts[:15]:
            print(
                f"         act_{row['account_id']}  {row['name'] or '(no name)'}  "
                f"[{row['currency']}, {row['status']}]"
            )
        if len(accounts) > 15:
            print(f"         ... and {len(accounts) - 15} more")

    # -- 6. the target account --------------------------------------------
    try:
        target = settings.resolve_account_id(args.account_id)
    except ConfigError as exc:
        ladder.record(WARN, "6. target account reachable", f"skipped: {exc}")
        return _finish(ladder)

    try:
        summary = client.account_summary(target)
    except MetaAdsReportingError as exc:
        ladder.record(FAIL, f"6. target act_{target} reachable", _first_lines(exc))
        hint(
            "The token is alive, so this is the account specifically. On Meta a "
            "missing object and a missing permission are the SAME error, so "
            "check both:\n"
            "    - is the id right? (it is the `act=` value in the Ads Manager URL)\n"
            "    - is this System User assigned to that ad account in Business\n"
            "      Settings > Users > System Users > Assign Assets?"
        )
        return _finish(ladder)
    except CurrencyError as exc:
        ladder.record(FAIL, f"6. target act_{target} reachable", str(exc))
        return _finish(ladder)

    ladder.record(
        PASS,
        f"6. target act_{target} reachable",
        f"{summary['name'] or '(no name)'} [{summary['currency']}, "
        f"{summary['timezone']}, {summary['status']}]",
    )
    if summary["status"] != "ACTIVE":
        hint(
            f"The account status is {summary['status']}, not ACTIVE. Reports "
            f"will still work; writes may not, and nothing will deliver."
            + (
                f" disable_reason: {summary['disable_reason']}"
                if summary.get("disable_reason")
                else ""
            )
        )

    # -- 7. the currency is one we can render -----------------------------
    # Before any money is read. An unknown currency means every budget in this
    # toolchain would be wrong by a factor of 100, silently.
    try:
        exponent = minor_unit_exponent(summary["currency"])
    except CurrencyError as exc:
        ladder.record(FAIL, "7. currency is supported", str(exc))
        return _finish(ladder)
    ladder.record(
        PASS,
        "7. currency is supported",
        f"{summary['currency']} has {exponent} decimal place(s), so a budget of "
        f"{10 ** exponent} in the API means 1 {summary['currency']}",
    )

    # -- 8. a real insights call ------------------------------------------
    # Proves ads_read ON THIS ACCOUNT, which nothing above does.
    window = last_n_days(7)
    query = InsightsQuery(
        fields=("spend", "impressions", "clicks"),
        level="campaign",
        since=window["since"],
        until=window["until"],
        time_increment=None,
        limit=25,
    )
    try:
        rows = client.insight_rows(query, account_id=target)
    except MetaAdsReportingError as exc:
        ladder.record(FAIL, "8. insights report runs", _first_lines(exc))
        hint(
            "Reading the account worked but reading its PERFORMANCE did not, "
            "which narrows it to the ads_read permission on this account."
        )
        return _finish(ladder)

    if not rows:
        # Not a failure: an account with no delivery in the window returns zero
        # rows, and that is a valid answer.
        ladder.record(
            WARN,
            "8. insights report runs",
            f"0 rows for {window['since']}..{window['until']} -- no campaigns, "
            "or no delivery",
        )
    else:
        total = sum(float(r.get("spend") or 0) for r in rows)
        ladder.record(
            PASS,
            "8. insights report runs",
            f"{len(rows)} campaign row(s) for {window['since']}..{window['until']}, "
            f"spend {total:,.2f} {summary['currency']}",
        )

    # -- 9. rate limit headroom -------------------------------------------
    usage = client.rate_limit()
    worst = usage.get("worst_percent_used", 0)
    if worst >= 80:
        ladder.record(
            WARN,
            "9. rate limit headroom",
            f"{worst}% of a Meta limit used. Calls start failing at 100%.",
        )
    else:
        ladder.record(PASS, "9. rate limit headroom", f"{worst}% used")

    return _finish(ladder)


def _first_lines(exc: Exception, count: int = 8) -> str:
    lines = str(exc).splitlines()
    shown = lines[:count]
    if len(lines) > count:
        shown.append(f"... ({len(lines) - count} more lines)")
    return "\n         ".join(shown)


def _finish(ladder: Ladder) -> int:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for status, _, _ in ladder.results:
        counts[status] += 1
    print(f"\n{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed.")
    if ladder.failed:
        print("Fix the FIRST failure above; later rungs depend on it.")
        return 1
    print("Connection is good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
