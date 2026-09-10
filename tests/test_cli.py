"""The command-line surface, including the promises the README makes about it."""

from __future__ import annotations

from datetime import date

import pytest

from src.main import EXIT_CONFIG, EXIT_NOTHING_SENT, EXIT_OK, build_parser, main
from src.models import Edition


def test_every_documented_command_parses():
    parser = build_parser()
    for argv in (
        ["run"], ["run", "--dry-run"], ["run", "--review"], ["run", "--force"],
        ["run", "--dry-run", "--offline", "--allow-skip"],
        ["discover", "--limit", "5", "--json"],
        ["preview"], ["preview", "--issue", "3"],
        ["schedule"], ["history", "--runs", "--json"],
        ["approve", "--issue", "2", "--yes"], ["rate", "4", "loved"],
        ["doctor"], ["doctor", "--deep"], ["sources"], ["verify-links"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_a_missing_subcommand_is_an_error():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_bad_config_path_exits_cleanly(capsys):
    code = main(["--config", "/nonexistent/config.yaml", "schedule"])
    assert code == EXIT_CONFIG
    assert "Configuration error" in capsys.readouterr().err


def test_schedule_command_runs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    assert main(["schedule"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "SCHEDULE" in out
    assert "GitHub Actions cron" in out


def test_sources_command_lists_the_registry(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    assert main(["sources"]) == EXIT_OK
    out = capsys.readouterr().out
    for name in ("wikimedia_potd", "nasa_apod", "loc", "met_museum", "wikimedia_music"):
        assert name in out


def test_history_is_empty_but_succeeds(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    assert main(["history"]) == EXIT_OK
    assert "(none yet)" in capsys.readouterr().out


def test_rate_rejects_an_unknown_rating(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    with pytest.raises(SystemExit):
        main(["rate", "1", "magnificent"])


def test_rate_updates_an_edition(monkeypatch, tmp_path, capsys):
    database_path = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    from src.storage.database import Database

    with Database(database_path) as database:
        database.record_edition(Edition(
            issue_number=1, edition_date=date.today(), candidate_id="c", title="T",
            category="history", image_url="https://x/y.jpg",
        ))
    assert main(["rate", "1", "loved"]) == EXIT_OK
    assert "rated 'loved'" in capsys.readouterr().out


def test_preview_without_an_edition_reports_it(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    assert main(["preview"]) != EXIT_OK
    assert "No stored edition" in capsys.readouterr().out


def test_approve_with_nothing_pending(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    assert main(["approve", "--yes"]) != EXIT_OK
    assert "Nothing is awaiting review" in capsys.readouterr().out


def test_allow_skip_controls_the_exit_code(monkeypatch, tmp_path):
    """CI needs to distinguish 'nothing was due' from 'something broke'."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))

    # The stub cannot pass quality control, so this run skips.
    assert main(["run", "--dry-run", "--offline"]) == EXIT_NOTHING_SENT
    assert main(["run", "--dry-run", "--offline", "--allow-skip"]) == EXIT_OK


def test_doctor_reports_configuration_problems(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("EMAIL_PROVIDER", "console")
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == EXIT_CONFIG
    assert "EMAIL_TO is empty" in out


def test_doctor_passes_when_configured(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("EMAIL_PROVIDER", "console")
    monkeypatch.setenv("EMAIL_TO", "reader@example.com")
    monkeypatch.setenv("EMAIL_FROM", "editor@example.com")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    assert main(["doctor"]) == EXIT_OK
    assert "Everything checks out" in capsys.readouterr().out
