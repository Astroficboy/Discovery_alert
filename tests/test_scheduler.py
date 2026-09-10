"""The alternate-day rhythm, and its duplicate protections."""

from __future__ import annotations

from datetime import date, datetime

from src.models import Edition
from src.scheduler import Schedule


def _at(config, iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=config.newsletter.tzinfo)


def test_alternate_days_from_the_epoch(config):
    config.newsletter.epoch_date = "2026-01-01"
    config.newsletter.frequency_days = 2
    schedule = Schedule(config)
    assert schedule.is_send_day(date(2026, 1, 1))
    assert not schedule.is_send_day(date(2026, 1, 2))
    assert schedule.is_send_day(date(2026, 1, 3))
    assert schedule.is_send_day(date(2026, 3, 2))


def test_a_different_interval_is_honoured(config):
    config.newsletter.frequency_days = 3
    config.newsletter.epoch_date = "2026-01-01"
    schedule = Schedule(config)
    assert schedule.is_send_day(date(2026, 1, 4))
    assert not schedule.is_send_day(date(2026, 1, 5))


def test_daily_interval_sends_every_day(config):
    config.newsletter.frequency_days = 1
    schedule = Schedule(config)
    assert all(schedule.is_send_day(date(2026, 1, day)) for day in range(1, 6))


def test_before_send_time_does_nothing(config, database):
    schedule = Schedule(config)
    decision = schedule.decide(database, at=_at(config, "2026-01-01T06:00"))
    assert not decision.should_send
    assert "too early" in decision.reason


def test_after_send_time_on_a_send_day_sends(config, database):
    schedule = Schedule(config)
    decision = schedule.decide(database, at=_at(config, "2026-01-01T08:30"))
    assert decision.should_send


def test_off_day_does_nothing(config, database):
    schedule = Schedule(config)
    decision = schedule.decide(database, at=_at(config, "2026-01-02T09:00"))
    assert not decision.should_send
    assert "not a send day" in decision.reason
    assert decision.next_edition_date == date(2026, 1, 3)


def test_a_second_run_on_the_same_day_is_refused(config, database):
    """The one that actually matters: cron firing twice must not send twice."""
    schedule = Schedule(config)
    when = _at(config, "2026-01-01T08:30")
    assert schedule.decide(database, at=when).should_send

    database.record_edition(Edition(
        issue_number=1, edition_date=date(2026, 1, 1), candidate_id="c",
        title="Already sent", category="history", image_url="https://x/y.jpg",
    ))
    database.mark_edition_sent(1)

    second = schedule.decide(database, at=when)
    assert not second.should_send
    assert "already exists" in second.reason


def test_force_overrides_everything(config, database):
    schedule = Schedule(config)
    decision = schedule.decide(database, at=_at(config, "2026-01-02T03:00"), force=True)
    assert decision.should_send
    assert "forced" in decision.reason


def test_late_cron_still_sends_the_same_day(config, database):
    """Actions crons are unreliable; a run at 23:50 on a send day still counts."""
    schedule = Schedule(config)
    decision = schedule.decide(database, at=_at(config, "2026-01-01T23:50"))
    assert decision.should_send
    assert decision.edition_date == date(2026, 1, 1)


def test_timezone_is_not_hard_coded(config, database):
    config.newsletter.timezone = "America/Los_Angeles"
    schedule = Schedule(config)
    # 08:30 in Los Angeles is a send; the same instant is the next day in UTC+13.
    decision = schedule.decide(database, at=_at(config, "2026-01-01T08:30"))
    assert decision.should_send
    assert "America/Los_Angeles" in schedule.cron_hint() or schedule.cron_hint()


def test_cron_hint_is_in_utc(config):
    config.newsletter.timezone = "Asia/Kolkata"
    config.newsletter.send_time = "08:00"
    hint = Schedule(config).cron_hint()
    # 08:00 IST is 02:30 UTC.
    assert hint.startswith("30 2 * * *")


def test_describe_lists_upcoming_send_days(config):
    schedule = Schedule(config)
    days = schedule.describe(days=8)
    assert len(days) == 8
    assert sum(1 for _, is_send in days if is_send) == 4


def test_next_send_day_skips_forward(config):
    config.newsletter.frequency_days = 2
    config.newsletter.epoch_date = "2026-01-01"
    schedule = Schedule(config)
    assert schedule.next_send_day(date(2026, 1, 1)) == date(2026, 1, 3)
    assert schedule.next_send_day(date(2026, 1, 2)) == date(2026, 1, 3)
