from __future__ import annotations

import boto3
import pytest

from ebs_orphans.accounts import BOTO_CONFIG, SessionFactory, filter_accounts, list_org_accounts
from ebs_orphans.models import Account
from tests.conftest import REGION, Org


def test_lists_every_active_account(org: Org) -> None:
    ids = [a.id for a in list_org_accounts(org.base)]
    assert set(ids) == {org.management, *org.members}
    assert ids == sorted(ids)


def test_skips_suspended_accounts(org: Org) -> None:
    org.base.client("organizations").close_account(AccountId=org.members[0])
    ids = {a.id for a in list_org_accounts(org.base)}
    assert org.members[0] not in ids
    assert org.members[1] in ids


def test_ou_scope_is_recursive(org: Org) -> None:
    client = org.base.client("organizations")
    root = client.list_roots()["Roots"][0]["Id"]
    workloads = client.create_organizational_unit(ParentId=root, Name="Workloads")[
        "OrganizationalUnit"
    ]["Id"]
    prod = client.create_organizational_unit(ParentId=workloads, Name="Prod")["OrganizationalUnit"][
        "Id"
    ]
    client.move_account(AccountId=org.members[1], SourceParentId=root, DestinationParentId=prod)
    accounts = list_org_accounts(org.base, [workloads])
    assert [a.id for a in accounts] == [org.members[1]]
    assert accounts[0].name == "workload-b"


def test_filter_accounts() -> None:
    accounts = [Account("1"), Account("2"), Account("3")]
    assert [a.id for a in filter_accounts(accounts, include=["1", "2"], exclude=["2"])] == ["1"]
    assert [a.id for a in filter_accounts(accounts, exclude=["3"])] == ["1", "2"]


def test_home_account_uses_own_credentials(org: Org) -> None:
    assert org.factory.for_account(org.management) is org.base


def test_member_account_is_assumed_and_cached(org: Org) -> None:
    session = org.factory.for_account(org.members[0])
    assert session is not org.base
    identity = session.client("sts").get_caller_identity()
    assert identity["Account"] == org.members[0]
    assert "OrganizationAccountAccessRole" in identity["Arn"]
    assert org.factory.for_account(org.members[0]) is session


def test_external_id_and_partition(aws: None) -> None:
    factory = SessionFactory(boto3.Session(region_name=REGION), "Audit", external_id="x-123")
    assert factory.role_arn("111122223333") == "arn:aws:iam::111122223333:role/Audit"
    gov = SessionFactory(boto3.Session(region_name="us-gov-west-1"), "Audit")
    assert gov.role_arn("1").startswith("arn:aws-us-gov:")
    # Moto accepts the ExternalId; the point is that it is passed without error.
    assert (
        factory.for_account("111122223333").client("sts").get_caller_identity()["Account"]
        == "111122223333"
    )


@pytest.mark.parametrize("state_key", ["State", "Status"])
def test_reads_either_state_field(monkeypatch: pytest.MonkeyPatch, state_key: str) -> None:
    class FakePaginator:
        def paginate(self, **_: object) -> list[dict[str, object]]:
            return [
                {
                    "Accounts": [
                        {"Id": "1", "Name": "a", state_key: "ACTIVE"},
                        {"Id": "2", "Name": "b", state_key: "SUSPENDED"},
                    ]
                }
            ]

    class FakeOrg:
        def get_paginator(self, _: str) -> FakePaginator:
            return FakePaginator()

    class FakeSession:
        def client(self, *_: object, **__: object) -> FakeOrg:
            return FakeOrg()

    accounts = list_org_accounts(FakeSession())  # type: ignore[arg-type]
    assert [a.id for a in accounts] == ["1"]


def test_boto_config_bounds_time_spent_on_a_dead_endpoint() -> None:
    # botocore defaults (60 s connect x 10 attempts) stalled a real scan for minutes.
    assert BOTO_CONFIG.connect_timeout <= 10
    assert BOTO_CONFIG.read_timeout <= 60
    # botocore renames the key in place to total_max_attempts once a client is built.
    retries = BOTO_CONFIG.retries
    assert retries.get("max_attempts", retries.get("total_max_attempts", 99)) <= 6
