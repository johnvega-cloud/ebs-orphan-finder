"""Deletion plan: a reviewable file between "found" and "deleted".

The scan never deletes anything. It can write a plan file that a person reviews
(or a change process approves). ``apply`` then re-checks every volume against
the live API before acting, because the world may have changed since the scan:

* a volume that no longer exists is reported as ``already-gone`` (re-running an
  interrupted apply is safe: idempotent);
* a volume that was re-attached, resized or replaced is skipped;
* a volume that gained a protect tag since the scan is skipped;
* a plan older than ``max_age_hours`` is refused outright.

Without ``--execute`` every call uses ``DryRun=True``: nothing changes, and the
run still proves the role has the permissions the real run will need.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ebs_orphans import __version__
from ebs_orphans.accounts import BOTO_CONFIG
from ebs_orphans.models import ProtectRule, ScanResult

log = logging.getLogger(__name__)

PLAN_KIND = "ebs-orphan-finder/deletion-plan"
PLAN_VERSION = 1
TAG_PREFIX = "ebs-orphans:"
MAX_TAGS = 50  # EC2 limit per resource

SessionGetter = Callable[[str], boto3.Session]


class PlanError(Exception):
    """The plan file cannot be applied (wrong format, expired)."""


@dataclass(frozen=True)
class Outcome:
    account_id: str
    region: str
    volume_id: str
    action: str
    detail: str = ""
    snapshot_id: str = ""

    # Actions that mean "the plan did what it should" (vs. needs a look).
    OK_ACTIONS = frozenset({"would-delete", "deleted", "already-gone"})

    @property
    def ok(self) -> bool:
        return self.action in self.OK_ACTIONS


def build_plan(
    result: ScanResult,
    protect: Sequence[ProtectRule],
    snapshot_before_delete: bool = True,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for v in result.volumes:
        ref = {"account_id": v.account_id, "region": v.region, "volume_id": v.volume_id}
        if v.protected:
            excluded.append({**ref, "reason": f"protected by tag {v.protected_by}"})
            continue
        items.append(
            {
                **ref,
                "name": v.name,
                "volume_type": v.volume_type,
                "size_gib": v.size_gib,
                "create_time": v.create_time.isoformat(),
                "age_days": v.age_days,
                "monthly_cost_usd": v.monthly_cost_usd,
            }
        )
    return {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "tool_version": __version__,
        "plan_id": uuid.uuid4().hex[:12],
        "generated_at": result.generated_at.isoformat(),
        "min_age_days": result.min_age_days,
        "snapshot_before_delete": snapshot_before_delete,
        "protect_tags": [str(p) for p in protect],
        "summary": {
            "volumes": len(items),
            "total_gib": sum(i["size_gib"] for i in items),
            "monthly_savings_usd": round(sum(i["monthly_cost_usd"] for i in items), 2),
            "excluded": len(excluded),
        },
        "items": items,
        "excluded": excluded,
    }


def validate_plan(plan: dict[str, Any], now: datetime, max_age_hours: float) -> None:
    if plan.get("kind") != PLAN_KIND or plan.get("version") != PLAN_VERSION:
        raise PlanError(f"not a {PLAN_KIND} v{PLAN_VERSION} file")
    generated = datetime.fromisoformat(plan["generated_at"])
    if max_age_hours > 0 and now - generated > timedelta(hours=max_age_hours):
        raise PlanError(
            f"plan {plan['plan_id']} was generated {generated:%Y-%m-%d %H:%M} UTC, more than "
            f"{max_age_hours:g} h ago. Re-run the scan and review a fresh plan."
        )


def apply_plan(
    plan: dict[str, Any],
    get_session: SessionGetter,
    execute: bool = False,
    extra_protect: Sequence[ProtectRule] = (),
    max_age_hours: float = 72,
    snapshot_timeout_minutes: int = 120,
    now: datetime | None = None,
) -> list[Outcome]:
    now = now or datetime.now(UTC)
    validate_plan(plan, now, max_age_hours)
    protect = [ProtectRule.parse(p) for p in plan.get("protect_tags", [])] + list(extra_protect)
    snapshot = bool(plan.get("snapshot_before_delete", True))
    mode = "EXECUTE" if execute else "dry-run"
    log.info("applying plan %s (%d volumes, %s)", plan["plan_id"], len(plan["items"]), mode)

    outcomes: list[Outcome] = []
    sessions: dict[str, boto3.Session | Exception] = {}
    for item in plan["items"]:
        acct = item["account_id"]
        if acct not in sessions:
            try:
                sessions[acct] = get_session(acct)
            except (ClientError, BotoCoreError) as exc:
                sessions[acct] = exc
        session = sessions[acct]
        if isinstance(session, Exception):
            outcome = Outcome(acct, item["region"], item["volume_id"], "error", _describe(session))
        else:
            ec2 = session.client("ec2", region_name=item["region"], config=BOTO_CONFIG)
            outcome = _apply_item(
                ec2, plan["plan_id"], item, protect, snapshot, execute, snapshot_timeout_minutes
            )
        log.info(
            "%s %s/%s %s %s",
            outcome.action,
            outcome.account_id,
            outcome.region,
            outcome.volume_id,
            outcome.detail,
        )
        outcomes.append(outcome)
    return outcomes


def _describe(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', '')}: {err.get('Message', '')}"
    return f"{type(exc).__name__}: {exc}"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _apply_item(
    ec2: Any,
    plan_id: str,
    item: dict[str, Any],
    protect: Sequence[ProtectRule],
    snapshot: bool,
    execute: bool,
    snapshot_timeout_minutes: int,
) -> Outcome:
    acct, region, vol_id = item["account_id"], item["region"], item["volume_id"]

    def out(action: str, detail: str = "", snapshot_id: str = "") -> Outcome:
        return Outcome(acct, region, vol_id, action, detail, snapshot_id)

    # 1. Re-check the live volume against what was reviewed.
    try:
        live = ec2.describe_volumes(VolumeIds=[vol_id])["Volumes"][0]
    except ClientError as exc:
        if _code(exc) == "InvalidVolume.NotFound":
            return out("already-gone")
        return out("error", _describe(exc))
    if live["State"] != "available":
        return out("skipped-changed", f"state is now {live['State']}")
    created = live["CreateTime"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if created != datetime.fromisoformat(item["create_time"]) or live["Size"] != item["size_gib"]:
        return out("skipped-changed", "size or creation time differs from the plan")
    tags = {t["Key"]: t["Value"] for t in live.get("Tags", [])}
    rule = next((r for r in protect if r.matches(tags)), None)
    if rule:
        return out("skipped-protected", f"tag {rule}")

    # 2. Dry run: prove permissions, change nothing.
    if not execute:
        calls: list[tuple[str, Callable[[], Any]]] = []
        if snapshot:
            calls.append(
                ("CreateSnapshot", lambda: ec2.create_snapshot(VolumeId=vol_id, DryRun=True))
            )
        calls.append(("DeleteVolume", lambda: ec2.delete_volume(VolumeId=vol_id, DryRun=True)))
        for name, call in calls:
            try:
                call()
            except ClientError as exc:
                if _code(exc) == "DryRunOperation":
                    continue
                if _code(exc) == "UnauthorizedOperation":
                    return out("denied", f"{name} not allowed")
                return out("error", _describe(exc))
        return out("would-delete", "with snapshot" if snapshot else "no snapshot")

    # 3. Execute: optional snapshot (reused if a previous run already made it), then delete.
    snapshot_id = ""
    try:
        if snapshot:
            snapshot_id = _ensure_snapshot(ec2, plan_id, vol_id, tags, snapshot_timeout_minutes)
        ec2.delete_volume(VolumeId=vol_id)
    except ClientError as exc:
        if _code(exc) == "InvalidVolume.NotFound":
            return out("already-gone", snapshot_id=snapshot_id)
        return out("error", _describe(exc), snapshot_id)
    except BotoCoreError as exc:  # e.g. WaiterError: snapshot not done in time; volume kept
        return out("error", _describe(exc), snapshot_id)
    return out("deleted", snapshot_id=snapshot_id)


def _ensure_snapshot(
    ec2: Any, plan_id: str, vol_id: str, volume_tags: dict[str, str], timeout_minutes: int
) -> str:
    existing = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[
            {"Name": f"tag:{TAG_PREFIX}source-volume", "Values": [vol_id]},
            {"Name": f"tag:{TAG_PREFIX}plan-id", "Values": [plan_id]},
        ],
    )["Snapshots"]
    usable = [s for s in existing if s["State"] in ("pending", "completed")]
    if usable:
        snapshot_id = str(usable[0]["SnapshotId"])
        log.info("reusing snapshot %s for %s", snapshot_id, vol_id)
    else:
        own = {
            f"{TAG_PREFIX}source-volume": vol_id,
            f"{TAG_PREFIX}plan-id": plan_id,
            f"{TAG_PREFIX}deleted-at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Keep the volume's own tags (owner, cost center) so the snapshot stays attributable.
        kept = [(k, v) for k, v in volume_tags.items() if not k.startswith("aws:")]
        kept = kept[: MAX_TAGS - len(own)]
        tags = [{"Key": k, "Value": v} for k, v in [*kept, *own.items()]]
        snapshot_id = str(
            ec2.create_snapshot(
                VolumeId=vol_id,
                Description=f"Final snapshot of {vol_id} before deletion (plan {plan_id})",
                TagSpecifications=[{"ResourceType": "snapshot", "Tags": tags}],
            )["SnapshotId"]
        )
        log.info("created snapshot %s for %s", snapshot_id, vol_id)
    delay = 15
    ec2.get_waiter("snapshot_completed").wait(
        SnapshotIds=[snapshot_id],
        WaiterConfig={"Delay": delay, "MaxAttempts": max(1, timeout_minutes * 60 // delay)},
    )
    return snapshot_id
