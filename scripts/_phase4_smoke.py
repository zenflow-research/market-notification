"""Phase 4 verification harness.

Verifies the junk-filter pipeline against real ingested rows. The script:

  1. Loads `config/filter_rules.json` into the DB via SqlaFilterRuleRepo.
  2. Builds a RegexFilterEngine from the freshly seeded table.
  3. Samples up to 200 already-ingested rows from `notifications` and runs
     the engine over them (offline — no network, no LLM).
  4. Reports the flagged percentage and writes verification artifacts.

Artifacts in `verification/phase_4_results/`:
  - seed_summary.txt         -- rule counts by type/source/created_by
  - flagged_sample_20.csv    -- first 20 flagged rows for manual eyeball
  - passed_sample_20.csv     -- first 20 not-flagged rows
  - perf_benchmark.txt       -- 1000-row throughput on real headlines
  - phase4_summary.txt       -- aggregate counters + exit-criteria checklist

Exit criteria mirror docs/VERIFICATION.md §Phase 4:
  - rules loaded
  - >=10% of 200-row sample flagged
  - <1.0s for 1000 evaluations
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from market_notification.db.models import (  # noqa: E402
    Notification,
    NotificationFilterRule,
)
from market_notification.db.repositories.filter_rule_repo_sqla import (  # noqa: E402
    SqlaFilterRuleRepo,
)
from market_notification.db.session import get_session  # noqa: E402
from market_notification.exchange.base import RawNotification  # noqa: E402
from market_notification.filter.filter_engine import RegexFilterEngine  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_4_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RULES_JSON = ROOT / "config" / "filter_rules.json"
SAMPLE_N = 200
PERF_N = 1000


def seed_rules() -> int:
    with RULES_JSON.open("r", encoding="utf-8") as f:
        rules = json.load(f)
    with get_session() as sess:
        repo = SqlaFilterRuleRepo(sess)
        for r in rules:
            repo.add(
                rule_type=r["rule_type"],
                pattern=r["pattern"],
                source=r.get("source"),
                action=r.get("action", "hide"),
                created_by=r.get("created_by", "system"),
                reason=r.get("reason"),
            )
    return len(rules)


def dump_seed_summary() -> dict:
    with get_session() as sess:
        rows = sess.execute(
            select(NotificationFilterRule).where(
                NotificationFilterRule.is_active == 1
            )
        ).scalars().all()
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_creator: dict[str, int] = {}
    for r in rows:
        by_type[r.rule_type] = by_type.get(r.rule_type, 0) + 1
        s = r.source or "<both>"
        by_source[s] = by_source.get(s, 0) + 1
        by_creator[r.created_by] = by_creator.get(r.created_by, 0) + 1
    summary = {
        "total_active": len(rows),
        "by_type": by_type,
        "by_source": by_source,
        "by_creator": by_creator,
    }
    (OUT_DIR / "seed_summary.txt").write_text(
        f"Phase 4 filter-rule seed summary @ {datetime.now().isoformat()}\n\n"
        f"Total active rules: {summary['total_active']}\n"
        f"By type:    {summary['by_type']}\n"
        f"By source:  {summary['by_source']}\n"
        f"By creator: {summary['by_creator']}\n",
        encoding="utf-8",
    )
    return summary


def _row_to_raw(row: Notification) -> RawNotification:
    return RawNotification(
        source=row.source,
        seq_id=row.seq_id,
        headline=row.headline,
        category=row.category,
        subcategory=row.subcategory,
        body=row.body,
        announced_at=row.announced_at,
        exchange_disseminated_at=row.exchange_disseminated_at,
        attachment_url=row.attachment_url,
        attachment_name=row.attachment_name,
        attachment_size=row.attachment_size,
        is_critical=bool(row.is_critical),
        has_xbrl=bool(row.has_xbrl),
        symbol=row.symbol,
        company_name_raw=row.company_name_raw,
        isin=row.isin,
        industry_raw=row.industry_raw,
        raw_json=row.raw_json or "{}",
    )


def sample_and_classify(engine: RegexFilterEngine, limit: int = SAMPLE_N):
    """Load up to `limit` ingested rows and bucket flagged vs passed."""
    with get_session() as sess:
        rows = sess.execute(
            select(Notification).order_by(Notification.id.desc()).limit(limit)
        ).scalars().all()

    flagged: list[tuple[Notification, object]] = []
    passed: list[Notification] = []
    for r in rows:
        match = engine.is_junk(_row_to_raw(r))
        if match is not None:
            flagged.append((r, match))
        else:
            passed.append(r)
    return rows, flagged, passed


def write_csv(path: Path, rows: list, header: list[str], to_tuple) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(to_tuple(r))


def perf_benchmark(engine: RegexFilterEngine, sample_rows) -> tuple[float, float]:
    """Run engine over `PERF_N` evaluations using real headlines (with cycling)."""
    if not sample_rows:
        return 0.0, 0.0
    raws = [_row_to_raw(r) for r in sample_rows]
    n = len(raws)
    start = time.perf_counter()
    matches = 0
    for i in range(PERF_N):
        if engine.is_junk(raws[i % n]) is not None:
            matches += 1
    elapsed = time.perf_counter() - start
    rate = PERF_N / elapsed if elapsed > 0 else float("inf")
    (OUT_DIR / "perf_benchmark.txt").write_text(
        f"Phase 4 perf benchmark @ {datetime.now().isoformat()}\n\n"
        f"Evaluations: {PERF_N}\n"
        f"Distinct source rows: {n}\n"
        f"Matches:    {matches}\n"
        f"Elapsed:    {elapsed:.4f}s\n"
        f"Rate:       {rate:.0f} evals/sec\n"
        f"NFR-PERF-001 target: >=1000 evals/sec/core (PASS={rate >= 1000})\n",
        encoding="utf-8",
    )
    return elapsed, rate


def main() -> int:
    print(f"Phase 4 smoke -- artifacts -> {OUT_DIR}")

    print("\n=== Seed rules ===")
    n_seeded = seed_rules()
    summary = dump_seed_summary()
    print(f"  seeded {n_seeded} rules from {RULES_JSON.name}")
    print(f"  active in DB: {summary['total_active']}")
    print(f"  by_type: {summary['by_type']}")

    print("\n=== Engine ===")
    engine = RegexFilterEngine(get_session)
    print(f"  loaded {len(engine)} active rules")

    print(f"\n=== Sample {SAMPLE_N} ===")
    rows, flagged, passed = sample_and_classify(engine, SAMPLE_N)
    print(f"  rows examined: {len(rows)}")
    print(f"  flagged:       {len(flagged)}")
    print(f"  passed:        {len(passed)}")
    pct = (len(flagged) / len(rows) * 100) if rows else 0.0
    print(f"  junk %: {pct:.1f}% (target: >=10% per Phase 4 exit criteria)")

    write_csv(
        OUT_DIR / "flagged_sample_20.csv",
        flagged[:20],
        ["id", "source", "company_id", "category", "subcategory",
         "headline", "rule_id", "rule_type", "pattern", "reason"],
        lambda fr: (
            fr[0].id, fr[0].source, fr[0].company_id, fr[0].category,
            fr[0].subcategory, fr[0].headline,
            fr[1].rule_id, fr[1].rule_type, fr[1].pattern, fr[1].reason,
        ),
    )
    write_csv(
        OUT_DIR / "passed_sample_20.csv",
        passed[:20],
        ["id", "source", "company_id", "category", "subcategory", "headline"],
        lambda r: (r.id, r.source, r.company_id, r.category, r.subcategory, r.headline),
    )

    print("\n=== Perf ===")
    elapsed, rate = perf_benchmark(engine, list(rows) if rows else [])
    print(f"  {PERF_N} evaluations in {elapsed:.3f}s ({rate:.0f}/sec)")

    print("\n=== Exit criteria ===")
    rules_ok = summary["total_active"] > 0
    sample_ok = (len(flagged) / len(rows)) >= 0.10 if rows else False
    perf_ok = elapsed < 1.0 if rows else False
    print(f"  rules loaded:               {rules_ok}")
    print(f"  >=10% of sample flagged:    {sample_ok}  ({pct:.1f}%)")
    print(f"  perf <1s for 1000 rows:     {perf_ok}  ({elapsed:.3f}s)")

    (OUT_DIR / "phase4_summary.txt").write_text(
        f"Phase 4 smoke summary @ {datetime.now().isoformat()}\n\n"
        f"Rules seeded:         {n_seeded}\n"
        f"Rules active (DB):    {summary['total_active']}\n"
        f"By type:              {summary['by_type']}\n"
        f"By source:            {summary['by_source']}\n"
        f"By creator:           {summary['by_creator']}\n\n"
        f"Sample size:          {len(rows)}\n"
        f"Flagged:              {len(flagged)}  ({pct:.1f}%)\n"
        f"Passed:               {len(passed)}\n\n"
        f"Perf evaluations:     {PERF_N}\n"
        f"Perf elapsed:         {elapsed:.4f}s\n"
        f"Perf rate:            {rate:.0f} evals/sec\n\n"
        f"Exit criteria:\n"
        f"  rules loaded               -> {rules_ok}\n"
        f"  >=10% of sample flagged    -> {sample_ok}  ({pct:.1f}%)\n"
        f"  perf <1s/1000 evaluations  -> {perf_ok}  ({elapsed:.3f}s)\n",
        encoding="utf-8",
    )

    return 0 if rules_ok and (sample_ok or not rows) and (perf_ok or not rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
