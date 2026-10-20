"""Which accounts to scan, and how to get credentials into each one."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

import boto3
from botocore.config import Config

from ebs_orphans.models import Account

log = logging.getLogger(__name__)

# Adaptive retries back off on throttling. Org-wide scans fan out to many
# account/region pairs at once, so throttling is expected, not exceptional.
# Explicit timeouts: botocore's defaults (60 s connect, retried up to
# max_attempts) let one unreachable regional endpoint stall a whole scan for
# many minutes. With these, a dead region fails in well under a minute and is
# reported as a scan error while every other region completes.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
MAX_ATTEMPTS = 5

BOTO_CONFIG = Config(
    connect_timeout=CONNECT_TIMEOUT_S,
    read_timeout=READ_TIMEOUT_S,
    retries={"max_attempts": MAX_ATTEMPTS, "mode": "adaptive"},
    user_agent_extra="ebs-orphan-finder",
)

ACTIVE_STATES = frozenset({"ACTIVE"})


def current_account_id(session: boto3.Session) -> str:
    return str(session.client("sts", config=BOTO_CONFIG).get_caller_identity()["Account"])


def list_org_accounts(session: boto3.Session, ou_ids: Iterable[str] = ()) -> list[Account]:
    """Active accounts in the organization, or only under the given OUs (recursively).

    Must run in the management account or an Organizations delegated administrator.
    """
    org = session.client("organizations", config=BOTO_CONFIG)
    raw: list[dict[str, str]] = []
    ou_list = list(ou_ids)
    if ou_list:
        for ou_id in ou_list:
            raw.extend(_accounts_under(org, ou_id))
    else:
        for page in org.get_paginator("list_accounts").paginate():
            raw.extend(page["Accounts"])  # type: ignore[arg-type]

    accounts: dict[str, Account] = {}
    for acc in raw:
        # "State" replaced "Status" in 2025; read either.
        state = acc.get("State") or acc.get("Status", "")
        if state not in ACTIVE_STATES:
            log.info("skipping account %s: state %s", acc["Id"], state)
            continue
        accounts[acc["Id"]] = Account(id=acc["Id"], name=acc.get("Name", ""))
    return sorted(accounts.values(), key=lambda a: a.id)


def _accounts_under(org: object, parent_id: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for page in org.get_paginator("list_accounts_for_parent").paginate(  # type: ignore[attr-defined]
        ParentId=parent_id
    ):
        found.extend(page["Accounts"])
    for page in org.get_paginator("list_organizational_units_for_parent").paginate(  # type: ignore[attr-defined]
        ParentId=parent_id
    ):
        for child in page["OrganizationalUnits"]:
            found.extend(_accounts_under(org, child["Id"]))
    return found


def filter_accounts(
    accounts: Iterable[Account], include: Iterable[str] = (), exclude: Iterable[str] = ()
) -> list[Account]:
    include_set, exclude_set = set(include), set(exclude)
    return [
        a for a in accounts if (not include_set or a.id in include_set) and a.id not in exclude_set
    ]


class SessionFactory:
    """Hands out one boto3 session per account.

    The account the tool runs in uses the caller's own credentials (the management
    account usually has no ``OrganizationAccountAccessRole`` of its own). Every
    other account is reached with ``sts:AssumeRole``. Sessions are cached per
    account and are thread-safe to request.
    """

    def __init__(
        self,
        base: boto3.Session,
        role_name: str,
        external_id: str | None = None,
        session_name: str = "ebs-orphan-finder",
        duration_seconds: int = 3600,
    ) -> None:
        self.base = base
        self.role_name = role_name
        self.external_id = external_id
        self.session_name = session_name
        self.duration_seconds = duration_seconds
        self.home_account = current_account_id(base)
        self._partition = self._detect_partition()
        self._cache: dict[str, boto3.Session] = {}
        self._lock = threading.Lock()

    def _detect_partition(self) -> str:
        return str(self.base.get_partition_for_region(self.base.region_name or "us-east-1"))

    def role_arn(self, account_id: str) -> str:
        return f"arn:{self._partition}:iam::{account_id}:role/{self.role_name}"

    def for_account(self, account_id: str) -> boto3.Session:
        with self._lock:
            if account_id in self._cache:
                return self._cache[account_id]
        session = self.base if account_id == self.home_account else self._assume(account_id)
        with self._lock:
            self._cache[account_id] = session
        return session

    def _assume(self, account_id: str) -> boto3.Session:
        sts = self.base.client("sts", config=BOTO_CONFIG)
        params: dict[str, object] = {
            "RoleArn": self.role_arn(account_id),
            "RoleSessionName": self.session_name,
            "DurationSeconds": self.duration_seconds,
        }
        if self.external_id:
            params["ExternalId"] = self.external_id
        creds = sts.assume_role(**params)["Credentials"]  # type: ignore[arg-type]
        log.debug("assumed %s", params["RoleArn"])
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.base.region_name,
        )
