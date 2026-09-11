"""Configuration: a YAML file for editorial policy, environment for secrets.

Two rules keep this honest:

* Anything that shapes the newsletter lives in ``config/config.yaml`` and is
  validated by pydantic, so a typo fails at startup rather than at 08:00.
* Anything secret lives in the environment and is never written back out.

A handful of settings appear in both places (timezone, interval, providers).
Environment wins, because that is what GitHub Actions can set.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------- #
# Editorial configuration (config.yaml)
# --------------------------------------------------------------------------- #
class NewsletterConfig(_Base):
    name: str = "A Curious Thing"
    tagline: str = ""
    frequency_days: int = Field(default=2, ge=1, le=30)
    send_time: str = "08:00"
    timezone: str = "UTC"
    epoch_date: str = "2026-01-01"
    first_issue_number: int = Field(default=1, ge=1)
    sign_off: str = "Next edition in {days} days."

    @field_validator("send_time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            hour, minute = (int(p) for p in v.split(":"))
        except ValueError as exc:  # pragma: no cover - message clarity only
            raise ValueError(f"send_time must look like 'HH:MM', got {v!r}") from exc
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"send_time out of range: {v!r}")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {v!r}") from exc
        return v

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def send_hour(self) -> int:
        return int(self.send_time.split(":")[0])

    @property
    def send_minute(self) -> int:
        return int(self.send_time.split(":")[1])


class WordCountConfig(_Base):
    min: int = 500
    max: int = 900
    hard_min: int = 380
    hard_max: int = 1150

    @model_validator(mode="after")
    def _ordered(self) -> WordCountConfig:
        if not self.hard_min <= self.min <= self.max <= self.hard_max:
            raise ValueError("word counts must satisfy hard_min <= min <= max <= hard_max")
        return self


class SafetyConfig(_Base):
    avoid_graphic_imagery: bool = True
    require_content_note_for: list[str] = Field(default_factory=list)
    banned_terms: list[str] = Field(default_factory=list)


class ContentConfig(_Base):
    min_quality_score: float = 78.0
    min_prefilter_score: float = 45.0
    story_word_count: WordCountConfig = Field(default_factory=WordCountConfig)
    categories: list[str] = Field(default_factory=list)
    avoid_recent_days: int = 30
    entity_cooldown_days: int = 180
    duplicate_similarity_threshold: float = Field(default=0.62, ge=0.0, le=1.0)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @field_validator("categories")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("content.categories must not be empty")
        return [c.strip().lower() for c in v]


class MusicBalanceConfig(_Base):
    dominant_regions: list[str] = Field(default_factory=list)
    max_dominant_share: float = Field(default=0.6, ge=0.0, le=1.0)


class MusicConfig(_Base):
    target_share: float = Field(default=0.15, ge=0.0, le=1.0)
    genres: dict[str, list[str]] = Field(default_factory=dict)
    subjects: list[str] = Field(default_factory=list)
    eras: list[str] = Field(default_factory=list)
    global_balance: MusicBalanceConfig = Field(default_factory=MusicBalanceConfig)

    @property
    def all_genres(self) -> list[str]:
        """Top-level genres plus every subgenre, flattened."""
        out = list(self.genres)
        for subs in self.genres.values():
            out.extend(subs)
        return sorted(set(out))

    def parent_genre(self, genre: str) -> str | None:
        genre = genre.lower()
        if genre in self.genres:
            return genre
        for parent, subs in self.genres.items():
            if genre in subs:
                return parent
        return None


class CrossDomainConfig(_Base):
    per_extra_domain: float = 2.5
    max: float = 7.5


class ScoringConfig(_Base):
    weights: dict[str, float] = Field(default_factory=dict)
    music_weights: dict[str, float] = Field(default_factory=dict)
    cross_domain_bonus: CrossDomainConfig = Field(default_factory=CrossDomainConfig)
    rotation_bonus_max: float = 6.0
    duplicate_penalty_max: float = 40.0

    @model_validator(mode="after")
    def _normalise(self) -> ScoringConfig:
        if not self.weights:
            raise ValueError("scoring.weights must not be empty")
        for name, table in (("weights", self.weights), ("music_weights", self.music_weights)):
            total = sum(table.values())
            if total <= 0:
                raise ValueError(f"scoring.{name} must sum to a positive number")
            for key in table:
                table[key] = table[key] / total
        return self


class PipelineConfig(_Base):
    discovery_target: int = 120
    prefilter_keep: int = 24
    triage_keep: int = 6
    research_keep: int = 3
    deep_score_keep: int = 2
    write_attempts: int = 2
    skip_edition_if_below_threshold: bool = True

    @model_validator(mode="after")
    def _monotonic(self) -> PipelineConfig:
        stages = [
            self.discovery_target,
            self.prefilter_keep,
            self.triage_keep,
            self.research_keep,
            self.deep_score_keep,
        ]
        if any(b > a for a, b in zip(stages, stages[1:], strict=False)):
            raise ValueError("pipeline stages must narrow: each keep <= the previous one")
        return self


class SourceConfig(_Base):
    name: str
    enabled: bool = True
    limit: int = Field(default=20, ge=1, le=500)
    options: dict[str, Any] = Field(default_factory=dict)


class DiscoveryConfig(_Base):
    sources: list[SourceConfig] = Field(default_factory=list)


class ImageConfig(_Base):
    min_width: int = 900
    min_height: int = 600
    email_display_width: int = 1200
    max_download_bytes: int = 20_000_000
    allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/webp"]
    )


class ResearchConfig(_Base):
    max_pages_per_candidate: int = 6
    max_page_chars: int = 18_000
    min_sources_per_edition: int = 3
    min_independent_sources_for_key_claim: int = 2
    request_timeout_seconds: int = 20
    allowed_domains_extra: list[str] = Field(default_factory=list)


class LLMConfig(_Base):
    provider: Literal["anthropic", "openai", "stub"] = "anthropic"
    model: str = "claude-sonnet-5"
    triage_model: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 4096
    temperature: float = 0.7
    analysis_temperature: float = 0.1
    timeout_seconds: int = 120
    max_retries: int = 3
    base_url: str | None = None
    api_key: str | None = Field(default=None, exclude=True, repr=False)


class QualityConfig(_Base):
    min_accuracy: float = 80
    min_writing: float = 72
    min_sourcing: float = 75
    require_image_attribution: bool = True
    require_source_links: bool = True
    min_sources: int = 3
    max_regeneration_attempts: int = 1


class EmailConfig(_Base):
    provider: Literal["smtp", "resend", "sendgrid", "console", "file"] = "smtp"
    subject_template: str = "{name} #{issue:03d} - {title}"
    preheader_from_hook: bool = True
    include_plain_text: bool = True
    music_listen_block: bool = True
    # Populated from the environment, never from YAML.
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    credentials: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)


class StorageConfig(_Base):
    database_path: str = "data/curious.db"
    run_retention_days: int = 365


class LoggingConfig(_Base):
    level: str = "INFO"
    format: Literal["text", "json"] = "text"


class Config(_Base):
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)
    content: ContentConfig
    music: MusicConfig = Field(default_factory=MusicConfig)
    scoring: ScoringConfig
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Not from YAML: assembled at load time.
    source_api_keys: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    #: True when no LLM_API_KEY was present and we silently fell back to the
    #: deterministic stub writer. The CLI warns loudly about this.
    llm_fell_back_to_stub: bool = False
    user_agent: str = "CuriousThings/1.0 (+https://github.com/)"
    #: Where templates, config and the bundled fixtures live.
    repo_root: Path = REPO_ROOT
    #: Where rendered editions and run artefacts are written. Kept separate
    #: from repo_root so it can be redirected (in tests, or to a mounted
    #: volume) without moving the templates.
    output_path: str = "output"

    @model_validator(mode="after")
    def _music_is_a_category(self) -> Config:
        if "music" not in self.content.categories:
            raise ValueError(
                "content.categories must include 'music' - it is a first-class domain"
            )
        return self

    @property
    def database_file(self) -> Path:
        path = Path(self.storage.database_path)
        return path if path.is_absolute() else self.repo_root / path

    @property
    def output_dir(self) -> Path:
        path = Path(self.output_path)
        return path if path.is_absolute() else self.repo_root / path


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from exc


def _split_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split(";")]
    return [p for p in parts if p]


_DISPLAY_ADDRESS = re.compile(r"<([^>]+)>")


def _bare_address(value: str) -> str:
    """``"A Curious Thing <me@example.com>"`` -> ``me@example.com``."""
    match = _DISPLAY_ADDRESS.search(value or "")
    return (match.group(1) if match else value or "").strip()


def load_config(path: str | Path | None = None, *, load_dotenv_file: bool = True) -> Config:
    """Read YAML + environment into a validated :class:`Config`.

    Raises ``ValueError`` with a readable message on bad configuration; the CLI
    turns that into a clean exit rather than a traceback.
    """
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(REPO_ROOT / ".env", override=False)
        except ImportError:  # python-dotenv is optional at runtime
            pass

    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ValueError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}

    # --- environment overrides ------------------------------------------- #
    newsletter = raw.setdefault("newsletter", {})
    if tz := _env("TIMEZONE"):
        newsletter["timezone"] = tz
    if (interval := _env_int("SEND_INTERVAL_DAYS")) is not None:
        newsletter["frequency_days"] = interval
    hour, minute = _env_int("SEND_HOUR"), _env_int("SEND_MINUTE")
    if hour is not None or minute is not None:
        current = newsletter.get("send_time", "08:00").split(":")
        hour = hour if hour is not None else int(current[0])
        minute = minute if minute is not None else int(current[1])
        newsletter["send_time"] = f"{hour:02d}:{minute:02d}"

    llm = raw.setdefault("llm", {})
    if provider := _env("LLM_PROVIDER"):
        llm["provider"] = provider.lower()
    if model := _env("LLM_MODEL"):
        llm["model"] = model
    if triage := _env("LLM_TRIAGE_MODEL"):
        llm["triage_model"] = triage
    if base_url := _env("LLM_BASE_URL"):
        llm["base_url"] = base_url
    llm["api_key"] = _env("LLM_API_KEY")
    # No key and no explicit choice of a keyless provider? Fall back to the
    # deterministic stub so `run --dry-run` works on a fresh clone.
    if not llm["api_key"] and llm.get("provider") != "stub":
        llm["_fell_back_to_stub"] = True

    email_cfg = raw.setdefault("email", {})
    if provider := _env("EMAIL_PROVIDER"):
        email_cfg["provider"] = provider.lower()
    email_cfg["sender"] = _env("EMAIL_FROM", "") or ""
    # A personal newsletter is usually sent to the person sending it, so an
    # unset EMAIL_TO defaults to the sender rather than being an error. Set
    # EMAIL_TO explicitly to send somewhere else.
    email_cfg["recipients"] = _split_recipients(_env("EMAIL_TO"))
    if not email_cfg["recipients"] and email_cfg["sender"]:
        email_cfg["recipients"] = [_bare_address(email_cfg["sender"])]
    email_cfg["reply_to"] = _env("EMAIL_REPLY_TO")
    email_cfg["credentials"] = {
        key: value
        for key, value in {
            "smtp_host": _env("SMTP_HOST"),
            "smtp_port": _env("SMTP_PORT"),
            "smtp_username": _env("SMTP_USERNAME"),
            "smtp_password": _env("SMTP_PASSWORD"),
            "smtp_security": _env("SMTP_SECURITY", "starttls"),
            "resend_api_key": _env("RESEND_API_KEY"),
            "sendgrid_api_key": _env("SENDGRID_API_KEY"),
        }.items()
        if value
    }

    storage = raw.setdefault("storage", {})
    if db_path := _env("DATABASE_PATH"):
        storage["database_path"] = db_path
    if output_path := _env("OUTPUT_DIR"):
        raw["output_path"] = output_path

    log_cfg = raw.setdefault("logging", {})
    if fmt := _env("LOG_FORMAT"):
        log_cfg["format"] = fmt.lower()
    if level := _env("LOG_LEVEL"):
        log_cfg["level"] = level.upper()

    fell_back = bool(llm.pop("_fell_back_to_stub", False))
    if fell_back:
        llm["provider"] = "stub"

    raw["source_api_keys"] = {
        key: value
        for key, value in {
            "nasa": _env("NASA_API_KEY"),
            "smithsonian": _env("SMITHSONIAN_API_KEY"),
            "europeana": _env("EUROPEANA_API_KEY"),
        }.items()
        if value
    }
    raw["user_agent"] = _env("HTTP_USER_AGENT") or (
        "CuriousThings/1.0 (personal newsletter; +https://github.com/)"
    )
    raw["llm_fell_back_to_stub"] = fell_back

    try:
        config = Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> readable message
        raise ValueError(f"invalid configuration in {config_path}:\n{exc}") from exc

    return config
