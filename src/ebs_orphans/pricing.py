"""Monthly cost estimate for an EBS volume from a versioned price table.

Why a table and not the AWS Price List API: the API needs extra IAM, only has
endpoints in a few regions, returns large nested documents per SKU, and its
results change silently under you. A small JSON file in the repo is reviewable
in a PR, works offline and in tests, and can be overridden with ``--price-file``
(for other regions or negotiated rates). Every cost carries its ``price_source``
so a report never hides that it used a fallback.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

Prices = dict[str, dict[str, Any]]


class PriceBook:
    def __init__(self, table: dict[str, Any]) -> None:
        self.meta: dict[str, Any] = table.get("_meta", {})
        self.fallback_region: str = self.meta.get("fallback_region", "us-east-1")
        self.regions: dict[str, Prices] = {k: v for k, v in table.items() if not k.startswith("_")}
        if self.fallback_region not in self.regions:
            raise ValueError(f"price table has no entry for fallback region {self.fallback_region}")

    @classmethod
    def load(cls, override: Path | None = None) -> PriceBook:
        """Built-in table, with regions/types from ``override`` layered on top."""
        builtin = json.loads(
            resources.files("ebs_orphans").joinpath("data/prices.json").read_text("utf-8")
        )
        if override is not None:
            extra = json.loads(override.read_text("utf-8"))
            for region, types in extra.items():
                if region == "_meta":
                    builtin["_meta"].update(types)
                else:
                    builtin.setdefault(region, {}).update(types)
        return cls(builtin)

    def monthly_cost(
        self,
        region: str,
        volume_type: str,
        size_gib: int,
        iops: int | None = None,
        throughput: int | None = None,
    ) -> tuple[float, str]:
        """Return ``(usd_per_month, price_source)``.

        ``price_source`` is the region whose price was used, ``fallback:<region>``
        when the volume's region is not in the table, or ``unknown`` (cost 0) for a
        volume type the table does not price.
        """
        source = region
        prices = self.regions.get(region, {}).get(volume_type)
        if prices is None:
            prices = self.regions[self.fallback_region].get(volume_type)
            source = f"fallback:{self.fallback_region}"
        if prices is None:
            return 0.0, "unknown"

        cost = size_gib * float(prices["gb_month"])
        if iops:
            if "iops_tiers" in prices:
                cost += _tiered(iops, prices["iops_tiers"])
            elif "iops_month" in prices:
                billable = max(0, iops - int(prices.get("free_iops", 0)))
                cost += billable * float(prices["iops_month"])
        if throughput and "throughput_month" in prices:
            billable = max(0, throughput - int(prices.get("free_throughput", 0)))
            cost += billable * float(prices["throughput_month"])
        return round(cost, 2), source


def _tiered(quantity: int, tiers: list[list[float | None]]) -> float:
    """Price ``quantity`` across ``[[upper_bound_or_null, unit_price], ...]`` tiers."""
    total, lower = 0.0, 0
    for upper, unit in tiers:
        top = quantity if upper is None else min(quantity, int(upper))
        if top > lower:
            total += (top - lower) * float(unit or 0)
        if upper is None or quantity <= upper:
            break
        lower = int(upper)
    return total
