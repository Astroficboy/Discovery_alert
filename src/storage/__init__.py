"""Persistence: the edition history that stops the newsletter repeating itself."""

from .database import Database, connect  # noqa: F401

__all__ = ["Database", "connect"]
