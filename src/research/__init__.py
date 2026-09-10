"""Research: turning a promising image into a dossier a writer can trust."""

from .fact_checker import FactChecker  # noqa: F401
from .researcher import Researcher  # noqa: F401
from .sources import authority_for, rank_sources  # noqa: F401

__all__ = ["FactChecker", "Researcher", "authority_for", "rank_sources"]
