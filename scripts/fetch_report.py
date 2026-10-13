#!/usr/bin/env python
"""Run a report and print it, or write it to CSV.

    ./.venv/bin/python scripts/fetch_report.py campaigns --days 30
    ./.venv/bin/python scripts/fetch_report.py creative_fatigue --days 14 --csv
    ./.venv/bin/python scripts/fetch_report.py adsets --since 2026-09-01 --until 2026-09-30
    ./.venv/bin/python scripts/fetch_report.py accounts

Read-only by construction: it goes through
:class:`~metaads_reporting.client.ReadOnlyMetaAdsClient`, which sends only GET.

``accounts`` is not an insights report -- it lists the ad accounts this token
can reach -- so it is handled separately rather than bent into the Report shape.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from metaads_reporting import logdb  # noqa: E402
from metaads_reporting.client import MetaAdsReportingError, ReadOnlyMetaAdsClient  # noqa: E402
from metaads_reporting.config import ConfigError  # noqa: E402
from metaads_reporting.export import expand_actions, output_path, to_dataframe, write_csv  # noqa: E402
from metaads_reporting.insights import last_n_days, name_contains, summarise  # noqa: E402
from metaads_reporting.reports import get_report, report_names  # noqa: E402

#: Action types lifted into their own columns by default. These are the ones
#: people actually ask about; everything else stays in the `actions` JSON.
DEFAULT_ACTIONS = ("purchase", "lead", "complete_registration", "link_click")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"reports: accounts, {', '.join(report_names())}",
    )
    parser.add_argument("report", help=f"accounts, or one of: {', '.join(report_names())}")
    parser.add_argument("--account-id", help="default: META_ADS_ACCOUNT_ID")
    parser.add_argument(
        "--days", type=int, default=30, help="window of N days ending YESTERDAY"
    )
    parser.add_argument("--since", help="YYYY-MM-DD (with --until)")
    parser.add_argument("--until", help="YYYY-MM-DD (with --since)")
    parser.add_argument("--campaign", help="narrow to campaigns whose name contains this")
    parser.add_argument("--node-id", help="report on one campaign/ad set/ad id")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--csv", action="store_true", help="write to the output dir")
    parser.add_argument(
        "--query", action="store_true", help="print the request parameters and stop"
    )
    args = parser.parse_args(argv)

    with logdb.record_tool_call("fetch_report", vars(args)):
        return _run(args)


def _run(args) -> int:
    try:
        client = ReadOnlyMetaAdsClient.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.report == "accounts":
        rows = client.list_ad_accounts()
        frame = pd.DataFrame(rows)
        print(frame.to_string(index=False) if len(frame) else "(no accounts)")
        return 0

    try:
        definition = get_report(args.report)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if bool(args.since) != bool(args.until):
        print("error: --since and --until must be given together.", file=sys.stderr)
        return 1

    filters = (name_contains("campaign", args.campaign),) if args.campaign else ()
    if args.since:
        query = definition.build(
            since=args.since, until=args.until, filtering=filters, limit=args.limit
        )
    else:
        window = last_n_days(args.days)
        query = definition.build(
            since=window["since"],
            until=window["until"],
            filtering=filters,
            limit=args.limit,
        )

    if args.query:
        for key, value in sorted(query.to_params().items()):
            print(f"{key} = {value}")
        return 0

    try:
        account = client.resolve_account_id(args.account_id)
        rows = client.insight_rows(query, node_id=args.node_id, account_id=account)
    except (MetaAdsReportingError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    currency = client.currency(account)
    expanded = expand_actions(rows, DEFAULT_ACTIONS)
    columns = list(definition.columns) + [f"action.{a}" for a in DEFAULT_ACTIONS]
    frame = to_dataframe(expanded, columns)

    print(f"{definition.name}: {len(frame)} row(s), {query.describe_window()}")
    print(f"attribution: {', '.join(query.attribution_windows)}  |  currency: {currency}")
    print()
    if args.csv:
        path = write_csv(
            frame, output_path(client.settings.output_dir, definition.name, account)
        )
        print(f"wrote {path}")
    elif len(frame):
        # `actions` is a JSON blob per row and makes the table unreadable; the
        # expanded action.* columns carry what it was for.
        shown = frame.drop(columns=[c for c in ("actions", "action_values") if c in frame])
        print(shown.to_string(index=False))
    else:
        print("(no rows -- no delivery in this window, or the filter matched nothing)")

    print()
    totals = summarise(rows)
    print("totals (reach and frequency omitted: they cannot be summed across rows)")
    for key, value in totals.items():
        print(f"  {key:12s} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
