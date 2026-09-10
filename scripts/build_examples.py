#!/usr/bin/env python3
"""Render the example editions in ``examples/*.json`` to HTML.

The examples are rendered through the same :class:`~src.delivery.renderer.Renderer`
and the same template as a real edition, so they cannot drift away from what
the system actually produces. They are also put through the licence gate and
the quality checks, and the script fails if an example would not have been
sent - an example that could not pass the project's own bar would be a poor
advertisement for it.

    python scripts/build_examples.py            # render and check
    python scripts/build_examples.py --check    # check only, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config  # noqa: E402
from src.delivery.renderer import Renderer  # noqa: E402
from src.licensing import vet_image  # noqa: E402
from src.logging_setup import configure_logging  # noqa: E402
from src.models import Article, Candidate  # noqa: E402

EXAMPLES = REPO_ROOT / "examples"


def build(check_only: bool = False) -> int:
    configure_logging("WARNING", "text")
    config = load_config(load_dotenv_file=False)
    renderer = Renderer(config)
    words = config.content.story_word_count
    problems: list[str] = []

    payloads = sorted(EXAMPLES.glob("issue-*.json"))
    if not payloads:
        print("No example payloads found in examples/.")
        return 1

    for path in payloads:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidate = Candidate.model_validate(payload["candidate"])
        article = Article.model_validate(payload["article"])

        usable, reason = vet_image(candidate.image, hints=candidate.image.institution or "")
        if not usable:
            problems.append(f"{path.name}: image would be refused - {reason}")
            continue

        count = article.word_count
        if not (words.hard_min <= count <= words.hard_max):
            problems.append(f"{path.name}: {count} words, outside "
                            f"{words.hard_min}-{words.hard_max}")
        if len(article.sources) < config.quality.min_sources:
            problems.append(f"{path.name}: only {len(article.sources)} sources")
        if not any(s.authority >= 70 for s in article.sources):
            problems.append(f"{path.name}: no authoritative source")

        rendered = renderer.render(
            candidate, article,
            issue_number=payload["issue_number"],
            edition_date=date.fromisoformat(payload["edition_date"]),
            footer_note="Sample edition, rendered from examples/ by scripts/build_examples.py",
        )
        html_path = path.with_suffix(".html")
        text_path = path.with_suffix(".txt")
        if not check_only:
            html_path.write_text(rendered.html, encoding="utf-8")
            text_path.write_text(rendered.text, encoding="utf-8")
        print(f"{path.name:<20} {count:>4} words  {len(article.sources)} sources  "
              f"-> {html_path.name}")

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nAll examples clear the project's own quality bar.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="do not write files")
    sys.exit(build(check_only=parser.parse_args().check))
