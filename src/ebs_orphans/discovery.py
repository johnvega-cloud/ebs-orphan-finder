"""Scan account/region pairs for unattached EBS volumes, in parallel, read-only."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ebs_orphans.accounts import BOTO_CONFIG
from ebs_orphans.models import Account, OrphanVolume, ProtectRule, ScanError, ScanResult
from ebs_orphans.pricing import PriceBook

log = logging.getLogger(__name__)

SessionGetter = Callable[[str], boto3.Session]


def enabled_regions(session: boto3.Session) -> list[str]:
    """Regions enabled in this account (opt-in regions are per account)."""
    ec2 = session.client("ec2", region_name=session.region_name or "us-east-1", config=BOTO_CONFIG)
    regions = ec2.describe_regions(AllRegions=False)["Regions"]
    return sorted(r["RegionName"] for r in regions if r.get("OptInStatus", "") != "not-opted-in")


def find_orphans(
    session: boto3.Session,
    account: Account,
    region: str,
    min_age_days: int,
    now: datetime,
    prices: PriceBook,
    protect: Sequence[ProtectRule] = (),
) -> list[OrphanVolume]:
    """Volumes in ``available`` state (attached to nothing) created ``min_age_days`` ago or more.

    The age is measured from ``CreateTime``: EC2 does not record when a volume was
    detached. A volume detached yesterday but created a year ago counts as old, so
    the deletion plan never runs without review (see the README).
    """
    ec2 = session.client("ec2", region_name=region, config=BOTO_CONFIG)
    found: list[OrphanVolume] = []
    pages = ec2.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}],
        PaginationConfig={"PageSize": 500},
    )
    for page in pages:
        for vol in page["Volumes"]:
            volume = _to_orphan(vol, account, region, now, prices, protect)
            if volume.age_days >= min_age_days:
                found.append(volume)
    return found


def _to_orphan(
    vol: Any,
    account: Account,
    region: str,
    now: datetime,
    prices: PriceBook,
    protect: Sequence[ProtectRule],
) -> OrphanVolume:
    created: datetime = vol["CreateTime"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
    cost, source = prices.monthly_cost(
        region, vol["VolumeType"], vol["Size"], vol.get("Iops"), vol.get("Throughput")
    )
    rule = next((r for r in protect if r.matches(tags)), None)
    return OrphanVolume(
        account_id=account.id,
        account_name=account.name,
        region=region,
        volume_id=vol["VolumeId"],
        volume_type=vol["VolumeType"],
        size_gib=vol["Size"],
        iops=vol.get("Iops"),
        throughput=vol.get("Throughput"),
        create_time=created,
        age_days=max(0, (now - created).days),
        availability_zone=vol.get("AvailabilityZone", ""),
        encrypted=bool(vol.get("Encrypted", False)),
        snapshot_id=vol.get("SnapshotId", "") or "",
        tags=tags,
        monthly_cost_usd=cost,
        price_source=source,
        protected_by=str(rule) if rule else "",
    )


def _error(account_id: str, region: str, exc: Exception) -> ScanError:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return ScanError(account_id, region, err.get("Code", "ClientError"), err.get("Message", ""))
    return ScanError(account_id, region, type(exc).__name__, str(exc))


def scan(
    accounts: Iterable[Account],
    get_session: SessionGetter,
    min_age_days: int,
    prices: PriceBook,
    regions: Sequence[str] = (),
    protect: Sequence[ProtectRule] = (),
    max_workers: int = 16,
    now: datetime | None = None,
) -> ScanResult:
    """Scan every account x region pair. Failures are collected, never fatal.

    Two phases: resolve a session and the region list per account, then fan out
    one task per account/region pair. ``max_workers`` caps concurrent API calls.
    """
    now = now or datetime.now(UTC)
    result = ScanResult(generated_at=now, min_age_days=min_age_days)
    tasks: list[tuple[Account, str, boto3.Session]] = []

    for account in accounts:
        try:
            session = get_session(account.id)
            account_regions = list(regions) or enabled_regions(session)
        except (ClientError, BotoCoreError) as exc:
            err = _error(account.id, "*", exc)
            log.warning("account %s skipped: %s %s", account.id, err.code, err.message)
            result.errors.append(err)
            continue
        result.accounts_scanned += 1
        tasks.extend((account, region, session) for region in account_regions)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(find_orphans, s, a, r, min_age_days, now, prices, protect): (a, r)
            for a, r, s in tasks
        }
        for future in as_completed(futures):
            account, region = futures[future]
            try:
                volumes = future.result()
            except (ClientError, BotoCoreError) as exc:
                err = _error(account.id, region, exc)
                log.warning("%s/%s failed: %s %s", account.id, region, err.code, err.message)
                result.errors.append(err)
                continue
            result.regions_scanned += 1
            if volumes:
                log.info("%s/%s: %d unattached volume(s)", account.id, region, len(volumes))
            result.volumes.extend(volumes)

    result.volumes.sort(key=lambda v: (-v.monthly_cost_usd, v.account_id, v.region, v.volume_id))
    result.errors.sort(key=lambda e: (e.account_id, e.region))
    return result
