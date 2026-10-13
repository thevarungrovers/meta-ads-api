#!/usr/bin/env python
"""What the access token in .env actually is, and when it dies.

    ./.venv/bin/python scripts/token_info.py

The one question worth answering before anything else goes wrong: **is this a
System User token or a person's?**

A user token is invalidated the moment that person changes their Facebook
password -- error code 190, subcode 460 -- and the symptom is every call in
every automation failing at once, with a message about a password that nobody
will connect to this repo. A System User has no password, so its token lives
until it is revoked or until an explicit expiry you set when you made it.

This calls ``/debug_token``, which reports the token's type, its app, its
scopes and its expiry. It prints no part of the token itself.

``--check`` exits non-zero when the token expires within ``--days`` (default
14), so it can be a cron line rather than something somebody remembers to run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaads_reporting.client import ReadOnlyMetaAdsClient  # noqa: E402
from metaads_reporting.config import ConfigError, Settings  # noqa: E402
from metaads_reporting.graph import GraphError  # noqa: E402

#: Scopes this toolchain uses, and what stops working without each.
NEEDED_SCOPES = {
    "ads_read": "reports (run_report, scripts/fetch_report.py)",
    "ads_management": "every preview_*/apply_* pair, and scripts/manage.py",
    "business_management": "listing which ad accounts the token can reach",
}


def describe_expiry(timestamp: int | None) -> tuple[str, int | None]:
    """A human line and the days remaining, or None for a token with no expiry."""
    if not timestamp:
        return ("never (no expiry set)", None)
    when = dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc)
    days = (when - dt.datetime.now(dt.timezone.utc)).days
    return (f"{when.isoformat()} ({days} days)", days)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the token expires within --days (for cron)",
    )
    parser.add_argument(
        "--days", type=int, default=14, help="threshold for --check (default 14)"
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    client = ReadOnlyMetaAdsClient.from_env(settings=settings)

    # debug_token wants the token under inspection as `input_token`; the
    # transport supplies the CALLING credential separately. Here they are the
    # same token inspecting itself, which Meta allows.
    try:
        payload = client._get("debug_token", input_token=settings.access_token)
    except GraphError as exc:
        print(f"error: could not inspect the token.\n{exc}", file=sys.stderr)
        return 1

    data = payload.get("data") or {}
    token_type = data.get("type") or "(unknown)"
    is_system_user = token_type.upper() == "SYSTEM_USER"
    scopes = sorted(data.get("scopes") or [])
    expires, days_left = describe_expiry(data.get("expires_at"))
    issued, _ = describe_expiry(data.get("issued_at"))

    print("Meta access token\n")
    print(f"  type           {token_type}")
    print(f"  valid          {data.get('is_valid')}")
    print(f"  app id         {data.get('app_id')} ({data.get('application') or '?'})")
    print(f"  user/actor id  {data.get('user_id') or '(none)'}")
    print(f"  issued         {issued}")
    print(f"  expires        {expires}")
    print(f"  data access    {describe_expiry(data.get('data_access_expires_at'))[0]}")
    print(f"  scopes         {', '.join(scopes) or '(none reported)'}")

    print()
    if is_system_user:
        print("  OK   This is a System User token. It survives a password change.")
    else:
        print(
            "  WARN This is NOT a System User token.\n"
            "       It will be invalidated the moment its owner changes their\n"
            "       Facebook password -- code 190, subcode 460 -- and every\n"
            "       automation using it will fail at once, with an error that\n"
            "       says nothing about this repo.\n"
            "       Business Settings > Users > System Users > Generate New Token."
        )

    print()
    missing = [s for s in NEEDED_SCOPES if s not in scopes]
    if missing:
        print("  Scopes this toolchain wants and does not have:")
        for scope in missing:
            print(f"    - {scope:22s} needed for: {NEEDED_SCOPES[scope]}")
        print(
            "\n  A missing scope and an unassigned ad account give the SAME\n"
            "  permission error, so fix the scope first, then check Business\n"
            "  Settings > System Users > Assign Assets."
        )
    else:
        print("  OK   Every scope this toolchain uses is present.")

    if args.check:
        if days_left is not None and days_left <= args.days:
            print(
                f"\n  FAIL expires in {days_left} days, inside the {args.days}-day "
                f"threshold.",
                file=sys.stderr,
            )
            return 1
        print(f"\n  OK   not expiring within {args.days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
