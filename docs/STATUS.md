# Market Notification System — Build Status

> **Updated:** 2026-05-09 (Session 4 — Phase 8 in progress; offline smoke green)
> **Maintained by:** the active agent during build.

---

## Current state

**Phase:** **Phase 8 — Gemma Summarizer** (offline + live smoke green; verified)
**Last completed step:** Phase 8 verified end-to-end. Implementation: `summarizer/{schema, prompts/summarize_v1, gemma_summarizer, queue_retry}.py` + `update_summary` on SQLA repo. Pure schema validator (FR-SUMM-002) is reused from orchestrator AND smoke-harness DB replay so "stored envelope is valid" is asserted post-persist. Stricter-prompt retry (FR-SUMM-002, max 2) handles malformed JSON inline; queue_retry (FR-SUMM-006, 30s/retry_max=3) handles Ollama-down inline. Deferred-doc routing (FR-SUMM-004) uses a separate prompt that withholds the body, with a post-processing safety net that guarantees the FR-SUMM-002 tag list contains the row's `deferred_doc_type`. **Operational alignment with `D:\gemma-retrieval`:** identical Ollama daemon (127.0.0.1:11434), identical model (`gemma4-zenflow-moe:latest`), identical knobs (`temperature=0.1`, `keep_alive=24h`, `request_timeout=300s`, retry_max=3, retry_delay=30s). Offline smoke (6 fixtures): 4 happy → `deep_dive_pending`, 2 deferred → `done_deferred`, 1 strict-retry recovers in 2 attempts, 1 Ollama-down → `summarize_dead`. **Live smoke (4 fixtures over real Gemma):** 4/4 ok, p50 **14.97s** / p95 **17.14s** (FR-SUMM-007 budget 30s), verbatim figures **10/10** (FR-SUMM-003 PASS), 0 fallbacks (FR-SUMM-002 PASS), deferred-tag present on the earnings fixture (FR-SUMM-004 PASS). Total test suite: **325 passing**, 2 live skipped (31 new in this phase).
**Next step:** Phase 9 — Backfill from Brain (FR-INGEST-009..010, NFR-REL-003).

**Known follow-ups (non-blocking):**
- Prompt v1.1 candidate — model over-applies `deferred_doc_tags=['large_misc']` to non-deferred multi-section announcements. Tighten the rule in `summarize_v1.py` to "Use `large_misc` only when the announcement IS the long document". The DB row's `deferred_doc_type` column is unaffected so pipeline routing is correct; only the in-envelope tag list is noisy.
- Live verification artifacts: `verification/phase_8_results/live_summary.md` and `live_per_row_envelope.jsonl`.
- Transient Ollama hiccup observed during the first live attempt (connection forcibly closed mid-call, then daemon-up but model unloaded). Recovered cleanly on retry; queue_retry would have handled this in production. Risk Q-3 (Ollama queue contention with brain/gemma-retrieval) remains open.

---

## Phase progress

| Phase | Title | Status | Started | Completed | Verification |
|---|---|---|---|---|---|
| 0 | Bootstrap & Foundation | **verified** | 2026-05-07 | 2026-05-07 | phase_0_results/ |
| 1 | Companies & Read-Only Data | **verified** | 2026-05-07 | 2026-05-07 | phase_1_results/ |
| 2 | Exchange Fetchers | **verified** | 2026-05-07 | 2026-05-07 | phase_2_results/ |
| 3 | Poller, Dedup, Cross-Exchange | **verified** | 2026-05-07 | 2026-05-07 | phase_3_results/ |
| 4 | Junk Filter | **verified** | 2026-05-07 | 2026-05-07 | phase_4_results/ |
| 5 | Taxonomy + Gemma Classifier | **verified** | 2026-05-07 | 2026-05-07 | phase_5_results/ |
| 6 | Priority Engine | **verified** | 2026-05-07 | 2026-05-07 | phase_6_results/ |
| 7 | PDF Pipeline | **verified** | 2026-05-07 | 2026-05-07 | phase_7_results/ |
| 8 | Gemma Summarizer | **verified** | 2026-05-09 | 2026-05-09 | phase_8_results/ |
| 9 | Backfill from Brain | pending | — | — | — |
| 10 | Pipeline Dispatcher | pending | — | — | — |
| 11 | gemini-rr Deep-Dive | pending | — | — | — |
| 12 | Streamlit UI | pending | — | — | — |
| 13 | Operations & Soak | pending | — | — | — |

Status values: `pending` | `in_progress` | `verified` | `blocked` | `deferred`.

---

## Phase 0 — Bootstrap & Foundation — checklist

### Documentation (100% — all 8 files)

- [x] `docs/PLAN.md` — master plan with 14 phases, schema, sequence diagrams
- [x] `docs/SPEC.md` — formal numbered FR/NFR contract + traceability matrix
- [x] `docs/architecture.md` — module diagrams, ABCs, data flow, swappability
- [x] `docs/STATUS.md` — this file
- [x] `docs/CONTEXT.md` — 5-min orientation for new sessions
- [x] `docs/QUESTIONS.md` — 10 open items captured
- [x] `docs/VERIFICATION.md` — per-phase verification protocol
- [x] `docs/HANDOVER.md` — session-handoff template
- [x] `docs/design-decisions.md` — answers + 14 in-build decisions D-07..D-20

### Project skeleton (100%)

- [x] Directory tree (35 dirs)
- [x] `__init__.py` in every Python package (25 files)
- [x] Top-level files: `pyproject.toml`, `README.md`, `.gitignore`, `.env.example`, `alembic.ini`
- [x] `config/default.toml` — full layered settings
- [x] `config/filter_rules.json.example`
- [x] `verification/README.md` (per-phase artifact map)

### ABCs (100%)

- [x] `exchange/base.py` — `ExchangeFetcher`, `RawNotification`
- [x] `companies/base.py` — `CompanyProvider`, `CompanyDTO`, `FundamentalsDTO`, `PriceSeriesDTO`
- [x] `filter/base.py` — `FilterEngineBase`, `FilterMatch`
- [x] `classifier/base.py` — `Classifier`, `ClassificationResult`
- [x] `priority/base.py` — `DeterministicPriority`, `LlmPriorityOverride`, `PriorityResult`
- [x] `attachments/base.py` — `AttachmentDownloader`, `PdfTextExtractor`, `PdfImageExtractor`, `LinkResolver`
- [x] `summarizer/base.py` — `Summarizer`, `SummaryResult` with extended schema (KeyFigure/Person/Date/Link)
- [x] `deep_dive/base.py` — `DeepDive`, `PromptBuilder`, `DeepDiveResult`
- [x] `pipeline/states.py` — `PipelineStatus` enum + ACTIVE/PENDING/FAILED/TERMINAL sets
- [x] `db/repositories/base.py` — `NotificationRepoBase`, `FilterRuleRepoBase`, `PollStateRepoBase`, `JournalRepoBase`

### Core infrastructure (100%)

- [x] `db/models.py` — full schema per PLAN.md §5 (7 tables, all indexes)
- [x] `db/session.py` — engine factory with WAL pragmas
- [x] `db/migrations/env.py` — Alembic env reading from Settings
- [x] `db/migrations/script.py.mako` — revision template
- [x] `db/migrations/README.md` — usage notes
- [x] `config/settings.py` — pydantic-settings with TOML + .env + MN_* env vars
- [x] `ops/logging.py` — rotating file (10MB×10) + stdout, with `--selftest`

### Scripts (100% as stubs)

- [x] `scripts/bootstrap_db.py` — real (creates DB + tables via `Base.metadata.create_all`)
- [x] `scripts/run_poller.py` — stub (real in Phase 3)
- [x] `scripts/run_workers.py` — stub (real in Phase 10)
- [x] `scripts/run_ui.py` — launcher (UI app real in Phase 12)
- [x] `src/market_notification/ui/app.py` — placeholder (real in Phase 12)

### Tests (60% — written but not yet executed)

- [x] `tests/conftest.py` — in-memory engine + session fixtures
- [x] `tests/unit/test_db_models.py` — 6 tests (table presence, insert, transition, dedup, journal, filter rule, poll state)
- [ ] **Run** `pytest tests/unit/ -v` — needs `pip install -e .[dev]` first
- [ ] Capture pytest output to `verification/phase_0_results/pytest_output.txt`

### Phase 0 exit verification (DONE 2026-05-07)

User ran:
- `python -m venv .venv && pip install -e .[dev]` ✓
- `python -m scripts.bootstrap_db` ✓ — 7 tables created, WAL mode active
- `pytest tests/unit/ -v` ✓ — schema tests pass

Artifacts captured:
- `verification/phase_0_results/db_schema.txt` — full schema dump (196 lines, 7 tables, 17 indexes)
- `verification/phase_0_results/log_excerpt.txt` — bootstrap log lines

---

### Phase 1 — Companies & Read-Only Data — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `companies/csv_source.py` — CsvCompanyProvider (identity + static mcap)
- [x] `companies/screener_original_source.py` — ScreenerOriginalCompanyProvider (prices + dynamic mcap)
- [x] `companies/composite.py` — CompositeCompanyProvider (chain)
- [x] `companies/factory.py` — default_company_provider() singleton
- [x] `tests/unit/test_company_provider.py` — 14 tests passing
- [x] `scripts/_phase1_smoke.py` — live verification harness

**Verification results:**
- [x] 5,249 companies loaded from CSV (header row excluded)
- [x] 11/11 sample lookups (RELIANCE, ITC, TCS, HDFCBANK, INFY, SBIN, ICICIBANK, KOTAKBANK, BHARTIARTL, AXISBANK, 20MICRONS) — all resolved
- [x] BSE/NSE/ISIN/company_id all succeed
- [x] RELIANCE price series — 35 bars for 60-day window
- [x] Coverage: ISIN 100% · mcap 100% · BSE 87.4% · NSE 53.5%

**Decisions / open items:**
- Q-7 RESOLVED — pv_df_check ported into `screener_original_source.py`
- Q-11 RESOLVED — fundamentals source is Screener_original `stockDirectory/{id}/`, not brain's empty tables
- Deviation 1 REVERTED (was based on a wrong-name grep)
- Deviation 2 ACTIVE — brain DB role reduced to corporate_actions + notification backfill only

---

### Phase 2 — Exchange Fetchers — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/exchange/normalizer.py` (195 LOC) — pure functions, no I/O
- [x] `src/market_notification/exchange/bse_fetcher.py` (268 LOC) — paged AnnSubCategoryGetData + AttachLive/AttachHis fallback
- [x] `src/market_notification/exchange/nse_fetcher.py` (320 LOC) — warmup probe + optional Playwright bootstrap + getCorporateInfo + corporate-announcements
- [x] `tests/unit/test_normalizer.py` — 47 unit tests (date formats, both NSE schema variants, BSE attachment URL synthesis)
- [x] `tests/integration/test_fetchers_smoke.py` — gated by `MN_RUN_LIVE_INTERNET=1`
- [x] `scripts/_phase2_smoke.py` — verification harness, writes 3 artifacts

**Verification results (live smoke 2026-05-07):**
- BSE: 420 rows fetched for today, 420/420 normalized cleanly, 0 date-parse failures
- NSE: 50 rows fetched (`fetch_latest`), all schema-valid
- 0 HTTP 4xx/5xx errors during run
- 68 unit tests passing in 0.81s

**Decisions:**
- Playwright is optional (lazy import); NSE raises clear RuntimeError if needed but missing
- BSE `fetch_latest(n)` delegates to `fetch_for_date(today)[:n]` (no native latest endpoint)
- BSE attachment URLs synthesized from `ATTACHMENTNAME` filename + `BSE_ATTACH_BASE_LIVE`

---

### Phase 7 — PDF Pipeline — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/attachments/downloader.py` (~240 LOC) — `HttpAttachmentDownloader` with per-source fetcher registry (BSE/NSE sessions reused), pre-download filename dedup + post-download MD5 byte-hash dedup, oversize cap, path-traversal-safe filename derivation, injectable transport for tests
- [x] `src/market_notification/attachments/pdfplumber_extractor.py` (~95 LOC) — `PdfPlumberExtractor` over first 20 pages; reports the FILE total page count (not the read count) so the >20-page deferred-tag rule still fires; `is_empty()` predicate for the orchestrator
- [x] `src/market_notification/attachments/gemma_vision_extractor.py` (~180 LOC) — `GemmaVisionExtractor`; PyMuPDF render PDF page → PNG → base64 → Ollama chat with `images=[...]` + `think=False` (consistent with Phase 5 reasoning-model fix); two caps (`max_pages` for render, `max_pages_render` for ship); per-page `BLANK` filter; transport injectable
- [x] `src/market_notification/attachments/deferred_tagger.py` (~170 LOC) — `DeferredDocTagger` with precedence ai_category > exchange category > headline/body keyword > filename keyword > pdf-text-head keyword > page-count fallback (>20 pages → `large_misc`); 6 supported tags
- [x] `src/market_notification/attachments/link_resolver.py` (~210 LOC) — `HttpHeadLinkResolver`; URL_REGEX strips trailing punctuation; SSRF guard drops RFC1918/loopback/link-local before probe; HEAD with GET fallback for servers that reject HEAD; `_infer_kind` orders office MIME before html (else `presentationml` substring matches `xml` and mis-classifies)
- [x] `src/market_notification/attachments/service.py` (~330 LOC) — `AttachmentService.run_for(notification_id)` orchestrator: download → tag → extract → optional vision → optional link resolve → persist via `update_attachment` → advance pipeline_status → journal. Skips vision + link resolution when `deferred_doc_type` is set (per FR-ATTACH-004 — body must NOT feed summarizer). Empty extractions advance to `summarize_pending` with `last_error="empty_extraction: ..."` rather than failing
- [x] `src/market_notification/db/repositories/notification_repo_sqla.py` — added `update_attachment(payload)` writing 9 columns (download_status, local_path, pdf_extracted_text, pdf_image_summary, pdf_pages, pdf_text_md5, deferred_doc_type, gemma_external_links, last_error) in a single round-trip
- [x] `tests/unit/test_attachment_downloader.py` — 13 tests (no_url skip, fresh download, filename fallback chain, pre-download dedup, post-download MD5 dedup, transport failure, oversize, fetcher registry, path-traversal sanitization, MD5 stability)
- [x] `tests/unit/test_pdfplumber_extractor.py` — 7 tests (real PDFs built with PyMuPDF; happy path, blank, max_pages cap, missing file, corrupt, configurable threshold)
- [x] `tests/unit/test_gemma_vision_extractor.py` — 6 tests (transport stubbed; renders + concats, page cap, BLANK filter, transport failure, missing PDF, corrupt PDF)
- [x] `tests/unit/test_link_resolver.py` — 9 tests (regex correctness, mime classification, failed HEAD, max_links cap, dedup-preserves-order, SSRF guard parametrized over 10 cases)
- [x] `tests/unit/test_deferred_tagger.py` — 14 tests (precedence chain across all 6 tag types + page-count threshold boundary)
- [x] `tests/unit/test_attachment_service.py` — 7 end-to-end tests against in-memory DB (happy path text, vision fallback, deferred skips body+vision, download failure, no_url, link persistence, empty-extraction note)
- [x] `scripts/_phase7_smoke.py` — offline harness; synthesizes 4 fixture PDFs, runs full service, writes 4 artifacts

**Verification results (smoke 2026-05-07):**
- 4 fixtures shipped, 4/4 covered (100%; target ≥90%): text_capex → pdfplumber 166 chars; blank_scan → gemma_vision 106 chars (vision fallback fired); with_link → pdfplumber 99 chars + 1 outbound link probed; long_doc (25 pages) → pdfplumber + `deferred_doc_type='large_misc'`
- All 4 advance to `pipeline_status='summarize_pending'`
- Deferred tagger demo: 7 cases firing across all 5 explicit tag types + page-count fallback
- Test suite: **294 passing, 2 live skipped** (70 new in this phase)

**Decisions:**
- **Fetcher registry uses Protocol structural typing.** The downloader accepts `Mapping[str, _BytesFetcher]` where `_BytesFetcher` is a `Protocol` with one method (`fetch_attachment`). Tests pass plain stubs without inheriting from `ExchangeFetcher`; production passes `{'BSE': BseFetcher(), 'NSE': NseFetcher()}` and the existing warmed sessions get reused — no duplicate retry/cookie logic.
- **Two-stage MD5 dedup.** Pre-download: filename collision → trust existing. Post-download: byte-hash scan in the company directory catches BSE/NSE re-publishing the same PDF under different GUIDs (a real pattern in brain's history). One MD5 walk per company dir is the cost; net saves on disk + DB churn.
- **Vision pass two caps.** `max_pages` bounds rendering work (cpu/memory); `max_pages_render` bounds Ollama traffic (network/quota). Default render cap is 20 (matches text); default ship cap is 5 (vision is ~30× more expensive than text).
- **Deferred-doc precedence.** Classifier verdict (`ai_category`) wins over keyword matches because the classifier already disambiguated headlines like "Outcome of Board Meeting — Audited Financial Results" that pure regex would mis-route to `earnings`. Exchange category beats keyword for the same reason. Page-count is the lowest-precedence fallback.
- **Deferred docs DO advance to `summarize_pending`.** FR-ATTACH-004 says "body MUST NOT feed summarizer" — but headline + metadata still go downstream. The service nulls `pdf_extracted_text` + `pdf_image_summary` on persist when `deferred_doc_type` is set; Phase 8 reads `deferred_doc_type` and generates a headline-only summary. Status transition stays the same.
- **SSRF protection at the link-resolver boundary.** Drop non-http(s), RFC1918, loopback, link-local *before* any HEAD call. Includes IPv6 loopback and the AWS metadata IP `169.254.169.254` — handled via `ipaddress.ip_address(...)` introspection rather than substring checks.
- **MIME ordering matters for `_infer_kind`.** Office MIME types are checked before html/xml because `application/vnd.openxmlformats-officedocument.presentationml.presentation` contains the substring `xml` and would otherwise mis-classify all PowerPoint links as `html`. Discovered + fixed via `test_classifies_html_and_office_docs`.
- **Empty-extraction is non-fatal.** When both extractors return empty + the doc isn't deferred, we still advance to `summarize_pending` (the summarizer can use headline + body) but write an `empty_extraction:` note into `last_error` so the Health UI flags it. Failing the row in this case would lose information.
- **Word-boundary trap in deferred regex.** `\bconcall\b` does NOT match `Q1FY26_concall_transcript.pdf` because `_` is a word character — no `\b` boundaries around `concall` inside underscores. Fixed by dropping `\b` for unambiguous compound keywords (`concall`, `audio_recording`).

---

### Phase 6 — Priority Engine — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/priority/thresholds.py` (220 LOC) — pure regex + arithmetic helpers; faithfully ports brain's regex set (commissioning/proposed/significant/special-dividend/newspaper/USFDA-VAI/USFDA-OAI/SEBI-ban/auditor-qualification/rating-upgrade/rating-downgrade/WOS-merger/buyback-procedural/QIP-procedural/promoter/CEO-CFO). Adds `extract_amount_cr` (with ₹ + cr/Cr/crore variants) and `pct_of` (None-safe, divide-by-zero safe).
- [x] `src/market_notification/priority/rubric.py` (130 LOC) — `CATEGORY_PRIORITY` dict (53 entries; all VALID_CATEGORIES + Uncategorized) + `BASE_SCORE` mapping + `default_for(category)` + `bucket_for_score(score)` + `assert_rubric_complete()` import-time check (crashes the module if a taxonomy category is missing from the rubric)
- [x] `src/market_notification/priority/deterministic.py` (260 LOC) — `DeterministicScorer` composing rubric + thresholds via 22 modular adjusters (`_adj_capex_stage`, `_adj_capex_pct_of_mcap`, `_adj_capex_absolute`, `_adj_order_win_pct_of_sales`, `_adj_order_win_keyword`, `_adj_acquisition_pct_of_sales`, `_adj_ofs_mcap`, `_adj_management_change_keyword`, `_adj_auditor_qualification`, `_adj_credit_rating`, `_adj_dividend`, `_adj_quarterly_results_special`, `_adj_demerger`, `_adj_usfda`, `_adj_tax_legal_pct`, `_adj_sebi`, `_adj_merger_third_party`, `_adj_buyback_procedural`, `_adj_qip_procedural`, `_adj_warrant_promoter`, `_adj_divestiture`, `_adj_rpt`). Each adjuster is 5–10 lines and independently testable.
- [x] `src/market_notification/priority/llm_override.py` (220 LOC) — `GemmaLlmPriorityOverride` with injectable transport + `think=False` (matches Phase 5 fix). Falls back to deterministic on transport error / unparseable JSON / invalid bucket; the failure note lands in `reasons` so the journal preserves it.
- [x] `src/market_notification/priority/prompts/override_v1.py` (90 LOC) — `priority_override_v1.0-2026-05-07` prompt; renders system+user pair with the deterministic verdict embedded so the model can confirm/upgrade/downgrade.
- [x] `src/market_notification/priority/service.py` (170 LOC) — `PriorityService.run_for(notification_id)` orchestrator; pulls notification + company + fundamentals, runs deterministic, then LLM override (if configured), persists via `repo.update_priority`, advances pipeline_status `priority_pending` → `summarize_pending`, journals the transition.
- [x] `src/market_notification/db/repositories/notification_repo_sqla.py` — real `update_priority` (writes `det_priority`/`det_score`/`det_reasons` and `ai_priority`/`ai_priority_score`/`ai_priority_reasons`; reasons JSON-encoded)
- [x] `tests/unit/test_priority_thresholds.py` — 26 tests (regex matchers, amount extraction with ₹/INR/Rs/cr variants, pct_of None+zero handling, QIP-procedural compound rule)
- [x] `tests/unit/test_priority_rubric.py` — 20 tests (taxonomy coverage, default lookup, bucket boundaries 0/1/39/40/69/70/95)
- [x] `tests/unit/test_priority_deterministic.py` — 22 tests covering every special rule (newspaper-ad ignored, auditor-qualification floor 85, USFDA OAI/VAI, SEBI ban, capex stages + absolute + pct, order-win pct + keyword, acquisition pct, OFS mcap, credit-rating up/down, special dividend, WOS vs third-party merger, QIP procedural, tax demand pct, demerger internal transfer, buyback procedural, score clamp 0..100)
- [x] `tests/unit/test_priority_llm_override.py` — 6 tests (upgrade, downgrade, confirm-keeps-det-score, invalid bucket → fallback, unparseable JSON → fallback, transport error → fallback)
- [x] `tests/unit/test_priority_service.py` — 3 tests (happy path persists + status transitions + journals; LLM override path produces ai_priority distinct from det_priority; newspaper-ad → ignored end-to-end)
- [x] `scripts/_phase6_smoke.py` — distribution + fixture + LLM override harness; 4 artifacts

**Verification results (smoke 2026-05-07):**
- Special-rule fixtures: 5/5 produce correct buckets (newspaper-ad → ignored; auditor-qualification → important@85; USFDA OAI → important; capex-commissioned → important; acquisition pct-of-rev → important)
- LLM override fired on 2 of 3 demo fixtures (upgrade Acquisition→important, downgrade Quarterly Results notice→normal; the third confirmed deterministic)
- Distribution on 53 Phase-5-classified rows: **important 15.09% (target 5–15%, just over) · medium 24.53% (in band) · normal 60.38% (in band) · ignored 0.0% (empty by design — junk filter strips those rows before classification)**. 3 of 4 buckets in band; the 1000-row criterion is gated by the Phase 9 backfill and re-verified there.
- Test suite: **224 passing, 2 live skipped** (79 new in this phase)

**Decisions:**
- **Modular adjuster pattern.** Brain's monolithic `determine_priority()` is 220 lines of branchy if/elif. Refactored into a tuple of 22 small `_adj_*(ctx)` functions that each handle one rule (e.g. `_adj_capex_commissioned`). Each takes a mutable `_ScoreCtx` and may add/subtract from the score plus push a human reason. Net: every rule is a 5–10 line function with its own test, and adding a new rule is a 3-line append plus one fixture. Output is bit-identical to brain's (same regexes, same magic numbers).
- **LLM override carries failure notes through `reasons`, never raises.** Transport error / unparseable JSON / invalid bucket all return the deterministic result with a `override_skipped: ...` line appended. The journal preserves the failure mode for debugging without polluting `ai_priority_*` with bad data.
- **Score consistency on bucket-confirm.** When the LLM confirms the deterministic bucket, we keep the deterministic score (preserving its rich threshold-based reasoning). When the LLM flips the bucket, we use the new bucket's base score. Avoids a confusing "important@45" or similar contradictions.
- **Status transition `priority_pending` → `summarize_pending`.** PLAN.md §3.2 has an intermediate `attachment_pending` step but `pipeline/states.py` already collapsed that into the dispatcher's responsibility (Phase 10). For Phase 6's standalone PriorityService we move directly to `summarize_pending`; the dispatcher will re-route to `attachment_pending` for rows with attachments.
- **Rubric completeness asserted at import.** `assert_rubric_complete()` runs once when `priority/rubric.py` loads. If a future taxonomy edit adds a category without a rubric entry, the import crashes loudly — preventing silent "I don't know what to do with this category, default to normal" behavior.
- **Distribution caveat.** PLAN says target bands "loosely match brain history". Our 53-row sample lands very close: 3 of 4 buckets in band, and the empty `ignored` bucket is correct (junk filter strips those rows before they reach the priority engine). Re-verify after Phase 9 backfill provides 1000+ historical rows.

---

### Phase 5 — Taxonomy + Gemma Classifier — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/classifier/taxonomy.py` (180 LOC) — `TAXONOMY` (10 groups, 53 categories) + `CATEGORY_TO_GROUP`, `VALID_CATEGORIES`, `validate_category` (canonical-group rewrite + legacy alias resolution), `taxonomy_as_text` for prompt rendering, `TAXONOMY_VERSION='v1.0-2026-05-07'`
- [x] `src/market_notification/classifier/prompts/classify_v1.py` (110 LOC) — pinned `PROMPT_VERSION='classify_v1.0-2026-05-07'`, system+user prompt builders, taxonomy injected at render-time so the prompt never drifts from the data, "Other" group hidden from the prompt to discourage Compliance-Filing fallback
- [x] `src/market_notification/classifier/llm_classifier.py` (270 LOC) — `GemmaLlmClassifier` with injectable `LlmTransport` (defaults to Ollama JSON-mode chat); parses + validates LLM output, coerces invalid categories to `Uncategorized` + `ai_category_source='fallback'`, advances pipeline_status `classify_pending` → `priority_pending` and journals the transition
- [x] `src/market_notification/db/repositories/notification_repo_sqla.py` — added `claim_next_for_classify()` (FR-CLASSIFY-002 latest-first; ORDER BY announced_at DESC, id DESC; transparent fallback when SQLite rejects FOR UPDATE) + real `update_classification()`
- [x] `src/market_notification/db/repositories/journal_repo_sqla.py` (75 LOC) — `SqlaJournalRepo.append` + `find_stale_in_status` (uses COALESCE(last_status_change_at, fetched_at) as freshness anchor)
- [x] `src/market_notification/pipeline/sla_monitor.py` (160 LOC) — `SlaMonitor.start/stop/check_once`; threshold + tick configurable; one journal entry per stuck row per breach period (re-alert only after row leaves classify_pending)
- [x] `tests/unit/test_taxonomy.py` — 14 tests (shape, lookup consistency, alias resolution, prompt-text rendering)
- [x] `tests/unit/test_classifier_prompt.py` — 9 tests (golden-snapshot markers, all real categories present in prompt, version pin, body truncation)
- [x] `tests/unit/test_classifier_llm.py` — 5 tests (happy path + status transition + journal, invalid category fallback, transport failure fallback, legacy alias path, latest-first claim)
- [x] `tests/unit/test_sla_monitor.py` — 4 tests (no-op when fresh, single-fire idempotency, status scope, threshold)
- [x] `scripts/_phase5_smoke.py` — offline harness; 5 artifacts
- [x] `scripts/_phase5_live_classify.py` — live Ollama probe vs brain's deterministic labels

**Verification results (smoke 2026-05-07):**
- Taxonomy: 10 groups, 53 categories, version `v1.0-2026-05-07`
- Offline classifier roundtrip: 20/20 rows advanced classify_pending → priority_pending; 0 fallbacks; 0.047s wall-clock
- SLA monitor: first tick = 1 new breach for the 12-min-old injected row; second tick = 0 new breaches (1 already_alerted); exactly 1 journal entry recorded
- Test suite: **145 passing, 2 live skipped** (32 new tests added in this phase)
- Live accuracy probe (`gemma4-zenflow-moe:latest`, 20 rows): **70% literal-match** vs brain's rule-based labels, **~85% effective** after manual review of the 6 "misses" (3 are Gemma-better-than-brain catch-all; 3 are genuine Compliance-Filing-fallback errors). Per-row median latency ~16s. See `verification/phase_5_results/live_accuracy_notes.md` for the full breakdown.

**Live-classifier gotcha discovered:** `gemma4-zenflow-moe` is a reasoning model. Without `think=False`, the entire `num_predict` budget gets consumed by hidden chain-of-thought and the visible content is empty (`done_reason='length'`, no exception). Fixed in `_default_ollama_transport` with `try/except TypeError` for older `ollama` clients lacking the param.

**Prompt evolution:** v1.0 → v1.1 — added explicit "routine compliance filings → Compliance Filing/Other" instruction. v1.0 deliberately hid Compliance Filing from the listing; turned out the model then over-used `Uncategorized` for routine admin filings, costing ~25 percentage points of agreement with brain. v1.1 keeps Compliance Filing hidden from the menu but explicitly enumerated in a fallback rule — best of both.

**Decisions:**
- `validate_category` always overrides the LLM-supplied group with the canonical group lookup; the model is allowed to be wrong about the group as long as it picks a real category
- Prompt deliberately omits the "Other" group from the listing to steer away from "Compliance Filing" fallback; the `Uncategorized` sentinel is reachable only via an explicit instruction
- Successful classification advances pipeline_status to `priority_pending` (not `priority_pending_classify` from PLAN.md §3.2 — we collapsed that intermediate state because the new state machine in `pipeline/states.py` doesn't model it; both classification + priority happen back-to-back in Phase 6 worker)
- LLM transport is injectable so the unit tests run without Ollama; the production default lazy-imports the `ollama` package
- SLA monitor uses `COALESCE(last_status_change_at, fetched_at)` so freshly-ingested rows that never transitioned still get measured against ingestion time
- Re-alert policy: one journal breach per row per stuck-period; we don't re-alert on every tick. When the row eventually leaves classify_pending and (hypothetically) re-enters, a fresh breach can fire
- A new `SqlaJournalRepo` was introduced in this phase rather than waiting for Phase 10 — both the classifier and the SLA monitor need it now

---

### Phase 4 — Junk Filter — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/filter/filter_engine.py` (175 LOC) — `RegexFilterEngine`, compiled-rule cache, 4 rule types (category/subcategory/headline_regex/keyword), source-scoped, invalid-regex tolerant
- [x] `src/market_notification/db/repositories/filter_rule_repo_sqla.py` (90 LOC) — `SqlaFilterRuleRepo` with explicit-NULL-aware idempotent `add()` (works around SQLite `UNIQUE NULL distinct` quirk)
- [x] `config/filter_rules.json` — 14 seed rules (7 brain system + 1 auto-learned + 1 user + 5 augmented) covering trading window, newspaper publication, share-cert administrative, AGM/EGM, regulatory boilerplate
- [x] `scripts/seed_filter_rules.py` — idempotent JSON-to-DB loader
- [x] Poller wiring: `Poller.__init__` accepts optional `filter_engine`; junk rows persist with `pipeline_status='ignored'` + `is_useless=1` + `junk_rule_id=<id>`; cross-exchange grouping is skipped for junk
- [x] `scripts/run_poller.py` builds `RegexFilterEngine(get_session)` by default
- [x] `tests/unit/test_filter_engine.py` — 13 tests (rule types, source scope, lifecycle, perf)
- [x] `tests/integration/test_filter_seed_and_poller.py` — 4 tests (seed loader, idempotency, poller wiring, backward-compat)
- [x] `scripts/_phase4_smoke.py` — verification harness, 4 artifacts

**Verification results (smoke 2026-05-07):**
- Rules seeded: 14 active (9 category, 1 subcategory, 1 keyword, 3 headline_regex)
- Sample classification: 31/158 rows flagged (**19.6%**, target ≥10%)
- Perf: 1000 evaluations in **0.004s = 254K evals/sec** (target ≥1000/sec; 250× headroom)
- Manual eyeball of 20 flagged: all genuinely junk (newspaper publication, postal ballot, trading window closure)
- Manual eyeball of 20 passed: real material disclosures (board meetings, dividend, results)
- 113 unit + integration tests passing (17 new in this phase)

**Decisions:**
- `pipeline_status='ignored'` per FR-FILTER-001 (NOT a new `'ignored_junk'` value); `is_useless=1` + `junk_rule_id` are the discriminators against the existing `'ignored_cross_exchange'` short-circuit
- Junk rows are persisted (FR-FILTER-005) so the UI's Ignored tab can surface them and offer un-ignore
- Junk rows skip cross-exchange grouping (no downstream consumer would use the pairing)
- `SqlaFilterRuleRepo.add()` does an explicit `IS NULL` lookup before insert because SQLite (and default Postgres) `UNIQUE` treat `NULL` as distinct — without this, re-seeding duplicated all rules with `source=None`
- Filter engine is constructor-injected on the Poller; omitting it preserves Phase 3 behavior, keeping all earlier tests green without modification

---

### Phase 3 — Poller, Dedup, Cross-Exchange — checklist (DONE 2026-05-07)

**Files shipped:**
- [x] `src/market_notification/db/repositories/notification_repo_sqla.py` (165 LOC) — SqlaNotificationRepo with idempotent insert
- [x] `src/market_notification/db/repositories/poll_state_repo_sqla.py` (60 LOC) — SqlaPollStateRepo (get + upsert)
- [x] `src/market_notification/poller/cross_exchange.py` (100 LOC) — pure cosine, find_match, assign_role
- [x] `src/market_notification/poller/company_resolver.py` (70 LOC) — wraps CompanyProvider with BSE/NSE/ISIN fallback
- [x] `src/market_notification/poller/poller.py` (230 LOC) — Poller (poll_once + start/stop threaded loop)
- [x] `scripts/run_poller.py` — real entrypoint (was stub)
- [x] `tests/unit/test_cross_exchange.py` — 16 tests
- [x] `tests/unit/test_dedup.py` — 8 tests
- [x] `tests/integration/test_poller_short_run.py` — 4 mocked + 1 live (gated)
- [x] `scripts/_phase3_smoke.py` — verification harness, captures 4 artifacts

**Verification results (live smoke 2026-05-07):**
- Pass 1: BSE fetched=100 inserted=97 dup=3 errors=0; NSE fetched=50 inserted=50 dup=0 errors=0
- Pass 2 (dedup): both 0 inserted, 150 dedups → dedup verified
- Poll state has BSE+NSE rows with status=idle
- Cross-exchange fixture test passes (1 group, primary + duplicate_dropped, ignored_cross_exchange)
- 96 tests passing (1 live skipped)

**Decisions:**
- Repositories stub later-phase abstract methods with `NotImplementedError` (avoids reaching ahead while satisfying ABC)
- Dedup is two-layered: app-level `exists_by_natural_key` + DB UNIQUE constraint
- Cosine similarity hand-rolled (Counter + dot product); no sklearn dep
- Cross-exchange grouping uses ±10min window query per candidate (FR-INGEST-005)
- Duplicate-dropped rows get `pipeline_status='ignored_cross_exchange'` to short-circuit pipeline

---

## Tool-call budget tracking (this session)

| Marker | Calls used (cum.) | Notes |
|---|---:|---|
| Initial survey + design-decisions | ~63 | Brain modules, priority rubric, GEMMA4 ref, gemini-rr §16, design Q&A |
| 8 planning docs incl. SPEC.md | ~80 | PLAN, SPEC, architecture, STATUS, CONTEXT, QUESTIONS, VERIFICATION, HANDOVER |
| Project skeleton (45+ files, ABCs, schema) | ~140 | Phase 0 deliverables |
| Phase 0 verification + artifacts | ~155 | DB schema dump, log excerpt |
| Phase 1 implementation + tests + smoke + artifacts | ~220 | 4 source files + 14 tests + smoke harness + 3 artifacts |
| **Estimated total this session** | **~220** | |

**Remaining budget:** ~780 calls.
**Handover trigger:** at ~700 calls. Phase 2 (~80 calls budgeted) fits comfortably.

---

## Decisions made this session (extending design-decisions.md)

See `PLAN.md` §14 for D-07..D-20. Most relevant:

- **D-07** SQLite + WAL now; Postgres-migration via repository pattern.
- **D-09** 3-process model (poller, worker, UI); worker single-threaded.
- **D-11** Gemma can both upgrade and downgrade priority; reasons logged.
- **D-13** Annual reports / investor presentations / earnings / ppt / credit notes are *tagged* but NOT summarized in this system — separate downstream pipeline.
- **D-16** Inject all fundamentals into deep-dive prompt; let Gemini decide relevance.
- **D-20** No git commits during build; `git init` + one consolidated commit at Phase 13 close.

---

## Live deviations from plan

- **Deviation 1** — REVERTED 2026-05-07. Was about price source; reverted after user pointed to correct file `pv_df_check.py`.
- **Deviation 2** — ACTIVE. Fundamentals source: Screener_original `stockDirectory/{id}/` (rich) instead of brain's `screener_essential.db` (empty tables). Brain role reduced. See `design-decisions.md` § Deviation 2.

---

## Risk dashboard

| Risk | Status | Last updated |
|---|---|---|
| Ollama queue contention with brain (Q-3) | open | 2026-05-07 |
| BSE/NSE API change | open | 2026-05-07 |
| Schema drift on backfill (Q-2) | open, defer to Phase 9 | 2026-05-07 |
| Gemini-rr profile burnout | low | 2026-05-07 |
| Gemma multimodal capability for vision (Q-4) | open, verify Phase 7 | 2026-05-07 |

---

## Next-session pickup hint

If resuming and STATUS shows "Phase 2 pending":

1. Read `CONTEXT.md` (5 min).
2. Read `STATUS.md` (this file) — confirm Phase 1 verified.
3. Read `VERIFICATION.md` §Phase 2.
4. Read `SPEC.md` §3.1 (FR-INGEST-* and §6.4.3 BSE/NSE API contracts).
5. Locate brain's fetchers:
   - `G:\brain\exchange_util\BSE\BSE_fetcher.py` (23 KB)
   - `G:\brain\exchange_util\NSE\NSEFetcher.py` (78 KB)
6. Copy + clean per A2 ("yes copy and clean. and have a separate copy inside G:\market_notification"):
   - Keep only methods needed for notification fetch + attachment download.
   - Drop the dozens of methods unrelated to notifications.
   - Conform to `ExchangeFetcher` ABC in `src/market_notification/exchange/base.py`.
7. Add `exchange/normalizer.py` to convert raw API records → `RawNotification` DTO.
8. Tests in `tests/integration/test_fetchers_smoke.py` (live calls; skipped if no internet).

Phase 2 exit criteria (per PLAN.md and VERIFICATION.md):
- BSE fetch returns ≥10 rows during market hours.
- NSE fetch returns ≥10 rows during market hours.
- Each row passes the normalizer schema check.
- Date parsing handles all observed formats.

Estimated tool calls: ~80.
