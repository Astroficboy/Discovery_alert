"""Persistence, and the duplicate protections that depend on it."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.models import Edition, RunRecord
from src.storage.database import Database, image_key


def _edition(issue: int, **kwargs) -> Edition:
    defaults = dict(
        issue_number=issue, edition_date=date.today(), candidate_id=f"cand{issue}",
        title=f"Edition {issue}", category="history", categories=["history"],
        image_url=f"https://archive.test/a/b/Image{issue}.jpg", score=85.0,
        keywords=["a"], entities=["B"], source_urls=["https://s"],
    )
    return Edition(**{**defaults, **kwargs})


def test_issue_numbers_increment_only_for_real_editions(database):
    assert database.next_issue_number(1) == 1
    database.record_edition(_edition(1))
    assert database.next_issue_number(1) == 1, "a draft must not consume an issue number"
    database.mark_edition_sent(1)
    assert database.next_issue_number(1) == 2


def test_image_key_normalises_commons_thumbnails():
    full = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Example.jpg"
    thumb = ("https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/"
             "Example.jpg/1200px-Example.jpg")
    assert image_key(full) == image_key(thumb)
    assert image_key(full + "?width=800") == image_key(full)


def test_used_image_lookup_survives_resizing(database):
    database.record_edition(_edition(1, image_url="https://x/a/ab/Photo.jpg"))
    assert database.has_used_image("https://x/thumb/a/ab/Photo.jpg/640px-Photo.jpg")
    assert not database.has_used_image("https://x/a/ab/Other.jpg")


def test_edition_for_date_prevents_a_second_send(database):
    today = date.today()
    assert not database.edition_exists_for_date(today)
    database.record_edition(_edition(1, edition_date=today))
    database.mark_edition_sent(1)
    assert database.edition_exists_for_date(today)


def test_recent_editions_respect_the_window(database):
    database.record_edition(_edition(1, edition_date=date.today() - timedelta(days=2)))
    database.mark_edition_sent(1)
    database.record_edition(_edition(2, edition_date=date.today() - timedelta(days=90)))
    database.mark_edition_sent(2)
    assert len(database.recent_editions(days=30)) == 1
    assert len(database.recent_editions(days=365)) == 2


def test_run_records_round_trip(database):
    run = RunRecord(execution_id="abc123", mode="dry_run")
    database.start_run(run)
    run.discovered, run.outcome = 42, "skipped"
    run.errors = ["one thing went wrong"]
    run.finished_at = datetime.now(UTC)
    database.finish_run(run)
    stored = database.recent_runs(1)[0]
    assert stored["discovered"] == 42
    assert stored["outcome"] == "skipped"
    assert "one thing went wrong" in stored["errors"]


def test_seen_candidates_remember_rejections(database, candidate):
    database.remember_candidate(candidate, "rejected", "thin sourcing")
    assert database.previously_rejected(candidate.id) == "thin sourcing"
    assert database.previously_rejected("some-other-id") is None


def test_ratings_are_tallied_per_category(database):
    database.record_edition(_edition(1, categories=["music", "history"]))
    database.mark_edition_sent(1)
    assert database.rate_edition(1, "loved")
    assert not database.rate_edition(99, "loved")
    summary = database.ratings_summary()
    assert summary["music"]["loved"] == 1
    assert summary["history"]["loved"] == 1


def test_schema_is_migrated_on_open(tmp_path):
    path = tmp_path / "fresh.db"
    with Database(path) as database:
        assert database.get_meta("schema_version") == "1"
    with Database(path) as database:  # opening again is a no-op
        assert database.next_issue_number(1) == 1


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    with Database(path) as database:
        database.set_meta("schema_version", "99")
    with pytest.raises(RuntimeError, match="newer version"):
        Database(path)


def test_prune_runs_removes_old_rows(database):
    old = RunRecord(execution_id="old",
                    started_at=datetime.now(UTC) - timedelta(days=400))
    database.start_run(old)
    database.start_run(RunRecord(execution_id="new"))
    assert database.prune_runs(365) == 1
    assert [r["execution_id"] for r in database.recent_runs()] == ["new"]
