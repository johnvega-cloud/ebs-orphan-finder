"""Command line: ``ebs-orphans scan`` (read-only) and ``ebs-orphans apply`` (deletion plan)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from ebs_orphans import __version__
from ebs_orphans.accounts import SessionFactory, filter_accounts, list_org_accounts
from ebs_orphans.discovery import scan
from ebs_orphans.models import Account, ProtectRule
from ebs_orphans.plan import PlanError, apply_plan, build_plan
from ebs_orphans.pricing import PriceBook
from ebs_orphans.report import render_markdown, write_csv, write_json, write_markdown

log = logging.getLogger("ebs_orphans")

# Exit codes: documented in the README so cron/CI can react to them.
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3  # finished, but some accounts/regions/volumes need attention

DEFAULT_ROLE = "OrganizationAccountAccessRole"
DEFAULT_PROTECT = ["DoNotDelete"]
FORMATS = ("csv", "json", "md")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


def setup_logging(verbosity: int, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    log.setLevel(logging.DEBUG if verbosity > 1 else logging.INFO if verbosity else logging.WARNING)


def _csv_list(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _protect_rule(text: str) -> ProtectRule:
    try:
        return ProtectRule.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="AWS profile to start from (default: environment)")
    common.add_argument("--region", default="us-east-1", help="home region for STS/Organizations")
    common.add_argument(
        "--role-name",
        default=DEFAULT_ROLE,
        help=f"role to assume in each member account (default: {DEFAULT_ROLE})",
    )
    common.add_argument("--external-id", help="sts:ExternalId required by the member role")
    common.add_argument(
        "--protect-tag",
        action="append",
        type=_protect_rule,
        metavar="KEY[=VALUE]",
        help="tag that marks a volume as never-delete; repeatable (default: DoNotDelete)",
    )
    common.add_argument("-v", "--verbose", action="count", default=1, help="-vv for debug")
    common.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    common.add_argument("--log-format", choices=("text", "json"), default="text")

    parser = argparse.ArgumentParser(
        prog="ebs-orphans",
        description="Find unattached EBS volumes across an AWS Organization, report their "
        "cost, and plan a safe cleanup.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", parents=[common], help="read-only discovery and cost report")
    s.add_argument("--min-age-days", type=int, default=30, help="minimum age in days (default 30)")
    s.add_argument("--regions", type=_csv_list, default=[], help="comma list (default: enabled)")
    scope = s.add_mutually_exclusive_group()
    scope.add_argument("--accounts", type=_csv_list, default=[], help="only these account IDs")
    scope.add_argument(
        "--current-account-only", action="store_true", help="skip Organizations; scan this account"
    )
    s.add_argument("--exclude-accounts", type=_csv_list, default=[], help="skip these account IDs")
    s.add_argument("--ou", type=_csv_list, default=[], help="only accounts under these OU IDs")
    s.add_argument("--price-file", type=Path, help="JSON price overrides (see examples/)")
    s.add_argument("--output-dir", type=Path, default=Path("reports"))
    s.add_argument(
        "--formats", type=_csv_list, default=list(FORMATS), help="csv,json,md (default all)"
    )
    s.add_argument("--plan", action="store_true", help="also write a deletion plan file")
    s.add_argument(
        "--no-snapshot",
        action="store_true",
        help="plan deletes without a final snapshot (default: snapshot first)",
    )
    s.add_argument("--max-workers", type=int, default=16, help="parallel API calls (default 16)")

    a = sub.add_parser("apply", parents=[common], help="apply a reviewed deletion plan")
    a.add_argument("plan_file", type=Path)
    a.add_argument(
        "--execute",
        action="store_true",
        help="really snapshot and delete (default: DryRun=True for every call)",
    )
    a.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    a.add_argument(
        "--max-plan-age-hours",
        type=float,
        default=72,
        help="refuse older plans (default 72; 0 disables the check)",
    )
    a.add_argument("--snapshot-timeout-minutes", type=int, default=120)
    a.add_argument("--results-file", type=Path, help="default: <plan>.results.json")
    return parser


def _session(args: argparse.Namespace) -> boto3.Session:
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def cmd_scan(args: argparse.Namespace) -> int:
    if args.min_age_days < 0:
        log.error("--min-age-days must be >= 0")
        return EXIT_USAGE
    unknown = set(args.formats) - set(FORMATS)
    if unknown:
        log.error("unknown format(s): %s", ", ".join(sorted(unknown)))
        return EXIT_USAGE

    protect = args.protect_tag or [ProtectRule.parse(p) for p in DEFAULT_PROTECT]
    prices = PriceBook.load(args.price_file)
    base = _session(args)
    factory = SessionFactory(base, args.role_name, args.external_id)

    accounts: list[Account]
    if args.current_account_only:
        accounts = [Account(id=factory.home_account, name="current")]
    elif args.accounts and not args.ou:
        # Explicit IDs do not need Organizations permissions.
        accounts = [Account(id=a) for a in args.accounts]
    else:
        accounts = list_org_accounts(base, args.ou)
    accounts = filter_accounts(accounts, args.accounts if args.ou else (), args.exclude_accounts)
    if not accounts:
        log.error("no accounts to scan")
        return EXIT_USAGE

    log.info(
        "scanning %d account(s), %s, volumes unattached and >= %d days old",
        len(accounts),
        ",".join(args.regions) or "all enabled regions",
        args.min_age_days,
    )
    result = scan(
        accounts,
        factory.for_account,
        args.min_age_days,
        prices,
        regions=args.regions,
        protect=protect,
        max_workers=args.max_workers,
    )

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = result.generated_at.strftime("%Y%m%dT%H%M%SZ")
    writers = {"csv": write_csv, "json": write_json, "md": write_markdown}
    for fmt in args.formats:
        path = out / f"ebs-orphans-{stamp}.{fmt}"
        writers[fmt](result, path)
        log.info("wrote %s", path)
    if args.plan:
        plan = build_plan(result, protect, snapshot_before_delete=not args.no_snapshot)
        path = out / f"ebs-orphans-{stamp}.plan.json"
        path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        log.info(
            "wrote deletion plan %s (%d volumes, $%.2f/month); review it, then: "
            "ebs-orphans apply %s",
            path,
            plan["summary"]["volumes"],
            plan["summary"]["monthly_savings_usd"],
            path,
        )

    summary = render_markdown(result, top=10).split("## By account")[0].rstrip()
    print(summary)
    return EXIT_PARTIAL if result.errors else EXIT_OK


def _confirm(plan: dict[str, Any]) -> bool:
    if not sys.stdin.isatty():
        log.error("--execute needs --yes when not run from a terminal")
        return False
    s = plan["summary"]
    print(
        f"About to DELETE {s['volumes']} volume(s), {s['total_gib']} GiB, "
        f"snapshot first: {plan['snapshot_before_delete']}."
    )
    answer = input(f"Type the plan id ({plan['plan_id']}) to continue: ").strip()
    return answer == str(plan["plan_id"])


def cmd_apply(args: argparse.Namespace) -> int:
    try:
        plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read plan: %s", exc)
        return EXIT_USAGE
    if args.execute and not args.yes and not _confirm(plan):
        log.error("not confirmed; nothing changed")
        return EXIT_USAGE

    factory = SessionFactory(_session(args), args.role_name, args.external_id)
    try:
        outcomes = apply_plan(
            plan,
            factory.for_account,
            execute=args.execute,
            extra_protect=args.protect_tag or [],
            max_age_hours=args.max_plan_age_hours,
            snapshot_timeout_minutes=args.snapshot_timeout_minutes,
        )
    except PlanError as exc:
        log.error("%s", exc)
        return EXIT_USAGE

    results_file = args.results_file or args.plan_file.with_suffix(".results.json")
    results_file.write_text(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "executed": args.execute,
                "finished_at": datetime.now(UTC).isoformat(),
                "outcomes": [o.__dict__ for o in outcomes],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.action] = counts.get(o.action, 0) + 1
    print(
        ("EXECUTED" if args.execute else "DRY RUN")
        + ": "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f" (details: {results_file})"
    )
    return EXIT_OK if all(o.ok for o in outcomes) else EXIT_PARTIAL


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(0 if args.quiet else args.verbose, args.log_format)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        return cmd_apply(args)
    except NoCredentialsError:
        log.error("no AWS credentials found (use --profile or the standard environment variables)")
        return EXIT_FATAL
    except (ClientError, BotoCoreError) as exc:
        # Failures before the per-account loop (Organizations, the caller's own STS).
        log.error("%s", exc)
        return EXIT_FATAL
