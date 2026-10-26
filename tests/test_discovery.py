from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from ebs_orphans import discovery
from ebs_orphans.accounts import list_org_accounts
from ebs_orphans.discovery import enabled_regions, find_orphans, scan
from ebs_orphans.models import Account, ProtectRule
from ebs_orphans.pricing import PriceBook
from tests.conftest import OTHER_REGION, REGION, Org


def _attach(org: Org, account_id: str, volume_id: str) -> None:
    ec2 = org.ec2(account_id)
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instance = ec2.run_instances(
        ImageId=image,
        MinCount=1,
        MaxCount=1,
        Placement={"AvailabilityZone": f"{REGION}a"},
    )["Instances"][0]["InstanceId"]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance, Device="/dev/sdf")


def test_finds_unattached_volumes_across_accounts_and_regions(
    org: Org, prices: PriceBook, later: datetime
) -> None:
    mgmt_vol = org.volume(org.management, Size=50, VolumeType="gp2")
    member_vol = org.volume(org.members[0], OTHER_REGION, Size=200, VolumeType="gp3")
    attached = org.volume(org.members[1])
    _attach(org, org.members[1], attached)

    result = scan(
        list_org_accounts(org.base),
        org.factory.for_account,
        min_age_days=30,
        prices=prices,
        regions=[REGION, OTHER_REGION],
        now=later,
    )

    found = {v.volume_id: v for v in result.volumes}
    assert set(found) == {mgmt_vol, member_vol}
    assert found[member_vol].account_id == org.members[0]
    assert found[member_vol].account_name == "workload-a"
    assert found[member_vol].region == OTHER_REGION
    assert found[mgmt_vol].monthly_cost_usd == 5.0
    assert found[member_vol].price_source == "fallback:us-east-1"
    assert result.accounts_scanned == 3
    assert result.regions_scanned == 6
    assert result.errors == []
    # Sorted most expensive first.
    assert result.volumes[0].volume_id == member_vol
    assert result.total_gib == 250


def test_age_threshold(org: Org, prices: PriceBook, later: datetime) -> None:
    org.volume(org.management)
    account = Account(org.management)
    assert find_orphans(org.base, account, REGION, 30, later, prices) != []
    assert find_orphans(org.base, account, REGION, 41, later, prices) == []
    assert find_orphans(org.base, account, REGION, 0, datetime.now(UTC), prices) != []


def test_protected_volumes_are_reported_but_flagged(
    org: Org, prices: PriceBook, later: datetime
) -> None:
    org.volume(
        org.management,
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [{"Key": "DoNotDelete", "Value": "true"}, {"Key": "Name", "Value": "db"}],
            }
        ],
    )
    [vol] = find_orphans(
        org.base, Account(org.management), REGION, 0, later, prices, [ProtectRule("DoNotDelete")]
    )
    assert vol.protected
    assert vol.protected_by == "DoNotDelete"
    assert vol.name == "db"


def test_account_failure_is_recorded_not_fatal(
    org: Org, prices: PriceBook, later: datetime
) -> None:
    org.volume(org.management)
    denied = org.members[0]

    def get_session(account_id: str) -> boto3.Session:
        if account_id == denied:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "AssumeRole"
            )
        return org.factory.for_account(account_id)

    result = scan(list_org_accounts(org.base), get_session, 30, prices, regions=[REGION], now=later)
    assert len(result.volumes) == 1
    assert result.accounts_scanned == 2
    [error] = result.errors
    assert (error.account_id, error.region, error.code) == (denied, "*", "AccessDenied")


def test_region_failure_is_recorded_not_fatal(
    org: Org, prices: PriceBook, later: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    org.volume(org.management)
    real = discovery.find_orphans

    def flaky(session, account, region, *args, **kwargs):  # type: ignore[no-untyped-def]
        if region == OTHER_REGION:
            raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeVolumes")
        return real(session, account, region, *args, **kwargs)

    monkeypatch.setattr(discovery, "find_orphans", flaky)
    result = scan(
        [Account(org.management)],
        org.factory.for_account,
        30,
        prices,
        regions=[REGION, OTHER_REGION],
        now=later,
    )
    assert len(result.volumes) == 1
    assert result.regions_scanned == 1
    assert [(e.region, e.code) for e in result.errors] == [(OTHER_REGION, "UnauthorizedOperation")]


def test_unreachable_region_is_recorded_not_fatal(
    org: Org, prices: PriceBook, later: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seen for real: me-south-1's EC2 endpoint stopped answering (connect timeout).
    org.volume(org.management)
    real = discovery.find_orphans

    def dead_region(session, account, region, *args, **kwargs):  # type: ignore[no-untyped-def]
        if region == OTHER_REGION:
            raise ConnectTimeoutError(endpoint_url=f"https://ec2.{region}.amazonaws.com/")
        return real(session, account, region, *args, **kwargs)

    monkeypatch.setattr(discovery, "find_orphans", dead_region)
    result = scan(
        [Account(org.management)],
        org.factory.for_account,
        30,
        prices,
        regions=[REGION, OTHER_REGION],
        now=later,
    )
    assert len(result.volumes) == 1
    assert [(e.region, e.code) for e in result.errors] == [(OTHER_REGION, "ConnectTimeoutError")]


def test_enabled_regions_skips_not_opted_in(org: Org) -> None:
    regions = enabled_regions(org.base)
    assert REGION in regions
    assert regions == sorted(regions)


def test_scan_defaults_to_enabled_regions(
    org: Org, prices: PriceBook, later: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "enabled_regions", lambda _session: [REGION])
    org.volume(org.management)
    result = scan([Account(org.management)], org.factory.for_account, 30, prices, now=later)
    assert result.regions_scanned == 1
    assert len(result.volumes) == 1


def test_naive_create_time_is_treated_as_utc(prices: PriceBook) -> None:
    vol = {
        "VolumeId": "vol-1",
        "VolumeType": "gp2",
        "Size": 1,
        "CreateTime": datetime(2026, 1, 1),
    }
    orphan = discovery._to_orphan(
        vol, Account("1"), REGION, datetime(2026, 1, 11, tzinfo=UTC), prices, []
    )
    assert orphan.age_days == 10
    assert orphan.create_time.tzinfo is UTC
