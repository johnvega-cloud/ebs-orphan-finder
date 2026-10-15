from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs_orphans.models import ProtectRule
from ebs_orphans.pricing import PriceBook


def test_gp2_is_size_times_rate(prices: PriceBook) -> None:
    assert prices.monthly_cost("us-east-1", "gp2", 100) == (10.0, "us-east-1")


def test_gp3_charges_only_iops_and_throughput_above_baseline(prices: PriceBook) -> None:
    assert prices.monthly_cost("us-east-1", "gp3", 100, 3000, 125) == (8.0, "us-east-1")
    # 100 GiB * 0.08 + 2000 IOPS * 0.005 + 125 MB/s * 0.04
    assert prices.monthly_cost("us-east-1", "gp3", 100, 5000, 250) == (23.0, "us-east-1")


def test_io1_charges_every_provisioned_iops(prices: PriceBook) -> None:
    assert prices.monthly_cost("us-east-1", "io1", 100, 1000) == (77.5, "us-east-1")


def test_io2_iops_are_tiered(prices: PriceBook) -> None:
    # 32000 * 0.065 + 32000 * 0.0455 + 6000 * 0.03185 = 3727.1, plus 100 GiB * 0.125
    cost, _ = prices.monthly_cost("us-east-1", "io2", 100, 70000)
    assert cost == pytest.approx(3739.6, abs=0.01)
    assert prices.monthly_cost("us-east-1", "io2", 10, 1000)[0] == pytest.approx(66.25)


def test_unknown_region_uses_fallback_and_says_so(prices: PriceBook) -> None:
    assert prices.monthly_cost("ap-southeast-7", "gp2", 10) == (1.0, "fallback:us-east-1")


def test_unknown_type_costs_zero_and_says_so(prices: PriceBook) -> None:
    assert prices.monthly_cost("us-east-1", "gp9", 10) == (0.0, "unknown")


def test_price_file_overrides_and_adds_regions(tmp_path: Path) -> None:
    override = tmp_path / "prices.json"
    override.write_text(
        json.dumps({"_meta": {"retrieved": "test"}, "eu-west-1": {"gp2": {"gb_month": 0.11}}})
    )
    book = PriceBook.load(override)
    assert book.monthly_cost("eu-west-1", "gp2", 100) == (11.0, "eu-west-1")
    assert book.monthly_cost("eu-west-1", "gp3", 100) == (8.0, "fallback:us-east-1")
    assert book.meta["retrieved"] == "test"


def test_price_table_needs_its_fallback_region() -> None:
    with pytest.raises(ValueError, match="fallback region"):
        PriceBook({"_meta": {"fallback_region": "xx-1"}, "us-east-1": {}})


@pytest.mark.parametrize(
    ("rule", "tags", "expected"),
    [
        ("DoNotDelete", {"DoNotDelete": "true"}, True),
        ("DoNotDelete", {"DoNotDelete": ""}, True),
        ("DoNotDelete", {"DoNotDelete": "false"}, False),
        ("DoNotDelete", {"DoNotDelete": "No"}, False),
        ("DoNotDelete", {"Other": "true"}, False),
        ("Backup=keep", {"Backup": "keep"}, True),
        ("Backup=keep", {"Backup": "daily"}, False),
        ("Backup=", {"Backup": ""}, True),
    ],
)
def test_protect_rule_matching(rule: str, tags: dict[str, str], expected: bool) -> None:
    assert ProtectRule.parse(rule).matches(tags) is expected


def test_protect_rule_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="KEY"):
        ProtectRule.parse("=x")


def test_protect_rule_str_round_trips() -> None:
    assert str(ProtectRule.parse("A")) == "A"
    assert str(ProtectRule.parse("A=b")) == "A=b"
