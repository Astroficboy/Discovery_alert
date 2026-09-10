"""When to send.

The design principle: **the schedule is a pure function of the calendar and
the database, not of process state.** Nothing is held in memory between runs,
there is no long-lived daemon, and no timer needs to survive anything.

    send day  ==  (edition_date - epoch_date) % frequency_days == 0

GitHub Actions runs the pipeline daily; the pipeline decides whether today is
one of its days. That arrangement is robust to the two things that actually
happen: Actions' cron firing late (or not at all, under load), and a run
being triggered twice.

Duplicate protection is separate and stronger than the interval check: an
edition row exists per ``edition_date``, so a second run on the same reader-day
finds it and declines, whatever the interval arithmetic says.

Everything is computed in the reader's timezone. If you move, change
``TIMEZONE`` and the rhythm follows you.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..logging_setup import Stage, get_logger, stage
from ..storage.database import Database

logger = get_logger(__name__)


@dataclass
class ScheduleDecision:
    should_send: bool
    reason: str
    edition_date: date
    next_edition_date: date
    is_send_day: bool
    already_sent: bool
    before_send_time: bool


@dataclass
class Schedule:
    config: Config

    @property
    def tz(self) -> ZoneInfo:
        return self.config.newsletter.tzinfo

    def now(self, at: datetime | None = None) -> datetime:
        if at is None:
            return datetime.now(self.tz)
        return at.astimezone(self.tz) if at.tzinfo else at.replace(tzinfo=self.tz)

    # ------------------------------------------------------------------ #
    def epoch(self) -> date:
        return date.fromisoformat(self.config.newsletter.epoch_date)

    def is_send_day(self, day: date) -> bool:
        frequency = self.config.newsletter.frequency_days
        if frequency <= 1:
            return True
        return (day - self.epoch()).days % frequency == 0

    def next_send_day(self, after: date) -> date:
        frequency = self.config.newsletter.frequency_days
        day = after + timedelta(days=1)
        for _ in range(frequency * 2 + 2):
            if self.is_send_day(day):
                return day
            day += timedelta(days=1)
        return after + timedelta(days=frequency)  # pragma: no cover - unreachable

    def send_time_today(self, day: date) -> datetime:
        newsletter = self.config.newsletter
        return datetime(day.year, day.month, day.day,
                        newsletter.send_hour, newsletter.send_minute, tzinfo=self.tz)

    # ------------------------------------------------------------------ #
    def decide(self, database: Database | None = None, *, at: datetime | None = None,
               force: bool = False) -> ScheduleDecision:
        """Should this run produce and send an edition?"""
        with stage(Stage.SCHEDULE):
            now = self.now(at)
            today = now.date()
            is_send_day = self.is_send_day(today)
            already_sent = bool(database and database.edition_exists_for_date(today))
            before_time = now < self.send_time_today(today)
            next_day = today if (is_send_day and not already_sent) else self.next_send_day(today)

            decision = ScheduleDecision(
                should_send=False, reason="", edition_date=today,
                next_edition_date=next_day, is_send_day=is_send_day,
                already_sent=already_sent, before_send_time=before_time,
            )

            if force:
                decision.should_send = True
                decision.reason = "forced (--force)"
            elif already_sent:
                decision.reason = (f"an edition already exists for {today.isoformat()}; "
                                   f"next is {next_day.isoformat()}")
            elif not is_send_day:
                decision.reason = (f"{today.isoformat()} is not a send day "
                                   f"(every {self.config.newsletter.frequency_days} days "
                                   f"from {self.epoch().isoformat()}); "
                                   f"next is {next_day.isoformat()}")
            elif before_time:
                decision.reason = (f"too early: send time is "
                                   f"{self.config.newsletter.send_time} "
                                   f"{self.config.newsletter.timezone}, it is "
                                   f"{now.strftime('%H:%M')}")
            else:
                decision.should_send = True
                decision.reason = f"scheduled edition for {today.isoformat()}"

            logger.info("schedule decision", should_send=decision.should_send,
                        reason=decision.reason,
                        timezone=self.config.newsletter.timezone)
            return decision

    def describe(self, database: Database | None = None,
                 *, days: int = 14) -> list[tuple[date, bool]]:
        """The next fortnight, for ``main.py schedule``."""
        today = self.now().date()
        return [
            (today + timedelta(days=offset), self.is_send_day(today + timedelta(days=offset)))
            for offset in range(days)
        ]

    def cron_hint(self) -> str:
        """The GitHub Actions cron line that matches this configuration.

        Actions crons are UTC only and have no notion of "every other day", so
        the workflow runs daily an hour before the reader's send time and lets
        :meth:`decide` do the rest.
        """
        newsletter = self.config.newsletter
        sample = self.send_time_today(date.today())
        utc = sample.astimezone(ZoneInfo("UTC"))
        return (f"{utc.minute} {utc.hour} * * *  "
                f"# {newsletter.send_time} {newsletter.timezone}")


__all__ = ["Schedule", "ScheduleDecision"]
