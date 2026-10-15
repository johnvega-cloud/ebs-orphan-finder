from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ebs_orphans import cli
from ebs_orphans.models import OrphanVolume, ScanError, ScanResult
from ebs_orphans.report import render_markdown, to_json, write_csv
from tests.conftest import REGION, Org

NOW = datetime(2026, 10, 15, tzinfo=UTC)


def _volume(vid: str, cost: float, **kwargs: object) -> OrphanVolume:
    params: dict[str, object] = {
        "account_id": "111111111111",
        "account_name": "prod",
        "region": REGION,
        "volume_id": vid,
        "volume_type": "gp3",
        "size_gib": 100,
        "iops": 3000,
        "throughput": 125,
        "create_time": datetime(2026, 1, 1, tzinfo=UTC),
        "age_days": 287,
        "availability_zone": "us-east-1a",
        "encrypted": True,
        "snapshot_id": "",
        "tags": {"Name": "old-data"},
        "monthly_cost_usd": cost,
        "price_source": REGION,
    }
    params.update(kwargs)
    return OrphanVolume(**params)  # type: ignore[arg-type]


def _result() -> ScanResult:
    result = ScanResult(generated_at=NOW, min_age_days=30, accounts_scanned=2, regions_scanned=4)
    result.volumes = [
        _volume("vol-a", 8.0),
        _volume(
            "vol-b",
            2.0,
            region="ap-southeast-7",
            price_source="fallback:us-east-1",
            protected_by="DoNotDelete",
        ),
        _volume("vol-c", 0.0, volume_type="gp9", price_source="unknown"),
    ]
    result.errors = [ScanError("222222222222", "*", "AccessDenied", "role | missing")]
    return result


def test_json_summary() -> None:
    data = to_json(_result())
    assert data["summary"]["volumes"] == 3
    assert data["summary"]["monthly_cost_usd"] == 10.0
    assert data["summary"]["annual_cost_usd"] == 120.0
    assert data["volumes"][0]["create_time"] == "2026-01-01T00:00:00+00:00"
    assert data["errors"][0]["code"] == "AccessDenied"


def test_csv_has_one_row_per_volume(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    write_csv(_result(), path)
    rows = list(csv.DictReader(path.open()))
    assert [r["volume_id"] for r in rows] == ["vol-a", "vol-b", "vol-c"]
    assert rows[0]["name"] == "old-data"
    assert rows[1]["protected_by"] == "DoNotDelete"


def test_markdown_flags_estimates_and_errors() -> None:
    md = render_markdown(_result())
    assert "**$10.00 / month**" in md
    assert "3 (1 protected by tag)" in md
    assert "ap-southeast-7 are not in the price table" in md
    assert "No price for volume type(s) gp9" in md
    assert "## Scan errors" in md
    assert "role / missing" in md  # pipes escaped so the table does not break
    assert "| 111111111111 prod | 3 | 300 | $10.00 |" in md


# ---------- CLI, end to end against moto ----------


def test_cli_scan_org_writes_reports_and_plan(org: Org, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    org.volume(org.members[0], Size=10, VolumeType="gp2")
    code = cli.main(
        [
            "scan",
            "--min-age-days",
            "0",
            "--regions",
            REGION,
            "--output-dir",
            str(tmp_path),
            "--plan",
            "-q",
        ]
    )
    assert code == cli.EXIT_OK
    files = {p.suffix for p in tmp_path.iterdir()}
    assert files == {".csv", ".json", ".md"}
    plan_file = next(tmp_path.glob("*.plan.json"))
    plan = json.loads(plan_file.read_text())
    assert plan["summary"]["volumes"] == 1
    assert plan["snapshot_before_delete"] is True
    assert "$1.00 / month" in capsys.readouterr().out

    # Dry-run apply through the CLI.
    code = cli.main(["apply", str(plan_file), "-q"])
    assert code == cli.EXIT_OK
    results = json.loads(plan_file.with_suffix(".results.json").read_text())
    assert results["executed"] is False
    assert results["outcomes"][0]["action"] == "would-delete"

    # Execute with --yes, then again: idempotent.
    assert cli.main(["apply", str(plan_file), "--execute", "--yes", "-q"]) == cli.EXIT_OK
    assert "EXECUTED: deleted=1" in capsys.readouterr().out
    assert cli.main(["apply", str(plan_file), "--execute", "--yes", "-q"]) == cli.EXIT_OK
    assert "already-gone=1" in capsys.readouterr().out


def test_cli_current_account_only(org: Org, tmp_path: Path) -> None:
    org.volume(org.management)
    org.volume(org.members[0])
    code = cli.main(
        [
            "scan",
            "--current-account-only",
            "--min-age-days",
            "0",
            "--regions",
            REGION,
            "--formats",
            "json",
            "--output-dir",
            str(tmp_path),
            "-q",
        ]
    )
    assert code == cli.EXIT_OK
    data = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert data["summary"]["accounts_scanned"] == 1
    assert {v["account_id"] for v in data["volumes"]} == {org.management}


def test_cli_explicit_accounts_and_exclusions(org: Org, tmp_path: Path) -> None:
    org.volume(org.members[0])
    org.volume(org.members[1])
    code = cli.main(
        [
            "scan",
            "--accounts",
            ",".join(org.members),
            "--exclude-accounts",
            org.members[1],
            "--min-age-days",
            "0",
            "--regions",
            REGION,
            "--formats",
            "json",
            "--output-dir",
            str(tmp_path),
            "--log-format",
            "json",
        ]
    )
    assert code == cli.EXIT_OK
    data = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert {v["account_id"] for v in data["volumes"]} == {org.members[0]}


def test_cli_partial_exit_code_when_an_account_fails(
    org: Org, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs_orphans import discovery

    def boom(_session):  # type: ignore[no-untyped-def]
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeRegions")

    monkeypatch.setattr(discovery, "enabled_regions", boom)
    code = cli.main(
        ["scan", "--current-account-only", "--output-dir", str(tmp_path), "--formats", "md", "-q"]
    )
    assert code == cli.EXIT_PARTIAL


@pytest.mark.parametrize(
    "args",
    [
        ["scan", "--min-age-days", "-1"],
        ["scan", "--formats", "pdf"],
        ["scan", "--accounts", "999999999999", "--exclude-accounts", "999999999999"],
    ],
)
def test_cli_usage_errors(org: Org, tmp_path: Path, args: list[str]) -> None:
    assert cli.main([*args, "--output-dir", str(tmp_path), "-q"]) == cli.EXIT_USAGE


def test_cli_bad_protect_tag_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit):
        cli.main(["scan", "--protect-tag", "=x"])


def test_cli_apply_rejects_bad_files(org: Org, tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert cli.main(["apply", str(missing), "-q"]) == cli.EXIT_USAGE
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"kind": "x"}')
    assert cli.main(["apply", str(foreign), "-q"]) == cli.EXIT_USAGE


def test_cli_execute_needs_confirmation(
    org: Org, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org.volume(org.management)
    cli.main(
        [
            "scan",
            "--current-account-only",
            "--min-age-days",
            "0",
            "--regions",
            REGION,
            "--output-dir",
            str(tmp_path),
            "--plan",
            "-q",
        ]
    )
    plan_file = next(tmp_path.glob("*.plan.json"))
    plan_id = json.loads(plan_file.read_text())["plan_id"]

    # Not a terminal and no --yes: refuse.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["apply", str(plan_file), "--execute", "-q"]) == cli.EXIT_USAGE

    # Terminal, wrong answer: refuse. Right answer: go.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong")
    assert cli.main(["apply", str(plan_file), "--execute", "-q"]) == cli.EXIT_USAGE
    monkeypatch.setattr("builtins.input", lambda _prompt: plan_id)
    assert cli.main(["apply", str(plan_file), "--execute", "-q"]) == cli.EXIT_OK


def test_cli_no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from botocore.exceptions import NoCredentialsError

    def no_creds(*_: object, **__: object) -> None:
        raise NoCredentialsError()

    monkeypatch.setattr(cli, "SessionFactory", no_creds)
    assert cli.main(["scan", "--output-dir", str(tmp_path), "-q"]) == cli.EXIT_FATAL


def test_cli_api_error_before_scan_is_fatal(
    org: Org, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from botocore.exceptions import ClientError

    def denied(*_: object, **__: object) -> None:
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListAccounts")

    monkeypatch.setattr(cli, "list_org_accounts", denied)
    assert cli.main(["scan", "--output-dir", str(tmp_path), "-q"]) == cli.EXIT_FATAL
