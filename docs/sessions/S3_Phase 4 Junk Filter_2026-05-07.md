---
session: 3
topic: "Phase 4 Junk Filter"
continued_from: null
status: crashed
start: "2026-05-07T18:03:21"
files_touched: ["docs/PLAN.md", "docs/STATUS.md", "docs/VERIFICATION.md", "docs/SPEC.md", "src/market_notification/filter/base.py", "src/market_notification/poller/poller.py", "src/market_notification/db/models.py", "src/market_notification/db/repositories/base.py", "src/market_notification/db/repositories/notification_repo_sqla.py", "src/market_notification/db/repositories/poll_state_repo_sqla.py", "src/market_notification/exchange/base.py", "src/market_notification/pipeline/states.py", "src/market_notification/config/settings.py", "scripts/bootstrap_db.py", "scripts/run_poller.py", "scripts/_phase3_smoke.py", "tests/conftest.py", "tests/integration/test_poller_short_run.py", "tests/unit/test_dedup.py", "config/default.toml", "config/filter_rules.json.example", "brain/exchange_util/notification_poller.py", "brain/notification_database.py", "src/market_notification/filter/filter_engine.py (NEW)", "src/market_notification/db/repositories/filter_rule_repo_sqla.py (NEW)", "config/filter_rules.json (NEW)", "scripts/seed_filter_rules.py (NEW)", "scripts/_phase4_smoke.py (NEW)", "tests/unit/test_filter_engine.py (NEW)", "tests/integration/test_filter_seed_and_poller.py (NEW)", "src/market_notification/poller/poller.py (filter wiring + junk counter)", "scripts/run_poller.py (RegexFilterEngine injection)", "pyproject.toml (perf marker)"]
tags: ["phase-4", "junk-filter", "FR-FILTER-001..006", "DECISION", "INSIGHT"]
---
# Session 3 -- Phase 4 Junk Filter

## Summary

- **E1** DECISION: start phase 4, junk filter  [phase-4] [junk-filter] [FR-FILTER-001..006] [DECISION] [INSIGHT]
<!-- end-summary -->

## Exchange Log

### <a name="e1_180345"></a>Exchange 1

**User:** start phase 4, junk filter

**Claude:** Implemented Phase 4 end-to-end: RegexFilterEngine (4 rule types, source-scoped, invalid-regex tolerant), SqlaFilterRuleRepo with explicit NULL-aware idempotent add() to dodge SQLite's UNIQUE-NULL-distinct quirk, 14-rule seed in config/filter_rules.json (ported from brain + augmented), seed_filter_rules.py loader, Poller wiring with new junk counter and is_useless+junk_rule_id+pipeline_status='ignored' for matched rows (and skipping cross-exchange grouping for junk). Added 13 unit tests + 4 integration tests + scripts/_phase4_smoke.py. Live smoke against the real DB: 31/158 sample rows flagged (19.6%, target >=10%); 1000 evals in 0.004s (254K/sec, target >=1000/sec). All 113 tests passing. Updated STATUS.md and VERIFICATION.md to mark Phase 4 verified. Phase 5 (Gemma classifier) is next.

| Key | Value |
|-----|-------|
| Tools | Read, Write, Edit, Bash, Grep, TaskCreate, TaskUpdate |
| Files Read | docs/PLAN.md, docs/STATUS.md, docs/VERIFICATION.md, docs/SPEC.md, src/market_notification/filter/base.py, src/market_notification/poller/poller.py, src/market_notification/db/models.py, src/market_notification/db/repositories/base.py, src/market_notification/db/repositories/notification_repo_sqla.py, src/market_notification/db/repositories/poll_state_repo_sqla.py, src/market_notification/exchange/base.py, src/market_notification/pipeline/states.py, src/market_notification/config/settings.py, scripts/bootstrap_db.py, scripts/run_poller.py, scripts/_phase3_smoke.py, tests/conftest.py, tests/integration/test_poller_short_run.py, tests/unit/test_dedup.py, config/default.toml, config/filter_rules.json.example, brain/exchange_util/notification_poller.py, brain/notification_database.py |
| Files Modified | src/market_notification/filter/filter_engine.py (NEW), src/market_notification/db/repositories/filter_rule_repo_sqla.py (NEW), config/filter_rules.json (NEW), scripts/seed_filter_rules.py (NEW), scripts/_phase4_smoke.py (NEW), tests/unit/test_filter_engine.py (NEW), tests/integration/test_filter_seed_and_poller.py (NEW), src/market_notification/poller/poller.py (filter wiring + junk counter), scripts/run_poller.py (RegexFilterEngine injection), pyproject.toml (perf marker), docs/STATUS.md, docs/VERIFICATION.md |
| Status | PASS |
| Fix | Caught SQLite UNIQUE-NULL-distinct foot-gun on idempotency test; fixed in repo.add() with explicit IS NULL pre-check. |
| Tags | [phase-4] [junk-filter] [FR-FILTER-001..006] [DECISION] [INSIGHT] |

