"""Plain data types shared by discovery, reporting and the deletion plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Account:
    id: str
    name: str = ""


@dataclass(frozen=True)
class ProtectRule:
    """A tag that marks a volume as "do not delete".

    ``value=None`` matches any value of the key, except the explicit opt-out
    values in ``OPT_OUT`` (so ``DoNotDelete=false`` does not protect).
    """

    key: str
    value: str | None = None

    OPT_OUT = frozenset({"false", "no", "0"})

    @classmethod
    def parse(cls, text: str) -> ProtectRule:
        key, sep, value = text.partition("=")
        if not key:
            raise ValueError(f"invalid protect tag {text!r}: expected KEY or KEY=VALUE")
        return cls(key=key, value=value if sep else None)

    def matches(self, tags: dict[str, str]) -> bool:
        if self.key not in tags:
            return False
        actual = tags[self.key]
        if self.value is None:
            return actual.strip().lower() not in self.OPT_OUT
        return actual == self.value

    def __str__(self) -> str:
        return self.key if self.value is None else f"{self.key}={self.value}"


@dataclass(frozen=True)
class OrphanVolume:
    account_id: str
    account_name: str
    region: str
    volume_id: str
    volume_type: str
    size_gib: int
    iops: int | None
    throughput: int | None
    create_time: datetime
    age_days: int
    availability_zone: str
    encrypted: bool
    snapshot_id: str
    tags: dict[str, str] = field(default_factory=dict)
    monthly_cost_usd: float = 0.0
    price_source: str = ""
    protected_by: str = ""

    @property
    def protected(self) -> bool:
        return bool(self.protected_by)

    @property
    def name(self) -> str:
        return self.tags.get("Name", "")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["create_time"] = self.create_time.isoformat()
        data["protected"] = self.protected
        return data


@dataclass(frozen=True)
class ScanError:
    """A failure scoped to one account (region ``*``) or one account/region pair.

    The scan keeps going: one locked-down account must not hide the rest of the org.
    """

    account_id: str
    region: str
    code: str
    message: str


@dataclass
class ScanResult:
    generated_at: datetime
    min_age_days: int
    accounts_scanned: int = 0
    regions_scanned: int = 0
    volumes: list[OrphanVolume] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)

    @property
    def total_monthly_cost(self) -> float:
        return round(sum(v.monthly_cost_usd for v in self.volumes), 2)

    @property
    def total_gib(self) -> int:
        return sum(v.size_gib for v in self.volumes)
