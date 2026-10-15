"""Write a scan result as CSV (for spreadsheets), JSON (for tools) and Markdown (for people)."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ebs_orphans import __version__
from ebs_orphans.models import OrphanVolume, ScanResult

CSV_FIELDS = [
    "account_id",
    "account_name",
    "region",
    "availability_zone",
    "volume_id",
    "name",
    "volume_type",
    "size_gib",
    "iops",
    "throughput",
    "encrypted",
    "snapshot_id",
    "create_time",
    "age_days",
    "monthly_cost_usd",
    "price_source",
    "protected_by",
]


def write_csv(result: ScanResult, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for vol in result.volumes:
            row = vol.to_dict()
            row["name"] = vol.name
            writer.writerow(row)


def to_json(result: ScanResult) -> dict[str, Any]:
    return {
        "tool": "ebs-orphan-finder",
        "version": __version__,
        "generated_at": result.generated_at.isoformat(),
        "min_age_days": result.min_age_days,
        "summary": {
            "accounts_scanned": result.accounts_scanned,
            "regions_scanned": result.regions_scanned,
            "volumes": len(result.volumes),
            "total_gib": result.total_gib,
            "monthly_cost_usd": result.total_monthly_cost,
            "annual_cost_usd": round(result.total_monthly_cost * 12, 2),
            "errors": len(result.errors),
        },
        "volumes": [v.to_dict() for v in result.volumes],
        "errors": [e.__dict__ for e in result.errors],
    }


def write_json(result: ScanResult, path: Path) -> None:
    path.write_text(json.dumps(to_json(result), indent=2) + "\n", encoding="utf-8")


def _group(
    volumes: list[OrphanVolume], key: Callable[[OrphanVolume], str]
) -> list[tuple[str, int, int, float]]:
    groups: dict[str, list[OrphanVolume]] = defaultdict(list)
    for vol in volumes:
        groups[key(vol)].append(vol)
    rows = [
        (name, len(vs), sum(v.size_gib for v in vs), round(sum(v.monthly_cost_usd for v in vs), 2))
        for name, vs in groups.items()
    ]
    return sorted(rows, key=lambda r: (-r[3], r[0]))


def _money(value: float) -> str:
    return f"${value:,.2f}"


def render_markdown(result: ScanResult, top: int = 20) -> str:
    vols = result.volumes
    protected = [v for v in vols if v.protected]
    fallback = sorted({v.region for v in vols if v.price_source.startswith("fallback")})
    unknown = sorted({v.volume_type for v in vols if v.price_source == "unknown"})

    lines = [
        "# Unattached EBS volumes report",
        "",
        f"Generated {result.generated_at:%Y-%m-%d %H:%M} UTC · "
        f"volumes unattached and created at least **{result.min_age_days} days** ago.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Accounts scanned | {result.accounts_scanned} |",
        f"| Account/region pairs scanned | {result.regions_scanned} |",
        f"| Unattached volumes | {len(vols)} ({len(protected)} protected by tag) |",
        f"| Provisioned storage | {result.total_gib:,} GiB |",
        f"| Estimated cost | **{_money(result.total_monthly_cost)} / month** · "
        f"{_money(result.total_monthly_cost * 12)} / year |",
        f"| Scan errors | {len(result.errors)} |",
        "",
    ]
    if fallback:
        lines += [
            f"> Prices for {', '.join(fallback)} are not in the price table; "
            "those volumes were priced with the fallback region. Use `--price-file` "
            "for exact figures.",
            "",
        ]
    if unknown:
        lines += [f"> No price for volume type(s) {', '.join(unknown)}: counted as $0.", ""]

    for title, key in (
        ("By account", lambda v: f"{v.account_id} {v.account_name}".strip()),
        ("By region", lambda v: v.region),
        ("By volume type", lambda v: v.volume_type),
    ):
        lines += [f"## {title}", "", "| | Volumes | GiB | $/month |", "|---|---:|---:|---:|"]
        lines += [f"| {n} | {c} | {g:,} | {_money(m)} |" for n, c, g, m in _group(vols, key)]
        lines.append("")

    lines += [
        f"## Top {min(top, len(vols))} by cost",
        "",
        "| Account | Region | Volume | Name | Type | GiB | Age (days) | $/month | Protected |",
        "|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for v in vols[:top]:
        lines.append(
            f"| {v.account_id} | {v.region} | `{v.volume_id}` | {v.name} | {v.volume_type} | "
            f"{v.size_gib:,} | {v.age_days} | {_money(v.monthly_cost_usd)} | {v.protected_by} |"
        )
    lines.append("")

    if result.errors:
        lines += [
            "## Scan errors",
            "",
            "These accounts or regions were **not** scanned; the totals above exclude them.",
            "",
            "| Account | Region | Error | Message |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {e.account_id} | {e.region} | {e.code} | {e.message.replace('|', '/')} |"
            for e in result.errors
        ]
        lines.append("")

    lines += [
        "---",
        "Costs are on-demand list-price estimates for the volumes only (snapshots are "
        "not included). Age is measured from creation time: EC2 does not record when a "
        "volume was detached.",
        "",
    ]
    return "\n".join(lines)


def write_markdown(result: ScanResult, path: Path) -> None:
    path.write_text(render_markdown(result), encoding="utf-8")
