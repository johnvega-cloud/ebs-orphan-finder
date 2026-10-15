from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, WaiterError

from ebs_orphans.discovery import scan
from ebs_orphans.models import Account, ProtectRule
from ebs_orphans.plan import PlanError, _apply_item, apply_plan, build_plan
from ebs_orphans.pricing import PriceBook
from tests.conftest import REGION, Org

PROTECT = [ProtectRule("DoNotDelete")]


def _plan(org: Org, prices: PriceBook, snapshot: bool = True) -> dict[str, Any]:
    result = scan(
        [Account(org.management), Account(org.members[0])],
        org.factory.for_account,
        min_age_days=0,
        prices=prices,
        regions=[REGION],
        protect=PROTECT,
    )
    return build_plan(result, PROTECT, snapshot_before_delete=snapshot)


def _exists(org: Org, account: str, volume_id: str) -> bool:
    vols = org.ec2(account).describe_volumes(
        Filters=[{"Name": "volume-id", "Values": [volume_id]}]
    )["Volumes"]
    return bool(vols)


def _our_snapshots(org: Org, account: str) -> list[Any]:
    # Filter by our tag: moto's built-in AMIs own many snapshots in the default account.
    return list(
        org.ec2(account).describe_snapshots(
            OwnerIds=["self"], Filters=[{"Name": "tag-key", "Values": ["ebs-orphans:plan-id"]}]
        )["Snapshots"]
    )


def test_plan_excludes_protected_volumes(org: Org, prices: PriceBook) -> None:
    keep = org.volume(
        org.management,
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "DoNotDelete", "Value": "yes"}]}
        ],
    )
    drop = org.volume(org.members[0], Size=10, VolumeType="gp2")
    plan = _plan(org, prices)
    assert [i["volume_id"] for i in plan["items"]] == [drop]
    assert plan["excluded"][0]["volume_id"] == keep
    assert plan["summary"] == {
        "volumes": 1,
        "total_gib": 10,
        "monthly_savings_usd": 1.0,
        "excluded": 1,
    }
    assert plan["protect_tags"] == ["DoNotDelete"]


def test_dry_run_changes_nothing(org: Org, prices: PriceBook) -> None:
    vol = org.volume(org.members[0])
    outcomes = apply_plan(_plan(org, prices), org.factory.for_account)
    assert [(o.volume_id, o.action) for o in outcomes] == [(vol, "would-delete")]
    assert outcomes[0].ok
    assert _exists(org, org.members[0], vol)
    assert _our_snapshots(org, org.members[0]) == []


def test_execute_snapshots_then_deletes_and_is_idempotent(org: Org, prices: PriceBook) -> None:
    vol = org.volume(
        org.members[0],
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "CostCenter", "Value": "42"}]}
        ],
    )
    plan = _plan(org, prices)

    [first] = apply_plan(plan, org.factory.for_account, execute=True)
    assert first.action == "deleted"
    assert first.snapshot_id.startswith("snap-")
    assert not _exists(org, org.members[0], vol)
    snap = org.ec2(org.members[0]).describe_snapshots(SnapshotIds=[first.snapshot_id])["Snapshots"][
        0
    ]
    tags = {t["Key"]: t["Value"] for t in snap["Tags"]}
    assert tags["ebs-orphans:source-volume"] == vol
    assert tags["ebs-orphans:plan-id"] == plan["plan_id"]
    assert tags["CostCenter"] == "42"

    # Re-running the same plan (e.g. after an interruption) is a no-op.
    [second] = apply_plan(plan, org.factory.for_account, execute=True)
    assert second.action == "already-gone"
    assert second.ok


def test_execute_without_snapshot(org: Org, prices: PriceBook) -> None:
    vol = org.volume(org.management)
    [outcome] = apply_plan(
        _plan(org, prices, snapshot=False), org.factory.for_account, execute=True
    )
    assert (outcome.action, outcome.snapshot_id) == ("deleted", "")
    assert not _exists(org, org.management, vol)


def test_reuses_snapshot_from_interrupted_run(org: Org, prices: PriceBook) -> None:
    vol = org.volume(org.management)
    plan = _plan(org, prices)
    ec2 = org.ec2(org.management)
    earlier = ec2.create_snapshot(
        VolumeId=vol,
        TagSpecifications=[
            {
                "ResourceType": "snapshot",
                "Tags": [
                    {"Key": "ebs-orphans:source-volume", "Value": vol},
                    {"Key": "ebs-orphans:plan-id", "Value": plan["plan_id"]},
                ],
            }
        ],
    )["SnapshotId"]
    [outcome] = apply_plan(plan, org.factory.for_account, execute=True)
    assert (outcome.action, outcome.snapshot_id) == ("deleted", earlier)
    assert len(_our_snapshots(org, org.management)) == 1


def test_skips_volume_that_gained_a_protect_tag(org: Org, prices: PriceBook) -> None:
    vol = org.volume(org.management)
    plan = _plan(org, prices)
    org.ec2(org.management).create_tags(
        Resources=[vol], Tags=[{"Key": "DoNotDelete", "Value": "true"}]
    )
    [outcome] = apply_plan(plan, org.factory.for_account, execute=True)
    assert outcome.action == "skipped-protected"
    assert not outcome.ok
    assert _exists(org, org.management, vol)


def test_extra_protect_rule_at_apply_time(org: Org, prices: PriceBook) -> None:
    vol = org.volume(
        org.management,
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "Owner", "Value": "legal-hold"}]}
        ],
    )
    [outcome] = apply_plan(
        _plan(org, prices),
        org.factory.for_account,
        execute=True,
        extra_protect=[ProtectRule.parse("Owner=legal-hold")],
    )
    assert outcome.action == "skipped-protected"
    assert _exists(org, org.management, vol)


def test_skips_volume_that_was_reattached(org: Org, prices: PriceBook) -> None:
    vol = org.volume(org.management)
    plan = _plan(org, prices)
    ec2 = org.ec2(org.management)
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instance = ec2.run_instances(
        ImageId=image, MinCount=1, MaxCount=1, Placement={"AvailabilityZone": f"{REGION}a"}
    )["Instances"][0]["InstanceId"]
    ec2.attach_volume(VolumeId=vol, InstanceId=instance, Device="/dev/sdf")
    [outcome] = apply_plan(plan, org.factory.for_account, execute=True)
    assert outcome.action == "skipped-changed"
    assert "in-use" in outcome.detail


def test_skips_volume_that_was_resized(org: Org, prices: PriceBook) -> None:
    org.volume(org.management)
    plan = _plan(org, prices)
    plan["items"][0]["size_gib"] = 1
    [outcome] = apply_plan(plan, org.factory.for_account, execute=True)
    assert outcome.action == "skipped-changed"


def test_refuses_expired_plan(org: Org, prices: PriceBook) -> None:
    org.volume(org.management)
    plan = _plan(org, prices)
    with pytest.raises(PlanError, match="Re-run the scan"):
        apply_plan(plan, org.factory.for_account, now=datetime.now(UTC) + timedelta(hours=73))
    # 0 disables the age check.
    outcomes = apply_plan(
        plan, org.factory.for_account, max_age_hours=0, now=datetime.now(UTC) + timedelta(days=9)
    )
    assert outcomes[0].action == "would-delete"


def test_refuses_foreign_file(org: Org) -> None:
    with pytest.raises(PlanError, match="deletion-plan"):
        apply_plan({"kind": "something-else"}, org.factory.for_account)


def test_account_session_failure_marks_items_as_error(org: Org, prices: PriceBook) -> None:
    org.volume(org.members[0])
    org.volume(org.members[0])
    plan = _plan(org, prices)
    calls = []

    def get_session(account_id: str) -> boto3.Session:
        calls.append(account_id)
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")

    outcomes = apply_plan(plan, get_session)
    assert [o.action for o in outcomes] == ["error", "error"]
    assert "AccessDenied" in outcomes[0].detail
    assert len(calls) == 1  # one attempt per account, not per volume


class FakeEC2:
    """Minimal EC2 stand-in for error paths moto cannot produce (IAM denials, waiter timeout)."""

    def __init__(self, deny: str = "", describe_error: str = "", waiter_fails: bool = False):
        self.deny = deny
        self.describe_error = describe_error
        self.waiter_fails = waiter_fails
        self.deleted: list[str] = []

    def describe_volumes(self, VolumeIds: list[str]) -> dict[str, Any]:
        if self.describe_error:
            raise ClientError({"Error": {"Code": self.describe_error}}, "DescribeVolumes")
        return {"Volumes": [{"State": "available", "CreateTime": CREATED, "Size": 10, "Tags": []}]}

    def _call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if self.deny == name:
            raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, name)
        if kwargs.get("DryRun"):
            raise ClientError({"Error": {"Code": "DryRunOperation"}}, name)
        return {"SnapshotId": "snap-1"}

    def create_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("CreateSnapshot", **kwargs)

    def delete_volume(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs["VolumeId"])
        return self._call("DeleteVolume", **kwargs)

    def describe_snapshots(self, **_: Any) -> dict[str, Any]:
        return {"Snapshots": []}

    def get_waiter(self, _: str) -> Any:
        fails = self.waiter_fails

        class Waiter:
            def wait(self, **_: Any) -> None:
                if fails:
                    raise WaiterError("SnapshotCompleted", "Max attempts exceeded", {})

        return Waiter()


CREATED = datetime(2026, 1, 1, tzinfo=UTC)
ITEM = {
    "account_id": "1",
    "region": REGION,
    "volume_id": "vol-1",
    "create_time": CREATED.isoformat(),
    "size_gib": 10,
}


@pytest.mark.parametrize("denied", ["CreateSnapshot", "DeleteVolume"])
def test_dry_run_reports_missing_permissions(denied: str) -> None:
    outcome = _apply_item(FakeEC2(deny=denied), "p", ITEM, [], True, False, 1)
    assert (outcome.action, outcome.detail) == ("denied", f"{denied} not allowed")


def test_snapshot_timeout_keeps_the_volume() -> None:
    ec2 = FakeEC2(waiter_fails=True)
    outcome = _apply_item(ec2, "p", ITEM, [], True, True, 1)
    assert outcome.action == "error"
    assert outcome.snapshot_id == ""
    assert ec2.deleted == []


def test_delete_failure_is_reported() -> None:
    outcome = _apply_item(FakeEC2(deny="DeleteVolume"), "p", ITEM, [], False, True, 1)
    assert outcome.action == "error"
    assert "UnauthorizedOperation" in outcome.detail


def test_describe_failure_is_reported() -> None:
    outcome = _apply_item(
        FakeEC2(describe_error="RequestLimitExceeded"), "p", ITEM, [], True, True, 1
    )
    assert outcome.action == "error"
