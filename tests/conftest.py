"""Shared fixtures: a fake AWS Organization with a management and two member accounts.

Everything runs against moto, in memory. No test can reach real AWS: the
credentials below are fake and the ``mock_aws`` context intercepts every call.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from ebs_orphans.accounts import SessionFactory
from ebs_orphans.pricing import PriceBook

REGION = "us-east-1"
OTHER_REGION = "eu-west-1"
ROLE = "OrganizationAccountAccessRole"


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@dataclass
class Org:
    base: boto3.Session
    factory: SessionFactory
    management: str
    members: list[str]

    def ec2(self, account_id: str, region: str = REGION):  # type: ignore[no-untyped-def]
        return self.factory.for_account(account_id).client("ec2", region_name=region)

    def volume(self, account_id: str, region: str = REGION, **kwargs: object) -> str:
        params: dict[str, object] = {"AvailabilityZone": f"{region}a", "Size": 100}
        params.update(kwargs)
        return str(self.ec2(account_id, region).create_volume(**params)["VolumeId"])


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def org(aws: None) -> Org:
    base = boto3.Session(region_name=REGION)
    client = base.client("organizations")
    client.create_organization(FeatureSet="ALL")
    members = [
        client.create_account(AccountName=name, Email=f"{name}@example.com")["CreateAccountStatus"][
            "AccountId"
        ]
        for name in ("workload-a", "workload-b")
    ]
    factory = SessionFactory(base, ROLE)
    return Org(base=base, factory=factory, management=factory.home_account, members=members)


@pytest.fixture
def prices() -> PriceBook:
    return PriceBook.load()


@pytest.fixture
def later() -> datetime:
    """'Now' for the scan, 40 days after moto created the volumes."""
    return datetime.now(UTC) + timedelta(days=40)
