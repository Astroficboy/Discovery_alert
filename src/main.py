"""Command-line interface.

    python -m src.main run --dry-run     # everything except sending
    python -m src.main run --review      # build it, hold it for approval
    python -m src.main run               # build it and send it
    python -m src.main preview           # re-render the last edition locally
    python -m src.main discover          # just the archives
    python -m src.main schedule          # when the next editions fall
    python -m src.main doctor            # is this deployment actually working?

Every command exits 0 on success and non-zero on failure, so a workflow can
tell the difference between "nothing was due" and "something broke".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .logging_setup import Stage, configure_logging, get_logger, new_execution_id, stage

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_NOTHING_SENT = 3

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _rule(char: str = "─", width: int = 66) -> str:
    return char * width


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def cmd_run(config: Config, args: argparse.Namespace) -> int:
    from .pipeline import build_pipeline

    pipeline = build_pipeline(config, dry_run=args.dry_run, review=args.review,
                              force=args.force, offline=args.offline)
    try:
        outcome = await pipeline.run()
    finally:
        pipeline.database.close()

    _print_outcome(config, outcome)

    if outcome.run.outcome in ("sent", "awaiting_review", "dry_run"):
        return EXIT_OK
    if outcome.run.outcome in ("not_due", "skipped"):
        return EXIT_OK if args.allow_skip else EXIT_NOTHING_SENT
    return EXIT_FAILED


async def cmd_discover(config: Config, args: argparse.Namespace) -> int:
    from .discovery import discover_candidates
    from .editorial.scorer import prefilter_score
    from .net import HttpClient

    new_execution_id()
    async with HttpClient(user_agent=config.user_agent) as http:
        candidates = await discover_candidates(config, http)

    if not candidates:
        print("No candidates were discovered.")
        return EXIT_FAILED

    for candidate in candidates:
        candidate.prefilter_score, candidate.prefilter_reasons = prefilter_score(candidate, config)
    candidates.sort(key=lambda c: -c.prefilter_score)

    if args.json:
        print(json.dumps([c.model_dump() for c in candidates[: args.limit]],
                         indent=2, default=str, ensure_ascii=False))
        return EXIT_OK

    print(f"\n{_c('DISCOVERED', BOLD)}  {len(candidates)} candidates\n{_rule()}")
    for candidate in candidates[: args.limit]:
        music = " [music]" if candidate.is_music else ""
        print(f"{candidate.prefilter_score:5.1f}  {candidate.title[:62]:<62}{music}")
        print(f"       {_c(candidate.source + ' · ' + candidate.image.license.name, DIM)}")
    print(_rule())
    return EXIT_OK


async def cmd_preview(config: Config, args: argparse.Namespace) -> int:
    """Re-render a stored edition locally, without touching the network."""
    from .delivery.renderer import Renderer
    from .delivery.sender import write_preview
    from .models import Article, Candidate, ImageAsset, LicenseInfo, MusicMeta
    from .storage.database import Database

    database = Database(config.database_file)
    try:
        edition = (database.get_edition(args.issue) if args.issue
                   else (database.recent_editions(limit=1, statuses=("sent", "pending_review",
                                                                     "draft", "failed")) or [None])[0])
        if edition is None:
            print("No stored edition to preview. Run `python -m src.main run --dry-run` first.")
            return EXIT_FAILED
        payload = database.get_edition_article(edition.issue_number)
        if not payload:
            print(f"Issue #{edition.issue_number} has no stored article body.")
            return EXIT_FAILED

        article = Article.model_validate(payload)
        candidate = Candidate(
            id=edition.candidate_id, source="stored", source_url=edition.image_page_url or "",
            title=edition.title, image=ImageAsset(
                url=edition.image_url, page_url=edition.image_page_url,
                credit=edition.image_credit,
                license=LicenseInfo(id="pd", name="see credit line", reusable=True),
            ),
            categories=edition.categories,
            music=MusicMeta(**edition.music.model_dump()) if edition.music else None,
        )
        rendered = Renderer(config).render(candidate, article,
                                           issue_number=edition.issue_number,
                                           edition_date=edition.edition_date)
    finally:
        database.close()

    path = write_preview(rendered, config.output_dir)
    print(f"\nIssue #{rendered.issue_number}: {rendered.subject}")
    print(f"Preview: {path}")
    return EXIT_OK


async def cmd_schedule(config: Config, args: argparse.Namespace) -> int:
    from .scheduler import Schedule
    from .storage.database import Database

    schedule = Schedule(config)
    database = Database(config.database_file)
    try:
        decision = schedule.decide(database)
        last = database.last_sent_edition()
    finally:
        database.close()

    newsletter = config.newsletter
    print(f"\n{_c('SCHEDULE', BOLD)}\n{_rule()}")
    print(f"Every {newsletter.frequency_days} day(s) at {newsletter.send_time} "
          f"{newsletter.timezone}")
    print(f"Anchored to {newsletter.epoch_date}")
    if last:
        print(f"Last sent: issue #{last.issue_number} on {last.edition_date} — {last.title[:50]}")
    else:
        print("Last sent: never")
    print(f"\nRight now: {'SEND' if decision.should_send else 'nothing to do'} "
          f"— {decision.reason}")
    print(f"\n{_c('Next two weeks', BOLD)}")
    for day, is_send in schedule.describe(days=14):
        print(f"  {day}  {'●  edition' if is_send else '·'}")
    print(f"\nGitHub Actions cron for this configuration:\n  {schedule.cron_hint()}")
    print(_rule())
    return EXIT_OK


async def cmd_history(config: Config, args: argparse.Namespace) -> int:
    from .storage.database import Database

    database = Database(config.database_file)
    try:
        editions = database.all_editions(limit=args.limit)
        runs = database.recent_runs(limit=args.limit) if args.runs else []
        ratings = database.ratings_summary()
    finally:
        database.close()

    if args.json:
        print(json.dumps({"editions": [e.model_dump() for e in editions],
                          "runs": runs, "ratings": ratings},
                         indent=2, default=str, ensure_ascii=False))
        return EXIT_OK

    print(f"\n{_c('EDITIONS', BOLD)}\n{_rule()}")
    if not editions:
        print("  (none yet)")
    for edition in editions:
        mark = {"sent": "✓", "pending_review": "…", "failed": "✗"}.get(edition.status, "·")
        rating = f"  {edition.rating}" if edition.rating else ""
        print(f"  {mark} #{edition.issue_number:03d}  {edition.edition_date}  "
              f"{edition.title[:44]:<44} {edition.score:5.1f}{rating}")
    if runs:
        print(f"\n{_c('RUNS', BOLD)}\n{_rule()}")
        for run in runs:
            print(f"  {run['started_at'][:19]}  {run['outcome']:<14} "
                  f"{(run['selected_title'] or '')[:40]}")
    if ratings:
        print(f"\n{_c('RATINGS BY CATEGORY', BOLD)}\n{_rule()}")
        for category, tally in sorted(ratings.items()):
            print(f"  {category:<16} " + ", ".join(f"{k}×{v}" for k, v in sorted(tally.items())))
    print()
    return EXIT_OK


async def cmd_approve(config: Config, args: argparse.Namespace) -> int:
    """Send an edition that ``run --review`` held back."""
    from .delivery.renderer import Renderer
    from .delivery.sender import send_edition
    from .models import Article, Candidate, ImageAsset, LicenseInfo
    from .storage.database import Database

    database = Database(config.database_file)
    try:
        pending = [e for e in database.all_editions(limit=50) if e.status == "pending_review"]
        edition = (database.get_edition(args.issue) if args.issue
                   else (pending[0] if pending else None))
        if edition is None:
            print("Nothing is awaiting review.")
            return EXIT_FAILED
        if edition.status != "pending_review" and not args.force:
            print(f"Issue #{edition.issue_number} is '{edition.status}', not awaiting review. "
                  "Pass --force to send it anyway.")
            return EXIT_FAILED
        payload = database.get_edition_article(edition.issue_number)
        if not payload:
            print(f"Issue #{edition.issue_number} has no stored article body.")
            return EXIT_FAILED

        article = Article.model_validate(payload)
        candidate = Candidate(
            id=edition.candidate_id, source="stored",
            source_url=edition.image_page_url or "", title=edition.title,
            image=ImageAsset(url=edition.image_url, page_url=edition.image_page_url,
                             credit=edition.image_credit,
                             license=LicenseInfo(id="pd", name="see credit line", reusable=True)),
            categories=edition.categories, music=edition.music,
        )
        rendered = Renderer(config).render(candidate, article,
                                           issue_number=edition.issue_number,
                                           edition_date=edition.edition_date)
        print(f"\nAbout to send issue #{edition.issue_number}: {rendered.subject}")
        print(f"To: {', '.join(config.email.recipients) or '(nobody - EMAIL_TO is empty)'}")
        if not args.yes and sys.stdin.isatty():
            answer = input("Send it? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Not sent.")
                return EXIT_NOTHING_SENT

        result = await send_edition(config, rendered)
        if result.ok:
            database.mark_edition_sent(edition.issue_number)
            print(f"Sent. {result.detail}")
            return EXIT_OK
        database.set_edition_status(edition.issue_number, "failed")
        print(f"Send failed: {result.detail}")
        return EXIT_FAILED
    finally:
        database.close()


async def cmd_rate(config: Config, args: argparse.Namespace) -> int:
    from .storage.database import Database

    valid = {"loved", "good", "okay", "skip"}
    if args.rating not in valid:
        print(f"Rating must be one of: {', '.join(sorted(valid))}")
        return EXIT_CONFIG
    database = Database(config.database_file)
    try:
        if database.rate_edition(args.issue, args.rating):
            print(f"Issue #{args.issue} rated '{args.rating}'.")
            return EXIT_OK
        print(f"No edition #{args.issue}.")
        return EXIT_FAILED
    finally:
        database.close()


async def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    """Check that this deployment can actually do its job."""
    from .delivery.sender import build_provider
    from .discovery import available_sources, build_sources
    from .llm.client import build_llm_client
    from .net import HttpClient
    from .scheduler import Schedule
    from .storage.database import Database

    problems: list[str] = []
    warnings: list[str] = []
    print(f"\n{_c('DOCTOR', BOLD)}\n{_rule()}")

    print(f"  config           config/config.yaml loaded, "
          f"{len(config.content.categories)} categories")

    # Database
    try:
        database = Database(config.database_file)
        count = len(database.all_editions(limit=1000))
        database.close()
        print(f"  database         {config.database_file} ({count} editions)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database: {exc}")
        print(f"  database         {_c('FAIL', BOLD)} {exc}")

    # LLM
    if config.llm_fell_back_to_stub:
        warnings.append("LLM_API_KEY is not set - the stub writer cannot pass quality control")
        print(f"  llm              stub (no LLM_API_KEY) {_c('- will not produce a real edition', DIM)}")
    else:
        try:
            client = build_llm_client(config)
            print(f"  llm              {client.name} / {config.llm.model}")
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"llm: {exc}")
            print(f"  llm              {_c('FAIL', BOLD)} {exc}")

    # Email
    try:
        provider = build_provider(config)
        detail = ""
        if args.deep:
            ok, detail = await provider.verify()
            if not ok:
                problems.append(f"email: {detail}")
        await provider.aclose()
        recipients = ", ".join(config.email.recipients) or "(none)"
        print(f"  email            {provider.name} -> {recipients} {_c(detail, DIM)}")
        if not config.email.recipients:
            problems.append("EMAIL_TO is empty - there is nobody to send to")
        if not config.email.sender:
            problems.append("EMAIL_FROM is empty")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"email: {exc}")
        print(f"  email            {_c('FAIL', BOLD)} {exc}")

    # Sources
    async with HttpClient(user_agent=config.user_agent) as http:
        sources = build_sources(config, http)
        usable = []
        for source in sources:
            try:
                source.check_available()
                usable.append(source.name)
            except Exception as exc:  # noqa: BLE001
                warnings.append(str(exc))
        print(f"  sources          {len(usable)}/{len(sources)} ready "
              f"({len(available_sources())} registered)")
        if len(usable) < 2:
            problems.append("fewer than two discovery sources are usable")

        if args.deep:
            for source in sources[:4]:
                try:
                    found = await source.discover()
                    print(f"    {source.name:<20} {len(found)} candidates")
                except Exception as exc:  # noqa: BLE001
                    print(f"    {source.name:<20} {_c('FAIL', BOLD)} {exc}")

    # Schedule
    schedule = Schedule(config)
    decision = schedule.decide()
    print(f"  schedule         next edition {decision.next_edition_date} "
          f"({config.newsletter.timezone})")

    print(_rule())
    for warning in warnings:
        print(f"  {_c('warning', DIM)}  {warning}")
    if problems:
        for problem in problems:
            print(f"  {_c('PROBLEM', BOLD)}  {problem}")
        print(f"\n{len(problems)} problem(s) found.\n")
        return EXIT_CONFIG
    print("\nEverything checks out.\n")
    return EXIT_OK


async def cmd_sources(config: Config, args: argparse.Namespace) -> int:
    from .discovery import available_sources

    configured = {s.name: s for s in config.discovery.sources}
    print(f"\n{_c('DISCOVERY SOURCES', BOLD)}\n{_rule()}")
    for name, cls in sorted(available_sources().items()):
        entry = configured.get(name)
        state = "enabled" if (entry and entry.enabled) else (
            "disabled" if entry else "not in config")
        key = f" needs {cls.requires_key.upper()}_API_KEY" if cls.requires_key else ""
        music = " · music-focused" if cls.music_focused else ""
        print(f"  {name:<20} {state:<14} authority {cls.authority}{key}{music}")
    print(_rule())
    return EXIT_OK


async def cmd_verify_links(config: Config, args: argparse.Namespace) -> int:
    """Check that the bundled fixture and example images still resolve.

    Both reference Wikimedia Commons files by name, and files get renamed.
    This tells you which references have rotted, rather than finding out when
    an edition ships with a broken hero image.
    """
    import json

    from .config import SourceConfig
    from .discovery.fixtures import FIXTURE_FILE, BundledFixtures
    from .net import FetchError, HttpClient

    targets: list[tuple[str, str]] = []
    source = BundledFixtures(config, SourceConfig(name="fixtures", limit=100,
                                                  options={"path": str(FIXTURE_FILE)}), None)
    targets += [(f"fixture: {c.title}", c.image.url) for c in source.load()]

    for path in sorted((config.repo_root / "examples").glob("issue-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            url = payload["candidate"]["image"]["url"]
        except (OSError, ValueError, KeyError):
            continue
        targets.append((f"example: {path.stem}", url))

    if not targets:
        print("Nothing to check.")
        return EXIT_CONFIG

    async with HttpClient(user_agent=config.user_agent) as http:
        print(f"\n{_c('LINK CHECK', BOLD)}  {len(targets)} references\n{_rule()}")
        failures = 0
        for label, url in targets:
            try:
                response = await http.head_or_get(url)
                content_type = (response.headers.get("Content-Type") or "?").split(";")[0]
                ok = content_type.startswith("image/")
                failures += 0 if ok else 1
                print(f"  {'✓' if ok else '✗'} {label[:52]:<52} {content_type}")
            except FetchError as exc:
                failures += 1
                print(f"  ✗ {label[:52]:<52} {str(exc)[:60]}")
        print(_rule())
        print(f"{len(targets) - failures}/{len(targets)} resolved.\n")
        return EXIT_OK if failures == 0 else EXIT_FAILED


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _print_outcome(config: Config, outcome: Any) -> None:
    run = outcome.run
    print()
    if outcome.candidate and outcome.article:
        candidate = outcome.candidate
        article = outcome.article
        scores = candidate.scores
        print(_rule("═"))
        print(f"{_c('Selected story', BOLD)}")
        print(f"  Title:     {article.title}")
        if article.subtitle:
            print(f"             {_c(article.subtitle, DIM)}")
        print(f"  Category:  {', '.join(candidate.categories) or 'uncategorised'}")
        if candidate.is_music and candidate.music:
            genres = candidate.music.subgenre or candidate.music.genre
            if genres:
                print(f"  Genre:     {', '.join(genres[:4])}")
        if scores:
            print(f"  Score:     {scores.overall:.1f}"
                  f"   (visual {scores.visual_score:.0f} · story {scores.story_score:.0f}"
                  f" · novelty {scores.novelty_score:.0f} · sources {scores.source_quality:.0f})")
        if outcome.quality:
            quality = outcome.quality
            print(f"  Quality:   accuracy {quality.accuracy:.0f} · sourcing "
                  f"{quality.sourcing:.0f} · writing {quality.writing:.0f}"
                  f"{'  ✓ passed' if quality.passed else '  ✗ failed'}")
        print(f"  Words:     {article.word_count}")
        print(f"  Image:     {candidate.image.url[:78]}")
        print(f"             {_c(candidate.image.credit or 'no credit line', DIM)}")
        if article.listen:
            print(f"  Listen:    {article.listen.artist} — {article.listen.track or ''}")
        print("  Sources:")
        for source in article.sources[:6]:
            print(f"    · {source.title[:52]:<52} {_c(source.url[:44], DIM)}")
        print()
    if outcome.preview_path:
        print(f"  Output:    {outcome.preview_path}")
        print(f"             {outcome.preview_path.parent / 'edition.json'}")
    print(f"  Outcome:   {run.outcome}")
    if run.email_status and run.email_status != "not_attempted":
        print(f"  Email:     {run.email_status}")
    if outcome.skipped_reason:
        print(f"  Reason:    {outcome.skipped_reason}")
    usage = run.stages.get("llm_usage") or {}
    if usage:
        print(f"  LLM:       {usage.get('calls', 0)} calls, "
              f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out")
    print(f"  Funnel:    {run.discovered} discovered → {run.after_prefilter} prefiltered "
          f"→ {run.after_triage} triaged → {run.researched} researched")
    for error in run.errors[:5]:
        print(f"  {_c('error', BOLD)}      {error[:150]}")
    print(_rule("═"))
    print()


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="A Curious Thing - an automated picture-and-story newsletter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.yaml (default: config/config.yaml)")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-format", default=None, choices=["text", "json"])

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="the whole pipeline")
    run.add_argument("--dry-run", action="store_true",
                     help="do everything except send; writes output/newsletter.html")
    run.add_argument("--review", action="store_true",
                     help="build the edition and hold it for approval")
    run.add_argument("--force", action="store_true",
                     help="ignore the schedule and run anyway")
    run.add_argument("--offline", action="store_true",
                     help="use the bundled candidates and bundled research; no archive "
                          "calls. Combine with LLM_PROVIDER=stub for a fully offline run")
    run.add_argument("--allow-skip", action="store_true",
                     help="exit 0 when nothing was due or nothing met the bar "
                          "(use this in CI)")
    run.set_defaults(func=cmd_run)

    discover = sub.add_parser("discover", help="query the archives and stop")
    discover.add_argument("--limit", type=int, default=30)
    discover.add_argument("--json", action="store_true")
    discover.set_defaults(func=cmd_discover)

    preview = sub.add_parser("preview", help="re-render a stored edition to output/")
    preview.add_argument("--issue", type=int, default=None)
    preview.set_defaults(func=cmd_preview)

    schedule = sub.add_parser("schedule", help="show when editions fall")
    schedule.set_defaults(func=cmd_schedule)

    history = sub.add_parser("history", help="past editions, runs and ratings")
    history.add_argument("--limit", type=int, default=25)
    history.add_argument("--runs", action="store_true")
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_history)

    approve = sub.add_parser("approve", help="send an edition held by --review")
    approve.add_argument("--issue", type=int, default=None)
    approve.add_argument("--yes", "-y", action="store_true", help="do not prompt")
    approve.add_argument("--force", action="store_true")
    approve.set_defaults(func=cmd_approve)

    rate = sub.add_parser("rate", help="rate an edition (feeds future personalisation)")
    rate.add_argument("issue", type=int)
    rate.add_argument("rating", choices=["loved", "good", "okay", "skip"])
    rate.set_defaults(func=cmd_rate)

    doctor = sub.add_parser("doctor", help="check this deployment")
    doctor.add_argument("--deep", action="store_true",
                        help="also hit the network: verify email credentials and sources")
    doctor.set_defaults(func=cmd_doctor)

    sources = sub.add_parser("sources", help="list discovery sources")
    sources.set_defaults(func=cmd_sources)

    verify = sub.add_parser("verify-links", help="check the bundled fixture images resolve")
    verify.set_defaults(func=cmd_verify_links)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ValueError as exc:
        configure_logging("ERROR", "text")
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(
        args.log_level or config.logging.level,
        args.log_format or config.logging.format,
    )
    with stage(Stage.CONFIG):
        logger.debug("configuration loaded", provider=config.llm.provider,
                     email=config.email.provider, timezone=config.newsletter.timezone)

    try:
        return asyncio.run(args.func(config, args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line
        logger.error("unhandled error", error=f"{type(exc).__name__}: {exc}", exc_info=True)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
