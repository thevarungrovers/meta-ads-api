#!/usr/bin/env python
"""Change the account from a terminal, with a human driving.

    ./.venv/bin/python scripts/manage.py find campaign "retargeting"
    ./.venv/bin/python scripts/manage.py campaign pause --name "..." --dry-run
    ./.venv/bin/python scripts/manage.py adset set-budget --id 123 --amount 50.00
    ./.venv/bin/python scripts/manage.py ad pause --id 456

NOTHING IS SENT WITHOUT A CONFIRMATION. Without ``--yes`` every mutating
command prints the plan, sends it to Meta with ``execution_options=
["validate_only"]`` so the server-side check runs and nothing changes, and then
asks for a typed ``yes``.

``--dry-run`` stops after that validation. It is not a local guess: Meta really
does check the request and really does change nothing.

**This is the human half of the bargain the MCP server makes.** The server
deliberately exposes no create and no delete, because those carry declarations
and irreversibility that belong to a person. This is where a person does them --
in their own scrollback, under their own hands.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metaads_reporting import logdb  # noqa: E402
from metaads_reporting.config import ConfigError  # noqa: E402
from metaads_reporting.currency import format_money, from_minor_units  # noqa: E402
from metaads_reporting.write import (  # noqa: E402
    MutatingMetaAdsClient,
    MutationError,
    execute,
    execute_chain,
)
from metaads_reporting.write import ads, adsets, audiences, campaigns, creatives, media  # noqa: E402

ENTITIES = {"campaign": campaigns, "adset": adsets, "ad": ads}


def confirm(plan, client, *, assume_yes: bool, dry_run: bool) -> bool:
    """Validate against Meta, show the plan, and ask.

    The validation happens BEFORE the question on purpose: there is no point
    asking a human to approve something Meta is going to reject, and the
    rejection usually says something they need to read.
    """
    print()
    print(plan.render())
    print()

    try:
        if plan.requires is not None:
            # A dependent create: only the first step can be validated, because
            # the second references an id that does not exist yet. Say so
            # rather than reporting a clean validation of half the work.
            result, _ = execute_chain(client, plan, apply=False)
            print(
                f"Meta validated the {plan.requires.entity}: {result}. The "
                f"{plan.entity} itself cannot be validated until that exists, "
                f"so it is checked at apply time."
            )
        else:
            result = execute(client, plan, apply=False)
            print(f"Meta validated it: {result}. Nothing has been changed.")
    except MutationError as exc:
        print(f"Meta refused this before it was applied:\n{exc}", file=sys.stderr)
        return False

    if dry_run:
        print("--dry-run: stopping here.")
        return False
    if assume_yes:
        return True

    print()
    answer = input('Apply this to the LIVE account? Type "yes" to confirm: ').strip()
    if answer != "yes":
        print("Not applied.")
        return False
    return True


def apply_plan(plan, client, *, assume_yes: bool, dry_run: bool) -> int:
    if not confirm(plan, client, assume_yes=assume_yes, dry_run=dry_run):
        return 0
    if plan.requires is not None:
        result, created = execute_chain(client, plan, apply=True)
        for node in created:
            print(f"  created {node}")
    else:
        result = execute(client, plan, apply=True)
    if not result.success:
        print(
            f"Meta accepted the request but did not confirm the change: "
            f"{result.response!r}. Check Ads Manager before retrying.",
            file=sys.stderr,
        )
        return 1
    print(f"APPLIED: {plan.entity} {result.node_id or plan.node_id}")
    return 0


def parse_amount(raw: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SystemExit(
            f'error: amount must be a decimal in the account currency, e.g. "50.00" '
            f"-- got {raw!r}. Never pass minor units."
        ) from exc
    if value < 0:
        raise SystemExit(f"error: amount cannot be negative, got {value}.")
    return value


def cmd_find(client, args) -> int:
    module = ENTITIES[args.entity]
    rows = module.search(client, args.needle)
    if not rows:
        print(f"No {args.entity} matching {args.needle!r}.")
        return 1
    currency = client.currency()
    for row in rows:
        candidate = module.as_candidate(row)
        line = candidate.render()
        budget = from_minor_units(row.get("daily_budget"), currency)
        if budget is not None:
            line += f"  budget {format_money(budget, currency)}/day"
        print(line)
    print(f"\n{len(rows)} {args.entity}(s).")
    return 0


def cmd_status(client, args, module, id_kwarg) -> int:
    plan = module.plan_set_status(
        client,
        status=args.status,
        name=args.name,
        **{id_kwarg: args.id},
    )
    return apply_plan(plan, client, assume_yes=args.yes, dry_run=args.dry_run)


def cmd_budget(client, args, module, id_kwarg) -> int:
    plan = module.plan_set_daily_budget(
        client,
        amount=parse_amount(args.amount),
        name=args.name,
        override_guardrail=args.override_budget_guardrail,
        **{id_kwarg: args.id},
    )
    return apply_plan(plan, client, assume_yes=args.yes, dry_run=args.dry_run)


def cmd_bid(client, args) -> int:
    plan = adsets.plan_set_bid_amount(
        client, amount=parse_amount(args.amount), ad_set_id=args.id, name=args.name
    )
    return apply_plan(plan, client, assume_yes=args.yes, dry_run=args.dry_run)


def add_target(parser) -> None:
    """Every mutating subcommand is addressed by id or by name, never both."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", help="the object id")
    group.add_argument("--name", help="exact name first, then substring")
    parser.add_argument("--dry-run", action="store_true", help="validate and stop")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="search for an object by name")
    find.add_argument("entity", choices=sorted(ENTITIES))
    find.add_argument("needle", nargs="?", default=None)

    campaign = sub.add_parser("campaign", help="change a campaign").add_subparsers(
        dest="action", required=True
    )
    for action in ("pause", "activate"):
        p = campaign.add_parser(action)
        add_target(p)
    budget = campaign.add_parser("set-budget", help="campaign-budget campaigns only")
    budget.add_argument("--amount", required=True, help='e.g. "250.00"')
    budget.add_argument("--override-budget-guardrail", action="store_true")
    add_target(budget)

    adset = sub.add_parser("adset", help="change an ad set").add_subparsers(
        dest="action", required=True
    )
    for action in ("pause", "activate"):
        p = adset.add_parser(action)
        add_target(p)
    adset_budget = adset.add_parser("set-budget")
    adset_budget.add_argument("--amount", required=True, help='e.g. "50.00"')
    adset_budget.add_argument("--override-budget-guardrail", action="store_true")
    add_target(adset_budget)
    adset_bid = adset.add_parser("set-bid", help="bid cap / cost target")
    adset_bid.add_argument("--amount", required=True, help='e.g. "3.50"')
    add_target(adset_bid)

    ad = sub.add_parser("ad", help="change one ad").add_subparsers(
        dest="action", required=True
    )
    for action in ("pause", "activate"):
        p = ad.add_parser(action)
        add_target(p)

    def add_copy_args(p):
        p.add_argument("--primary-text", help="new body text")
        p.add_argument("--headline", help="new headline")
        p.add_argument("--description", help="new description")
        p.add_argument("--link", help="new destination URL")
        p.add_argument("--image", help="local image file to upload and use")
        p.add_argument("--video-id", help="an EXISTING Meta video id to use")

    dup = ad.add_parser(
        "duplicate", help="new PAUSED ad beside this one, with changed copy"
    )
    dup.add_argument("--new-name", help="name for the new ad")
    add_copy_args(dup)
    add_target(dup)

    setcopy = ad.add_parser(
        "set-copy", help="repoint THIS ad at a new creative (discards social proof)"
    )
    add_copy_args(setcopy)
    add_target(setcopy)

    aud = sub.add_parser("audience", help="audiences").add_subparsers(
        dest="action", required=True
    )
    aud_list = aud.add_parser("list", help="audiences that already exist")
    aud_list.add_argument(
        "--kind", choices=("custom", "saved", "all"), default="all"
    )
    lal = aud.add_parser("lookalike", help="create a lookalike from an existing source")
    lal.add_argument("--name", required=True)
    lal.add_argument("--from-id", required=True, help="origin audience id")
    lal.add_argument("--country", required=True, help="two-letter code, e.g. CA")
    lal.add_argument("--ratio", type=float, default=0.01, help="0.01 to 0.20")
    lal.add_argument("--dry-run", action="store_true")
    lal.add_argument("--yes", action="store_true")
    saved = aud.add_parser("saved", help="create a saved targeting spec")
    saved.add_argument("--name", required=True)
    saved.add_argument(
        "--targeting", required=True, help="Meta targeting spec as JSON"
    )
    saved.add_argument("--dry-run", action="store_true")
    saved.add_argument("--yes", action="store_true")

    return parser


STATUS_FOR = {"pause": "PAUSED", "activate": "ACTIVE"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    with logdb.record_tool_call(f"manage.{args.command}", vars(args)):
        try:
            client = MutatingMetaAdsClient.from_env()
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        try:
            return _dispatch(client, args)
        except MutationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


def copy_edits(args) -> dict:
    """The --primary-text/--headline/... flags, as the editor's vocabulary."""
    edits = {
        "primary_text": getattr(args, "primary_text", None),
        "headline": getattr(args, "headline", None),
        "description": getattr(args, "description", None),
        "link": getattr(args, "link", None),
    }
    return {k: v for k, v in edits.items() if v is not None}


def cmd_creative(client, args, *, duplicate: bool) -> int:
    """ad duplicate / ad set-copy.

    The image upload happens AFTER the confirmation, not before: uploading on a
    --dry-run would put a file in the account's media library for a change the
    operator then declined.
    """
    builder = ads.plan_duplicate_ad if duplicate else ads.plan_update_copy
    kwargs = dict(
        ad_id=args.id,
        edits=copy_edits(args),
        image_path=args.image,
        video_id=args.video_id,
    )
    if duplicate:
        kwargs["new_name"] = args.new_name
    if args.name and not args.id:
        found = ads.find(client, name=args.name)
        kwargs["ad_id"] = found["id"]

    plan = builder(client, **kwargs)
    if not confirm(plan, client, assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    if args.image:
        image = media.inspect_image(args.image)
        print(f"  uploading {image.path.name} ({image.describe()['size_kb']}KB) ...")
        image_hash = media.upload_image(client, image, apply=True)
        print(f"  image_hash {image_hash}")
        plan.requires.payload = ads._fill_uploaded_image(plan.requires.payload, image_hash)

    result, created = execute_chain(client, plan, apply=True)
    for node in created:
        print(f"  created {node}")
    if not result.success:
        print(f"Meta did not confirm: {result.response!r}", file=sys.stderr)
        return 1
    print(f"APPLIED: {plan.entity} {result.node_id or plan.node_id}")
    return 0


def cmd_audience(client, args) -> int:
    import json as _json

    if args.action == "list":
        if args.kind in ("custom", "all"):
            for row in audiences.list_custom(client):
                print(
                    f"  {row['audience_id']:<20} {(row['name'] or '')[:40]:40s} "
                    f"[{row['subtype']}, ~{row['approx_size']}]"
                )
        if args.kind in ("saved", "all"):
            for row in audiences.list_saved(client):
                print(f"  {row['saved_audience_id']:<20} {(row['name'] or '')[:40]:40s} [saved]")
        return 0

    if args.action == "lookalike":
        plan = audiences.plan_create_lookalike(
            client,
            name=args.name,
            origin_audience_id=args.from_id,
            country=args.country,
            ratio=args.ratio,
        )
    else:
        try:
            targeting = _json.loads(args.targeting)
        except ValueError as exc:
            print(f"error: --targeting is not valid JSON: {exc}", file=sys.stderr)
            return 1
        plan = audiences.plan_create_saved(client, name=args.name, targeting=targeting)

    return apply_plan(plan, client, assume_yes=args.yes, dry_run=args.dry_run)


def _dispatch(client, args) -> int:
    if args.command == "find":
        return cmd_find(client, args)
    if args.command == "audience":
        return cmd_audience(client, args)

    module, id_kwarg = {
        "campaign": (campaigns, "campaign_id"),
        "adset": (adsets, "ad_set_id"),
        "ad": (ads, "ad_id"),
    }[args.command]

    if args.action in STATUS_FOR:
        args.status = STATUS_FOR[args.action]
        return cmd_status(client, args, module, id_kwarg)
    if args.action == "set-budget":
        return cmd_budget(client, args, module, id_kwarg)
    if args.action == "set-bid":
        return cmd_bid(client, args)
    if args.action == "duplicate":
        return cmd_creative(client, args, duplicate=True)
    if args.action == "set-copy":
        return cmd_creative(client, args, duplicate=False)
    raise SystemExit(f"error: unknown action {args.action!r}")


if __name__ == "__main__":
    raise SystemExit(main())
