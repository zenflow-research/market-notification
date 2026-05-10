"""Seed `notification_filter_rules` from `config/filter_rules.json` (FR-FILTER-003).

One-shot loader: idempotent on the (rule_type, pattern, source) UNIQUE so
re-running doesn't duplicate rules. Source-of-truth is the JSON file; the DB
table is the runtime cache (UI-driven additions go in there too).

Usage:
    python -m scripts.seed_filter_rules
    python -m scripts.seed_filter_rules --rules-path config/filter_rules.json
    python -m scripts.seed_filter_rules --activate-existing  (reactivates any
        deactivated rules whose key matches the JSON; default off)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.db.repositories.filter_rule_repo_sqla import (  # noqa: E402
    SqlaFilterRuleRepo,
)
from market_notification.db.session import dispose_engine, get_session  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "filter_rules.json"


@click.command()
@click.option(
    "--rules-path",
    type=click.Path(path_type=Path, exists=False),
    default=DEFAULT_RULES_PATH,
    help="Path to filter_rules.json (default: config/filter_rules.json).",
)
def main(rules_path: Path) -> int:
    configure_logging()
    log = get_logger("scripts.seed_filter_rules")

    if not rules_path.exists():
        log.error("Rules file not found: %s", rules_path)
        return 1

    with rules_path.open("r", encoding="utf-8") as f:
        rules = json.load(f)
    if not isinstance(rules, list):
        log.error("Rules file must be a JSON array, got %s", type(rules).__name__)
        return 1
    log.info("Loaded %d rule definitions from %s", len(rules), rules_path)

    inserted = 0
    skipped = 0
    try:
        with get_session() as sess:
            repo = SqlaFilterRuleRepo(sess)
            existing_ids = {r["id"] for r in repo.list_active()}
            for rule in rules:
                rid = repo.add(
                    rule_type=rule["rule_type"],
                    pattern=rule["pattern"],
                    source=rule.get("source"),
                    action=rule.get("action", "hide"),
                    created_by=rule.get("created_by", "system"),
                    confidence=rule.get("confidence"),
                    reason=rule.get("reason"),
                )
                if rid in existing_ids:
                    skipped += 1
                else:
                    inserted += 1
                    existing_ids.add(rid)
        log.info("Seed done: inserted=%d skipped=%d", inserted, skipped)
    finally:
        dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
