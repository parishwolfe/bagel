import json

from bagel_market_data.cli import app
from typer.testing import CliRunner


def test_dry_run_and_symbol_override_do_not_connect(monkeypatch):
    monkeypatch.setenv("BAGEL_SYMBOLS", "AAPL")
    result = CliRunner().invoke(
        app,
        [
            "backfill",
            "--dry-run",
            "--symbols",
            "SPY,VXUS",
            "--start",
            "2026-10-20",
            "--end",
            "2026-10-22",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["symbols"] == ["SPY", "VXUS"]
    assert "test-secret" not in result.output


def test_missing_credentials_are_reported_without_values(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in (
        "DATABASE_URL",
        "ALPACA_API_KEY",
        "APCA_API_KEY_ID",
        "ALPACA_SECRET_KEY",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(app, ["check-config"])
    assert result.exit_code == 1
    assert "Operation failed" not in result.output
    assert json.loads(result.stdout)["missing"] == [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "DATABASE_URL",
    ]


def test_applied_migration_runner_refuses_remote():
    import pytest

    from tools.migrate import migrate

    with pytest.raises(ValueError, match="Remote"):
        migrate("postgresql://example:secret@db.example.com/postgres")
