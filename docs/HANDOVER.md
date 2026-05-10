# Market Notification System — Session Handover Template

> **When to use.** Fill this in when the current session is approaching the
> 1000 tool-call cap (typically at ~700) AND we're at a clean boundary
> (end of a phase, end of a clearly-bounded sub-task — never mid-implementation
> of a single file).
>
> **Don't write this if you're not approaching the cap.** It's only for handoff.
>
> **Format.** Add a `## Session N → Session N+1` section each time. Don't delete
> previous handover sections; they form the project audit trail.

---

## Template (copy this for each new handover)

```markdown
## Session N → Session N+1  (YYYY-MM-DD)

### Why handing over
- Tool calls used: ~XXX / 1000
- Clean boundary: <Phase X completed> | <Phase X step Y completed>

### What was completed this session
- Phase X.subY — <one line>
- Phase X.subZ — <one line>

### What is the exact next step
- Phase X.subW: <imperative sentence — "Implement <module>.<function> per architecture.md §X.Y">
- Files involved: <list>
- Tests to write or run next: <list>

### Open subtasks for the rest of Phase X
- [ ] subA — <one line>
- [ ] subB — <one line>
- [ ] subC — <one line>

### Decisions made this session (added to design-decisions.md §P)
- D-XX: <one line>

### Open questions raised this session (added to QUESTIONS.md)
- Q-XX: <one line>

### Files modified this session
- src/market_notification/...
- tests/...
- docs/...

### Don't-forget checklist
- [ ] STATUS.md updated
- [ ] design-decisions.md §P updated if any new decisions
- [ ] QUESTIONS.md updated if any new opens
- [ ] verification/phase_X_results/ artifacts captured if Phase X verified
- [ ] Session log written via `python ~/.claude/scripts/session_log.py end --session N`

### Next session quick-start
1. Read CONTEXT.md (5 min)
2. Read STATUS.md → confirm "current step" matches the "exact next step" above
3. Read this handover section in HANDOVER.md
4. Resume at the next step
```

---

## Past handovers

---

## Session 1 → Session 2  (2026-05-07)

### Why handing over

- Tool calls used: **~225 / 1000**.
- Clean boundary: **Phase 1 verified end-to-end**; Phase 2 is a fresh, well-bounded scope.
- User-initiated handoff — they want to start Phase 2 in a fresh session.

### What was completed this session

- **All 8 planning docs created** (PLAN, SPEC, architecture, STATUS, CONTEXT, QUESTIONS, VERIFICATION, HANDOVER) plus design-decisions.md filled with user's answers.
- **Phase 0 verified**: project skeleton, schema (7 tables), settings/logging/config, alembic infra, 6 unit tests for schema, bootstrap_db.py runs clean. Artifacts in `verification/phase_0_results/`.
- **Phase 1 verified**: CompanyProvider chain shipped — CsvCompanyProvider + ScreenerOriginalCompanyProvider + CompositeCompanyProvider + factory. **14/14 unit tests pass.** Live smoke: 5,249 companies, RELIANCE 35-bar price series, ISIN 100% / mcap 100% / BSE 87.4% / NSE 53.5%. Artifacts in `verification/phase_1_results/`.
- **Resolved Q-7**: `pv_df_check` (not `check_pv_df`) is the right helper. Ported its logic; no runtime import.
- **Resolved Q-11**: Fundamentals come from `G:\Screener_original\stockDirectory\{id}\` per-company folders, NOT brain's `screener_essential.db` (which has empty fundamentals tables). Recorded as Deviation 2.
- **Reverted Deviation 1**: Was based on wrong-name search; user corrected.

### What is the exact next step

**Start Phase 2 — Exchange Fetchers.**

Create cleaned copies of brain's BSE + NSE fetchers under `src/market_notification/exchange/`:

| Source (brain) | Target | Size | What to keep |
|---|---|---|---|
| `G:\brain\exchange_util\BSE\BSE_fetcher.py` | `src/market_notification/exchange/bse_fetcher.py` | 23 KB | Notification fetch + attachment download. Drop methods unrelated to notifications (price, mcap, sector, shareholding, etc.). |
| `G:\brain\exchange_util\NSE\NSEFetcher.py` | `src/market_notification/exchange/nse_fetcher.py` | 78 KB | Same pattern — keep `fetch_latest_announcements`, `fetch_announcements_by_symbol` and the auth/cookie warm-up. Drop everything else. |

Then add:

| File | Purpose |
|---|---|
| `src/market_notification/exchange/normalizer.py` | Convert raw API records → `RawNotification` DTO (defined in `exchange/base.py`). Handle the date-format zoo from brain (see `_NSE_DATE_FMTS`, `_BSE_DATE_FMTS` in `notification_poller.py`). |
| `tests/integration/test_fetchers_smoke.py` | Live calls to BSE + NSE; assert ≥10 rows; schema-validate one record. Mark `@pytest.mark.live_internet` for skip-if-no-internet. |

Both fetchers MUST conform to the `ExchangeFetcher` ABC at `src/market_notification/exchange/base.py`:
```python
fetch_latest(self, n: int = 50) -> list[RawNotification]
fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]
fetch_attachment(self, url: str) -> bytes
```

### Open subtasks for the rest of Phase 2

- [ ] Read brain's BSE_fetcher.py end-to-end; identify the 3-5 methods that touch notifications.
- [ ] Read brain's NSEFetcher.py; same.
- [ ] Confirm BSE/NSE API URLs match what's in `SPEC.md` §6.4.3.
- [ ] Write cleaned `bse_fetcher.py` (target ~5-8 KB after stripping unrelated methods).
- [ ] Write cleaned `nse_fetcher.py` (target ~10-15 KB; the auth-cookie flow is the largest chunk we keep).
- [ ] Write `normalizer.py` with date parsers covering both `_NSE_DATE_FMTS` and `_BSE_DATE_FMTS`.
- [ ] Write `tests/integration/test_fetchers_smoke.py` with live fixtures + recorded JSON for offline mode.
- [ ] Write `tests/fixtures/raw_notifications/{bse,nse}_sample.json` from one live capture so offline tests work.
- [ ] Run smoke test live, capture artifacts in `verification/phase_2_results/`.
- [ ] Update STATUS.md to mark Phase 2 verified.

### Decisions made this session (all captured in design-decisions.md and PLAN.md §14)

- **D-07 .. D-20** added during planning (SQLite+WAL, 3-process model, Gemma full priority override, deferred-doc tagging only, full-fundamentals injection, no git during build, etc.).
- **Deviation 1** REVERTED.
- **Deviation 2** ACTIVE: fundamentals from Screener_original stockDirectory (not brain).

### Open questions raised this session (all in QUESTIONS.md)

- Q-1, Q-2 — defer to Phase 4 / 9 entry.
- Q-3 — Ollama queue contention with brain — measure during Phase 8.
- Q-4 — Gemma multimodal capability — verify Phase 7.
- Q-7 — RESOLVED.
- Q-8 — gemini-rr cache-hit metadata parsing — Phase 11.
- Q-9 — Multi-user-ready schema (`user_id` already added at Phase 0).
- Q-10 — Annual report downstream pipeline ownership — Phase 13 forward.
- Q-11 — RESOLVED.

### Files modified this session

**Source code (Phase 1):**
- `src/market_notification/companies/csv_source.py` (new)
- `src/market_notification/companies/screener_original_source.py` (new)
- `src/market_notification/companies/composite.py` (new)
- `src/market_notification/companies/factory.py` (new)

**Tests:**
- `tests/unit/test_company_provider.py` (new, 14 tests)

**Scripts (helpers, not deliverables):**
- `scripts/_dump_schema.py` (Phase 0 verification helper)
- `scripts/_inspect_brain_db.py`, `_inspect_brain_tables.py` (Phase 1 entry investigations — can delete or keep for re-runs)
- `scripts/_phase1_smoke.py` (Phase 1 verification harness)

**Docs:**
- `docs/PLAN.md`, `docs/SPEC.md`, `docs/architecture.md`, `docs/STATUS.md`, `docs/CONTEXT.md`, `docs/QUESTIONS.md`, `docs/VERIFICATION.md`, `docs/HANDOVER.md` (this file), `docs/design-decisions.md` (user-edited + my additions).

**Verification artifacts:**
- `verification/phase_0_results/db_schema.txt`, `log_excerpt.txt`
- `verification/phase_1_results/coverage_report.md`, `lookup_results.csv`, `price_smoke.json`

### Don't-forget checklist (verify before starting Phase 2)

- [x] STATUS.md updated to "Phase 2 — Exchange Fetchers (pending)"
- [x] design-decisions.md Deviation 1 (reverted) and Deviation 2 (active) recorded
- [x] QUESTIONS.md Q-7 and Q-11 marked RESOLVED
- [x] verification/phase_0_results/ and phase_1_results/ contain artifacts
- [x] Session log: `python ~/.claude/scripts/session_log.py end --session 1` (run on session close)

### Next-session quick-start (Session 2)

```powershell
# 1. Activate the venv (already created)
cd G:\market_notification
.venv\Scripts\Activate.ps1

# 2. Sanity check
python -m pytest tests/unit/ -v   # all should pass (20 tests now: 6 schema + 14 company)
python -c "from market_notification.companies.factory import default_company_provider; cp = default_company_provider(); print(cp.get_by_nse_symbol('RELIANCE'))"
```

Then in the new Claude Code session:

1. **Read these in order** (the agent should do this without prompting; if it doesn't, prompt it):
   - `docs/CONTEXT.md` (5 min orientation)
   - `docs/STATUS.md` (current state — should say "Phase 2 pending")
   - `docs/SPEC.md` §3.1 (FR-INGEST-* — what the fetchers must do)
   - `docs/SPEC.md` §6.4.3 (BSE/NSE API contracts)
   - `docs/VERIFICATION.md` §Phase 2 (exit criteria)
   - This handover section

2. **Tell the agent**: "Start Phase 2. Copy and clean brain's BSE_fetcher.py and NSEFetcher.py per the handover doc. Don't import brain at runtime — port the logic. Keep only notification-related methods. Conform to ExchangeFetcher ABC. Build normalizer.py with date format support. Smoke test live."

3. **Tool budget for Session 2**: ~80 calls planned for Phase 2. If you also want to start Phase 3 (poller, dedup, cross-exchange) in the same session, budget ~200 calls total.

### Watch-outs for Session 2

- **The `.venv` is at `G:\market_notification\.venv\Scripts\python.exe`.** When the agent runs `python -m pytest`, it must use the venv's python — `pytest` directly may pull system Python which doesn't have the package installed. The Phase 1 smoke script uses absolute path explicitly.
- **Pyright will show "import not resolved" warnings** for `market_notification.*` modules in any new files. These are FALSE POSITIVES — the package IS installed (`pip install -e .`). Pyright in the editor doesn't see the venv. Tell the agent to ignore them.
- **`bash` and `Bash` tool spawn fresh shells** without venv activation. Use PowerShell with absolute path to `.venv\Scripts\python.exe` for runs that need the package.
- **brain's BSE_fetcher / NSEFetcher have many unused methods.** Don't blindly copy — surface only the 3-5 notification-related ones.
- **NSE has cookie warm-up complexity.** Brain handles it via session keep-alive. The cleaned version must preserve this.
- **Date format zoo**: there are 2+ formats per source. brain's lists are at `notification_poller.py` lines ~55-65 (`_NSE_DATE_FMTS`, `_BSE_DATE_FMTS`) — copy them verbatim into the normalizer.

### Phase 2 reference materials in brain

| File | Path | Purpose |
|---|---|---|
| BSE fetcher | `G:\brain\exchange_util\BSE\BSE_fetcher.py` (23 KB) | The whole file — pull notification methods only |
| NSE fetcher | `G:\brain\exchange_util\NSE\NSEFetcher.py` (78 KB) | Pull notification + auth methods only |
| Existing poller (uses both) | `G:\brain\exchange_util\notification_poller.py` (62 KB) | Reference for how the two are wired together |
| Date format constants | Same poller file, lines ~55-65 | Copy verbatim |
| Symbol API patterns | `G:\brain\exchange_util\NSE\nse_notification.py` (8 KB) | Symbol-specific endpoints, may inform `fetch_for_date` |

### Phase 2 verification target (from VERIFICATION.md)

- [ ] BSE fetch returns ≥10 rows during market hours.
- [ ] NSE fetch returns ≥10 rows during market hours.
- [ ] Each row passes the normalizer schema check (RawNotification DTO valid).
- [ ] Date parsing handles all observed formats (no `None` from `_parse_dt`).
- [ ] No HTTP 429/401/403 errors in 5-minute light-poll run.
- [ ] Artifacts: `verification/phase_2_results/{sample_bse_response.json, sample_nse_response.json, normalized_sample.json}`.

### One thing to double-check at Session 2 start

The `Pyright` diagnostic stream from the harness has been spammy with false-positive "import not resolved" warnings. They're harmless. If the new agent gets distracted fixing them, just tell it: *"Ignore Pyright import-resolution warnings; the package is installed but Pyright in this harness doesn't see the venv. The runtime tests are the source of truth."*

---

## Session 2 → Session 3  (2026-05-07)

### Why handing over

- Tool calls used: ~140 / 1000 (well under the cap; clean phase boundary).
- Clean boundary: **Phase 2 verified AND Phase 3 verified**. Both shipped + smoke tested live this session.
- Natural pause point — Phase 4 (Junk Filter) is a fresh, well-bounded scope.

### What was completed this session

- **Phase 2 verified**: cleaned BSEFetcher (268 LOC) + NSEFetcher (320 LOC) + pure normalizer (195 LOC). Live smoke during market hours: BSE 420 rows, NSE 50 rows, 0 date-parse failures. 47 normalizer unit tests pass.
- **Phase 3 verified**: SqlaNotificationRepo + SqlaPollStateRepo + Poller orchestrator (230 LOC) + cross_exchange (100 LOC) + company_resolver (70 LOC). Live smoke pass 1 inserted 147 rows, pass 2 inserted 0 (dedup verified). Cross-exchange grouping verified on mocked fixture. `scripts/run_poller.py` now real (no longer stub). Total suite: 96 passing.

### What is the exact next step

**Start Phase 4 — Junk Filter.**

Implement the regex/keyword junk filter that the Poller currently doesn't apply (it's TODO -- the `is_useless` and `junk_rule_id` columns exist in the schema but the poller doesn't populate them).

Files to produce (per `architecture.md` §filter and `SPEC.md` §3.4 FR-FILTER-*):

| File | Purpose |
|---|---|
| `src/market_notification/filter/regex_engine.py` | Concrete `FilterEngineBase` impl. Loads rules from `notification_filter_rules` table + `config/filter_rules.json` seed. Compiles regex on load. Returns `FilterMatch` (rule_id, action, reason). |
| `src/market_notification/filter/loader.py` | Reads `config/filter_rules.json` once, upserts into DB (idempotent on `uq_filter_rule_key`). |
| `src/market_notification/db/repositories/filter_rule_repo_sqla.py` | Concrete `FilterRuleRepoBase` impl (list_active + add + deactivate). |
| Wire into `Poller._ingest_one`: after company resolution, before insert, call filter engine. Set `is_useless`, `junk_rule_id`, and if matched set `pipeline_status='ignored_junk'`. |
| `tests/unit/test_filter_engine.py` | Rule match, miss, regex compile, perf benchmark per VERIFICATION.md §Phase 4. |
| `scripts/_phase4_smoke.py` | Apply filter to backfilled or freshly-polled rows; sample 200; assert >=10% flagged. |

Phase 4 exit criteria (from VERIFICATION.md):
- Rules loaded from `config/filter_rules.json`
- Sample 200 raw notifications: >=10% flagged as junk (sanity floor)
- Performance: 1000 notifications classified in <1s

Brain has a junk-rules source to seed from -- per CONTEXT.md: `G:\brain\<filter rules table>` (one-time import). Find it via:

```powershell
sqlite3 "G:\brain\data\notifications.db" ".tables" | grep -i filter
```

### Open subtasks for the rest of Phase 4

- [ ] Locate brain's filter rules table; document the schema mapping in `design-decisions.md`
- [ ] Write `config/filter_rules.json` seed (export from brain or use the `.example` file as starting point)
- [ ] Implement `FilterRuleRepoSqla` (3 methods)
- [ ] Implement `RegexFilterEngine` with compile-once regex cache
- [ ] Wire into `Poller._ingest_one` between company resolution and insert
- [ ] Unit tests: rule match/miss, regex compile errors, perf benchmark
- [ ] `scripts/_phase4_smoke.py` — sample 200 backfilled rows, capture counts
- [ ] Update STATUS.md, write `verification/phase_4_results/summary.md`

### Decisions made this session

- **Repositories stub later-phase methods with `NotImplementedError`** — avoids reaching ahead while satisfying the ABC. Documented in `phase_3_results/summary.md`.
- **Dedup is two-layered**: app-level `exists_by_natural_key` short-circuit + DB UNIQUE safety net.
- **Cosine similarity hand-rolled** (Counter + dot product); no sklearn dep added.
- **Cross-exchange duplicate-dropped rows get `pipeline_status='ignored_cross_exchange'`** so the dispatcher can skip them in Phase 10.

### Open questions raised this session

None new. Everything aligned with prior plan.

### Files modified this session

**Source code (Phase 2):**
- `src/market_notification/exchange/normalizer.py` (new)
- `src/market_notification/exchange/bse_fetcher.py` (new)
- `src/market_notification/exchange/nse_fetcher.py` (new)

**Source code (Phase 3):**
- `src/market_notification/db/repositories/notification_repo_sqla.py` (new)
- `src/market_notification/db/repositories/poll_state_repo_sqla.py` (new)
- `src/market_notification/poller/cross_exchange.py` (new)
- `src/market_notification/poller/company_resolver.py` (new)
- `src/market_notification/poller/poller.py` (new)
- `scripts/run_poller.py` (rewritten from stub)

**Tests:**
- `tests/unit/test_normalizer.py` (47 tests, new)
- `tests/integration/test_fetchers_smoke.py` (live, gated)
- `tests/unit/test_cross_exchange.py` (16 tests, new)
- `tests/unit/test_dedup.py` (8 tests, new)
- `tests/integration/test_poller_short_run.py` (4 mocked + 1 live gated, new)

**Scripts (helpers):**
- `scripts/_phase2_smoke.py`
- `scripts/_phase3_smoke.py`

**Docs:**
- `docs/STATUS.md` updated (Phase 2 + Phase 3 verified, Phase 4 next)
- `docs/HANDOVER.md` (this section)

**Verification artifacts:**
- `verification/phase_2_results/{sample_bse_response.json, sample_nse_response.json, normalized_sample.json, summary.md}`
- `verification/phase_3_results/{poll_run.txt, dedup_proof.txt, poll_state_after.csv, cross_exchange_groups.csv, summary.md}`

### Don't-forget checklist

- [x] STATUS.md updated to "Phase 4 — Junk Filter (pending)"
- [x] Phase 2 + Phase 3 verification artifacts saved
- [x] design-decisions.md unchanged this session (no new decisions worth recording)
- [x] QUESTIONS.md unchanged (no new opens)
- [ ] Session log: `python ~/.claude/scripts/session_log.py end --session 2` (run on session close)

### Tool budget at handoff

Used ~140 / 1000 in Session 2. Session 3 has ~860 remaining. Phase 4 (~100 calls) + Phase 5 Taxonomy/Classifier prep (~150 calls) fit comfortably.

### Watch-outs for Session 3

- **Brain filter-rules schema**: don't blindly copy. Check column types and ensure they align with our `notification_filter_rules` schema (rule_type, pattern, source, action, created_by, confidence, reason, is_active).
- **Filter perf**: pre-compile regex on load. Don't re-compile per call. Verification target is <1s for 1000 notifications.
- **Filter ordering**: rules are applied in priority order. If a rule with `action='block'` matches, we stop scanning for that row. SPEC §3.4 has the ordering.
- **The Poller currently doesn't call the filter engine** -- adding it is one of the wiring tasks above. The notification's `is_useless` flag is set by the poller, not by the worker; this matters for Phase 10 dispatcher (junk rows skip the pipeline).
- **In-memory engine injection trick** (used in `tests/integration/test_poller_short_run.py::fresh_db` fixture): `os.environ["MN_DB__URL"]=...` does NOT override TOML in the cached `get_settings()`. Direct injection of the engine into `session_mod._engine` is the cleanest test approach.

### Next-session quick-start

```powershell
cd G:\market_notification
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pytest tests/ -v --ignore=tests/integration/test_fetchers_smoke.py
# 96 should pass; 1 skipped
```

Then in the new session:

1. Read `docs/CONTEXT.md`, `docs/STATUS.md`, this handover section.
2. Read `docs/SPEC.md` §3.4 FR-FILTER-*.
3. Read `verification/phase_3_results/summary.md` if you want to see the Phase 3 wiring.
4. Locate brain's filter-rules table.
5. Begin Phase 4 per the subtasks above.
