"""Structured logging with named pipeline stages.

Every log line carries a stage (DISCOVERY, RESEARCH, SCORING, ...) and the
execution id, so one run can be pulled out of a week of CI logs with a grep.

``LOG_FORMAT=json`` gives one JSON object per line for machine consumption;
``text`` gives an aligned, readable console format for local work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any


class Stage:
    """The canonical stage names. Used as a namespace, not an enum, so that
    ``logger.info(...)`` calls stay short and greppable."""

    CONFIG = "CONFIG"
    SCHEDULE = "SCHEDULE"
    DISCOVERY = "DISCOVERY"
    LICENSING = "LICENSING"
    PREFILTER = "PREFILTER"
    TRIAGE = "TRIAGE"
    RESEARCH = "RESEARCH"
    FACTCHECK = "FACTCHECK"
    SCORING = "SCORING"
    DEDUP = "DEDUP"
    SELECTION = "SELECTION"
    IMAGE = "IMAGE_DOWNLOAD"
    WRITING = "WRITING"
    QUALITY = "QUALITY_CHECK"
    RENDER = "RENDER"
    EMAIL = "EMAIL"
    STORAGE = "STORAGE"
    LLM = "LLM"


_execution_id: ContextVar[str] = ContextVar("execution_id", default="-")
_stage: ContextVar[str] = ContextVar("stage", default="-")

#: Anything that looks like a credential gets redacted before it can reach a
#: log file or a CI transcript.
_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(xkeysib-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(re_[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(SG\.[A-Za-z0-9_\-.]{12,})"),
    re.compile(r"((?:api[_-]?key|token|password|secret)[\"'\s:=]+)([^\s\"',}]{6,})", re.I),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}***redacted***", text)
        else:
            text = pattern.sub("***redacted***", text)
    return text


def new_execution_id() -> str:
    exec_id = uuid.uuid4().hex[:12]
    _execution_id.set(exec_id)
    return exec_id


def set_execution_id(exec_id: str) -> None:
    _execution_id.set(exec_id)


def get_execution_id() -> str:
    return _execution_id.get()


class stage:  # noqa: N801 - used as a context manager, reads like a keyword
    """``with stage(Stage.RESEARCH): ...`` tags every line inside."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._token = None

    def __enter__(self) -> stage:
        self._token = _stage.set(self.name)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _stage.reset(self._token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.execution_id = _execution_id.get()
        record.stage = getattr(record, "stage", None) or _stage.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "stage": getattr(record, "stage", "-"),
            "execution_id": getattr(record, "execution_id", "-"),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    _COLOURS = {
        "DEBUG": "\033[38;5;244m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;203m\033[97m",
    }
    _RESET = "\033[0m"

    def __init__(self, *, colour: bool) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        stage_name = getattr(record, "stage", "-")
        level = record.levelname
        head = f"{ts} {level:<7} {stage_name:<14}"
        if self.colour:
            head = f"{self._COLOURS.get(level, '')}{head}{self._RESET}"
        message = redact(record.getMessage())
        extras = getattr(record, "extra_fields", {})
        if extras:
            message += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            message += "\n" + redact(self.formatException(record.exc_info))
        return f"{head} {message}"


#: kwargs that belong to ``logging`` itself and must not become log fields.
_RESERVED_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})


class StageLogger(logging.LoggerAdapter):
    """Adds ``logger.info("msg", field=value)`` style structured extras."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = kwargs.pop("extra", None) or {}
        fields = {k: kwargs.pop(k) for k in list(kwargs) if k not in _RESERVED_KWARGS}
        merged = {**extra, "extra_fields": {**extra.get("extra_fields", {}), **fields}}
        kwargs["extra"] = merged
        return msg, kwargs


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        colour = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
        handler.setFormatter(TextFormatter(colour=colour))
    handler.addFilter(_ContextFilter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Third-party noise we never want in the transcript.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> StageLogger:
    return StageLogger(logging.getLogger(name), {})


__all__ = [
    "Stage",
    "StageLogger",
    "configure_logging",
    "get_execution_id",
    "get_logger",
    "new_execution_id",
    "redact",
    "set_execution_id",
    "stage",
]
