"""SQLite persistence.

Why SQLite and not a hosted database: this is a personal newsletter that runs
once every two days and stores a few hundred rows a year. A single file that
GitHub Actions commits back to the repository is the smallest thing that
works, needs no account, no network and no secret, and gives you the entire
edition history in ``git log``. See the README for the trade-offs and for
when to graduate to Postgres.

The schema is created and migrated on open, so there is no separate migration
step to forget.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import Stage, get_logger, stage
from ..models import Candidate, Edition, MusicMeta, RunRecord

logger = get_logger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number      INTEGER NOT NULL UNIQUE,
    edition_date      TEXT    NOT NULL,
    candidate_id      TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    category          TEXT    NOT NULL,
    categories        TEXT    NOT NULL DEFAULT '[]',
    image_url         TEXT    NOT NULL,
    image_page_url    TEXT,
    image_credit      TEXT,
    image_key         TEXT    NOT NULL DEFAULT '',
    score             REAL    NOT NULL DEFAULT 0,
    music             TEXT,
    keywords          TEXT    NOT NULL DEFAULT '[]',
    entities          TEXT    NOT NULL DEFAULT '[]',
    source_urls       TEXT    NOT NULL DEFAULT '[]',
    article           TEXT,
    html_path         TEXT,
    status            TEXT    NOT NULL DEFAULT 'draft',
    rating            TEXT,
    created_at        TEXT    NOT NULL,
    sent_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_editions_date     ON editions(edition_date);
CREATE INDEX IF NOT EXISTS idx_editions_status   ON editions(status);
CREATE INDEX IF NOT EXISTS idx_editions_candidate ON editions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_editions_imagekey ON editions(image_key);

CREATE TABLE IF NOT EXISTS runs (
    execution_id      TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    mode              TEXT NOT NULL DEFAULT 'run',
    discovered        INTEGER NOT NULL DEFAULT 0,
    after_prefilter   INTEGER NOT NULL DEFAULT 0,
    after_triage      INTEGER NOT NULL DEFAULT 0,
    researched        INTEGER NOT NULL DEFAULT 0,
    selected_candidate_id TEXT,
    selected_title    TEXT,
    selected_score    REAL,
    issue_number      INTEGER,
    email_status      TEXT NOT NULL DEFAULT 'not_attempted',
    outcome           TEXT NOT NULL DEFAULT 'unknown',
    errors            TEXT NOT NULL DEFAULT '[]',
    stages            TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- Candidates we looked at and rejected. Keeps us from re-researching the same
-- near-miss every other day, and is useful when tuning the scorer.
CREATE TABLE IF NOT EXISTS seen_candidates (
    candidate_id TEXT PRIMARY KEY,
    image_key    TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    source       TEXT NOT NULL,
    score        REAL NOT NULL DEFAULT 0,
    outcome      TEXT NOT NULL,
    reason       TEXT,
    last_seen    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_imagekey ON seen_candidates(image_key);
CREATE INDEX IF NOT EXISTS idx_seen_lastseen ON seen_candidates(last_seen);
"""


def image_key(url: str) -> str:
    """Normalised image identity: the path, without the size query string.

    Commons and most archives serve the same photograph at many widths; the
    query string is not part of what the reader has already seen.
    """
    cleaned = url.split("?")[0].split("#")[0].lower().rstrip("/")
    # Commons thumbnails look like .../thumb/a/ab/File.jpg/1200px-File.jpg
    if "/thumb/" in cleaned:
        cleaned = cleaned.split("/thumb/", 1)[1]
        cleaned = "/".join(cleaned.split("/")[:-1])
    return cleaned.rsplit("/", 1)[-1] or cleaned


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Database:
    """A thin, explicit data-access layer. No ORM, no magic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # -- lifecycle ------------------------------------------------------ #
    def _migrate(self) -> None:
        with stage(Stage.STORAGE):
            self.connection.executescript(SCHEMA)
            current = self.get_meta("schema_version")
            if current is None:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
            elif int(current) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database at {self.path} was written by a newer version "
                    f"(schema {current} > {SCHEMA_VERSION}); upgrade the code"
                )

    def close(self) -> None:
        """Checkpoint and close.

        The WAL is folded back into the main file first, so that the single
        ``.db`` the workflow commits is complete on its own - the ``-wal`` and
        ``-shm`` sidecars are transient and deliberately not committed.
        """
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:  # pragma: no cover - closing must not raise
            pass
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    # -- meta ----------------------------------------------------------- #
    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- editions ------------------------------------------------------- #
    def next_issue_number(self, first: int = 1) -> int:
        row = self.connection.execute(
            "SELECT MAX(issue_number) AS n FROM editions WHERE status IN ('sent','pending_review')"
        ).fetchone()
        highest = row["n"] if row and row["n"] is not None else None
        return first if highest is None else highest + 1

    def record_edition(self, edition: Edition, *, article_json: str | None = None) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO editions (
                    issue_number, edition_date, candidate_id, title, category, categories,
                    image_url, image_page_url, image_credit, image_key, score, music,
                    keywords, entities, source_urls, article, html_path, status,
                    created_at, sent_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(issue_number) DO UPDATE SET
                    status = excluded.status,
                    sent_at = excluded.sent_at,
                    html_path = excluded.html_path,
                    article = COALESCE(excluded.article, editions.article)
                """,
                (
                    edition.issue_number,
                    edition.edition_date.isoformat(),
                    edition.candidate_id,
                    edition.title,
                    edition.category,
                    _json(edition.categories),
                    edition.image_url,
                    edition.image_page_url,
                    edition.image_credit,
                    image_key(edition.image_url),
                    edition.score,
                    _json(edition.music.model_dump()) if edition.music else None,
                    _json(edition.keywords),
                    _json(edition.entities),
                    _json(edition.source_urls),
                    article_json,
                    edition.html_path,
                    edition.status,
                    datetime.now(UTC).isoformat(),
                    edition.sent_at.isoformat() if edition.sent_at else None,
                ),
            )
            return int(cursor.lastrowid or 0)

    def mark_edition_sent(self, issue_number: int, *, when: datetime | None = None) -> None:
        self.connection.execute(
            "UPDATE editions SET status = 'sent', sent_at = ? WHERE issue_number = ?",
            ((when or datetime.now(UTC)).isoformat(), issue_number),
        )

    def set_edition_status(self, issue_number: int, status: str) -> None:
        self.connection.execute(
            "UPDATE editions SET status = ? WHERE issue_number = ?", (status, issue_number)
        )

    def rate_edition(self, issue_number: int, rating: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE editions SET rating = ? WHERE issue_number = ?", (rating, issue_number)
        )
        return cursor.rowcount > 0

    def recent_editions(self, days: int | None = None, *, limit: int = 200,
                        statuses: tuple[str, ...] = ("sent", "pending_review")) -> list[Edition]:
        placeholders = ",".join("?" for _ in statuses)
        query = f"SELECT * FROM editions WHERE status IN ({placeholders})"  # noqa: S608
        params: list[Any] = list(statuses)
        if days is not None:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            query += " AND edition_date >= ?"
            params.append(cutoff)
        query += " ORDER BY edition_date DESC, issue_number DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._row_to_edition(row) for row in rows]

    def all_editions(self, *, limit: int = 500) -> list[Edition]:
        rows = self.connection.execute(
            "SELECT * FROM editions ORDER BY issue_number DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_edition(row) for row in rows]

    def get_edition(self, issue_number: int) -> Edition | None:
        row = self.connection.execute(
            "SELECT * FROM editions WHERE issue_number = ?", (issue_number,)
        ).fetchone()
        return self._row_to_edition(row) if row else None

    def get_edition_article(self, issue_number: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT article FROM editions WHERE issue_number = ?", (issue_number,)
        ).fetchone()
        return _loads(row["article"], None) if row else None

    def last_sent_edition(self) -> Edition | None:
        row = self.connection.execute(
            "SELECT * FROM editions WHERE status = 'sent' "
            "ORDER BY edition_date DESC, issue_number DESC LIMIT 1"
        ).fetchone()
        return self._row_to_edition(row) if row else None

    def edition_exists_for_date(self, when: date,
                                statuses: tuple[str, ...] = ("sent", "pending_review")) -> bool:
        placeholders = ",".join("?" for _ in statuses)
        row = self.connection.execute(
            f"SELECT 1 FROM editions WHERE edition_date = ? AND status IN ({placeholders}) LIMIT 1",  # noqa: S608
            (when.isoformat(), *statuses),
        ).fetchone()
        return row is not None

    def has_used_image(self, url: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM editions WHERE image_key = ? LIMIT 1", (image_key(url),)
        ).fetchone()
        return row is not None

    def has_used_candidate(self, candidate_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM editions WHERE candidate_id = ? LIMIT 1", (candidate_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_edition(row: sqlite3.Row) -> Edition:
        music_payload = _loads(row["music"], None)
        return Edition(
            issue_number=row["issue_number"],
            edition_date=date.fromisoformat(row["edition_date"]),
            candidate_id=row["candidate_id"],
            title=row["title"],
            category=row["category"],
            categories=_loads(row["categories"], []),
            image_url=row["image_url"],
            image_page_url=row["image_page_url"],
            image_credit=row["image_credit"],
            score=row["score"],
            music=MusicMeta(**music_payload) if music_payload else None,
            keywords=_loads(row["keywords"], []),
            entities=_loads(row["entities"], []),
            source_urls=_loads(row["source_urls"], []),
            html_path=row["html_path"],
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
            status=row["status"],
            rating=row["rating"],
        )

    # -- seen candidates ------------------------------------------------ #
    def remember_candidate(self, candidate: Candidate, outcome: str,
                           reason: str | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO seen_candidates
                (candidate_id, image_key, title, source, score, outcome, reason, last_seen)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                score = excluded.score, outcome = excluded.outcome,
                reason = excluded.reason, last_seen = excluded.last_seen
            """,
            (
                candidate.id, image_key(candidate.image.url), candidate.title[:300],
                candidate.source, candidate.overall, outcome, reason,
                datetime.now(UTC).isoformat(),
            ),
        )

    def previously_rejected(self, candidate_id: str, *, within_days: int = 45) -> str | None:
        cutoff = (datetime.now(UTC) - timedelta(days=within_days)).isoformat()
        row = self.connection.execute(
            "SELECT reason FROM seen_candidates WHERE candidate_id = ? "
            "AND outcome = 'rejected' AND last_seen >= ?",
            (candidate_id, cutoff),
        ).fetchone()
        return row["reason"] if row else None

    # -- runs ----------------------------------------------------------- #
    def start_run(self, run: RunRecord) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO runs (execution_id, started_at, mode) VALUES (?,?,?)",
            (run.execution_id, run.started_at.isoformat(), run.mode),
        )

    def finish_run(self, run: RunRecord) -> None:
        self.connection.execute(
            """
            UPDATE runs SET
                finished_at = ?, discovered = ?, after_prefilter = ?, after_triage = ?,
                researched = ?, selected_candidate_id = ?, selected_title = ?,
                selected_score = ?, issue_number = ?, email_status = ?, outcome = ?,
                errors = ?, stages = ?
            WHERE execution_id = ?
            """,
            (
                (run.finished_at or datetime.now(UTC)).isoformat(),
                run.discovered, run.after_prefilter, run.after_triage, run.researched,
                run.selected_candidate_id, run.selected_title, run.selected_score,
                run.issue_number, run.email_status, run.outcome,
                _json(run.errors), _json(run.stages), run.execution_id,
            ),
        )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_runs(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        cursor = self.connection.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        return cursor.rowcount

    # -- stats ---------------------------------------------------------- #
    def category_counts(self, days: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edition in self.recent_editions(days=days):
            for category in edition.categories or [edition.category]:
                counts[category] = counts.get(category, 0) + 1
        return counts

    def ratings_summary(self) -> dict[str, dict[str, int]]:
        """Per-category rating tallies - the raw material for the personalisation
        engine described in the README's roadmap."""
        summary: dict[str, dict[str, int]] = {}
        rows = self.connection.execute(
            "SELECT categories, rating FROM editions WHERE rating IS NOT NULL"
        ).fetchall()
        for row in rows:
            for category in _loads(row["categories"], []):
                bucket = summary.setdefault(category, {})
                bucket[row["rating"]] = bucket.get(row["rating"], 0) + 1
        return summary


def connect(path: str | Path) -> Database:
    return Database(path)


__all__ = ["Database", "SCHEMA_VERSION", "connect", "image_key"]
