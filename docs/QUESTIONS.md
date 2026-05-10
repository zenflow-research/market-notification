# Market Notification System — Open Questions, Issues, and Deferred Decisions

> Append to this file when you hit a fork that needs the user's input,
> a constraint we discovered post-planning, or something we explicitly parked.
>
> Format per item:
> ```
> ## Q-N — <short title>  (raised in Phase X, YYYY-MM-DD, status)
> **Question / Issue.**
> **Why it matters.**
> **Possible resolutions.**
> **Recommendation.**
> **Resolution.** (filled when resolved)
> ```

---

## Current open items

### Q-1 — Brain's filter rules location for seeding (Phase 4)

**Question / Issue.** `D-D1` says import brain's seeded + auto-learned filter rules. Brain stores them in `notification_filter_rules` table inside `data/notifications.db`. Confirm we read from there at seed time.

**Why it matters.** Without this we lose your past triage work and start with empty rules.

**Possible resolutions.**
- (a) Read at Phase 4 from `G:\brain\data\notifications.db` `notification_filter_rules` table directly.
- (b) Export to JSON one-time and bundle in our `config/filter_rules.json`.

**Recommendation.** (b). Decouples our build from brain's DB existing at runtime. We export once at Phase 4.

**Resolution.** _pending — confirm with user when starting Phase 4._

---

### Q-2 — Backfill scope: brain only, or also Screener_original?

**Question / Issue.** User answered C3: "All of brain's notifications.db (~80k rows) imported once, also check screener_original, there also we download." We need to locate Screener_original's notification archive.

**Why it matters.** If Screener_original has older notifications brain doesn't, we want them.

**Possible resolutions.**
- (a) Inspect `G:\Screener_original\stockDirectory\{id}\notification\BSE_notification.csv` and `NSE_notification.csv` (per-company CSVs that brain mentioned in its context.md).
- (b) Skip Screener_original; brain is comprehensive enough.

**Recommendation.** (a) but verify volume first. If per-company CSVs cover the same period as brain, skip them; if they go further back, import.

**Resolution.** _pending — investigate at Phase 9._

---

### Q-3 — How to coordinate Gemma queue with brain's concurrent Ollama usage (Phase 5/8)

**Question / Issue.** A3 says "create a priority system for gemma so notification is evaluated first." Both brain and our system share Ollama@11434 (FIFO at server). If brain is summarizing a long concall, our notification waits behind it.

**Why it matters.** Defeats the priority guarantee for `important` notifications.

**Possible resolutions.**
- (a) Accept FIFO. Gemma is fast (~148 tok/s); typical notification summary < 30s. Probably fine.
- (b) Run a second Ollama instance on a different port for our system only (requires more VRAM).
- (c) Add a cooperative lock file; brain checks it before each call. Couples us to brain.
- (d) Use brain's existing `summarizer.pause()` API. Couples us to brain.

**Recommendation.** (a) for MVP. Measure latency in production. If `important` notifications wait >2 min routinely, escalate to (b).

**Resolution.** _pending — measure during Phase 8 verification._

---

### Q-4 — Vision model for image extraction: Gemma 4 MoE multimodal vs. dedicated VLM?

**Question / Issue.** G3 says "use Gemma MOE to extract image as well." But `gemma4-zenflow-moe:latest` may or may not be a multimodal variant — depends on which Gemma 4 release is loaded. The reference doc (`GEMMA4_MOE_REFERENCE.md`) treats it as text-only with `flash_attention=true`.

**Why it matters.** If Gemma MoE in our Ollama is text-only, vision fallback fails.

**Possible resolutions.**
- (a) Verify Gemma's multimodal capability via `ollama show gemma4-zenflow-moe:latest`. If multimodal, use it. If not, fall back to (b).
- (b) Pull a dedicated VLM (e.g. `qwen2.5-vl:7b` or `llava:13b`) for vision-only fallback.
- (c) Skip vision; treat empty-text PDFs as `deferred_doc_type='large_misc'` and move on.

**Recommendation.** (a) verify, fall back to (b) if needed. Document outcome here.

**Resolution.** _pending — verify at Phase 7._

---

### Q-5 — Earnings tracker: where does it eventually live?

**Question / Issue.** K1-K6 all parked. User says "tag the notification, separate module later." Confirm the tagging is enough at this stage.

**Why it matters.** Decides what columns we need now vs. later. We're tagging via `gemma_deferred_tags` and `deferred_doc_type`, which is sufficient.

**Possible resolutions.** N/A — already designed.

**Recommendation.** Phase out: implement tagging now (Phase 8); revisit Earnings tab in a future scope-extension.

**Resolution.** _agreed; tagging-only at MVP._

---

### Q-6 — Sector taxonomy file paths: confirm existence

**Question / Issue.** I3 references three files:
- `G:\brain\screener_util\basic_industry_taxonomy.json`
- `D:\Annual_report_extract\docs\sector_metrics_kpis.md`
- `D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md`

Confirm they exist at those paths before Phase 11.

**Why it matters.** Phase 11 (deep-dive) needs them. Missing files means deep-dive prompts are weaker.

**Possible resolutions.** Verify each path at Phase 11 start; if missing, raise as blocker.

**Recommendation.** Verify at Phase 11 start. Open new Q items if any are missing.

**Resolution.** _pending — verify at Phase 11._

---

### Q-7 — `check_pv_df` location and signature  (RESOLVED 2026-05-07, Phase 1 entry)

**Question / Issue.** J4 says use Screener_original's `check_pv_df` for price data. We need its module path and signature.

**Resolution.** **`check_pv_df` does not exist** in either `G:\Screener_original` or `G:\brain` (grep returned no matches). The user's recollection was incorrect. Two candidates discovered:

1. `G:\Screener_original\screener_util\file_paths.py:547` — `get_price_volume_data(data_company_id)` returns `<folder>/{id}_PV.csv` path. Per-company CSV files.
2. `G:\brain\screener_essential.db.pv` — single SQLite table, 6,406,126 rows, columns: `company_id, Date, open, high, low, close, wap, turnover, shares_traded, deliveries, mcap, ff_mcap, no_of_shares, no_of_ff_shares, split_ratio, dividend, split, bonus, right_issue, merger, buy_back, equity_history, esops, mrs_nifty, mrs_midcap, mrs_smlcap, mrs_sensex`.

**Decision (revised after user correction 2026-05-07).** Use **`G:\Screener_original\screener_util\pv_df_check.py`** as the reference implementation. The function reads
`G:\Screener_original\stockDirectory\{data_company_id}\{data_company_id}_PV.csv`
which contains all the columns we need:
`Date, open, high, low, close, wap, turnover, shares_traded, deliveries, Equity History, Dividend, Split, Merger, mcap, ff_mcap, no_of_shares, no_of_ff_shares, split_ratio, open_adj, high_adj, low_adj, close_adj, shares_traded_adj, deliveries_adj`.

There's also a `_PV_metrics.csv` for derived metrics (`type='metric'` parameter).

Per A1/A2 user pattern: we **port** the logic into `market_notification` cleanly (no runtime import of Screener_original), but the source files stay where they are.

**Impact.** Phase 1 `companies/price_source.py` is a clean port of `pv_df_check.py`. **Deviation 1 in design-decisions.md is REVERTED — J4 stands as originally answered.**

---

### Q-11 — Fundamentals data source (NEW, raised 2026-05-07, Phase 1 entry)

**Question / Issue.** Per design-decisions.md J1, fundamentals (mcap, sales, EBITDA, PAT, ratios) come from `G:\brain\screener_essential.db`. **Discovery:** the fundamentals tables in that DB are EMPTY:
- `mcap` (0 rows)
- `key_metrics` (0 rows)
- `margins` (0 rows)
- `financials` (0 rows)
- `segments` (0 rows)
- `stock_margin` (0 rows)

The `master` table holds a single cell containing the company-mapping CSV (24 columns) — same shape as `company_sector_mapping_master.csv` plus 4 AI-derived columns. Useful for identity, not deep fundamentals.

**Why it matters.** Phase 6 (priority engine) uses thresholds against mcap and quarterly sales. Phase 11 (deep-dive) injects "all fundamentals" into prompts. We need a real source.

**Available real data:**
- `pv` (6.4M rows) — has `mcap`, `ff_mcap`, `no_of_shares` time-series.
- `corporate_actions` (1,761 rows).
- `company_info` (65 rows) — HTML "About" text for 65 companies only.

**Possible resolutions.**
- (a) Use CSV `mcap` column for static mcap; query `pv.mcap` for time-series mcap. Defer sales/EBITDA/PAT to a Phase 6 sub-task that locates and ingests them from elsewhere (parquet? Screener_original CSVs? screener.in scrape?).
- (b) Locate brain's actual fundamentals storage. Brain's `notification_priority.py` does `SELECT mcap, sales_latest_quarter FROM companies WHERE id = :id` — it must connect to a different DB. Investigate brain's `database.py` to find which DB.
- (c) Build a fundamentals scrape pipeline (out of scope for v1.0).

**Recommendation.** **(a) for Phase 1, (b) at Phase 6 entry.**
- Phase 1 ships `FundamentalsDTO` returning `mcap` from CSV/pv, all other fields `None`. This satisfies the type contract.
- Phase 6 entry investigates option (b) to find sales/PAT data. If not found, reduce priority-rule coverage to mcap-based rules only and document in `design-decisions.md` §P as Deviation 2.

**Resolution (revised 2026-05-07).** **Source is Screener_original's `stockDirectory/{id}/` per-company folders, NOT brain's screener_essential.db.** Discovery during Phase 1 entry (when checking pv_df_check):

```
G:\Screener_original\stockDirectory\{id}\
├── {id}_PV.csv                  # OHLCV, mcap, ff_mcap, shares, splits...
├── {id}_PV_metrics.csv          # 60+ derived metrics (MAs, RSI, ATR, ROC, etc.)
├── {id}_mcap.csv                # mcap time-series
├── annual_report_{id}.csv
├── concall_{id}.csv
├── corporate_action.json
├── credit_rating_{id}.csv
├── {id}_Read_More.txt           # company description
├── {id}_Read_More_markup.html
├── balance-sheet/                # subdir
├── profit-loss/                  # subdir
├── cash-flow/                    # subdir
├── ratios/                       # subdir
├── quarters/                     # subdir (quarterly results)
├── shareholding/                 # subdir
├── annual_report/, Concall/, Presentation/, peers/, ...
```

This is the **canonical fundamentals source**. Brain's `screener_essential.db` is a compact subset (mostly empty for fundamentals).

**Implication.**
- Phase 1: read CSV `mcap` column for static mcap; read `{id}_PV.csv` for price. Build `ScreenerOriginalCompanyProvider`.
- Phase 6 (priority): read `quarters/` subdir contents for `sales_latest_quarter`. The exact CSV format inside `quarters/` to be inspected at Phase 6 entry.
- Phase 11 (deep-dive fundamentals injection): read `ratios/`, `profit-loss/`, `balance-sheet/`, `cash-flow/`, `shareholding/` subdirs for full metrics block. To be detailed at Phase 11 entry.

**Deviation 2 in design-decisions.md is REVISED:** the source is Screener_original, not brain. This invalidates J1's default but honors the spirit of "use available data, no scraping."

---

### Q-8 — gemini-rr cache hit detection

**Question / Issue.** Per I7, default 30-day cache. But `gemini-rr` returns a `# orchestrator: ... cache=hit|miss` line; we need to parse it to mark `gemini_cache_hit` correctly in the DB.

**Why it matters.** UI surfaces "this was cached" so the user knows freshness.

**Possible resolutions.** Parse first stdout line per gemini-aliases-usage.md §16.5.

**Recommendation.** Implement at Phase 11.

**Resolution.** _pending — Phase 11 implementation._

---

### Q-9 — UI auth: single-user assumed but multi-user planned later (B1)

**Question / Issue.** B1 said "expand for multiple users in future." Currently no auth. Plan for Phase 13+ should include design notes for adding auth without breaking existing pages.

**Why it matters.** If we hard-code single-user assumptions deep in the UI (e.g. global state for "selected company"), retrofitting is painful.

**Possible resolutions.** Design UI session state via `st.session_state` per request; design DB to allow `user_id` column on user-specific tables (read state, notes) when added later.

**Recommendation.** Add a `user_id` column (default 'system') to `notifications.user_notes`-related rows now. Streamlit session-state per request. Document in PLAN's future-work.

**Resolution.** _take into account in Phase 0 schema._

---

### Q-10 — Annual report / investor presentation pipeline ownership

**Question / Issue.** D-13 says we tag and skip annual reports / investor presentations. Where does the downstream pipeline live? (`D:\Annual_report_extract`?)

**Why it matters.** We need to write the tag to a place the downstream can find it. Sharing the same DB row is the simplest approach.

**Possible resolutions.**
- (a) Just set `deferred_doc_type` in our notifications table; downstream queries it.
- (b) Emit a separate JSON envelope to a known path for the downstream to consume.

**Recommendation.** (a) for now. Coordinate with downstream owner later.

**Resolution.** _agreed for now; revisit if downstream needs change._

---

## Resolved items

(none yet)

---

## Parked items (intentional non-decisions)

These are things we explicitly decided not to design now. Listed here so they don't get lost:

- **Earnings tracker** (K1-K6) — tagging-only in MVP; full tracker is out-of-scope.
- **Annual report / investor presentation summarization** — delegated to a separate downstream module.
- **Mobile UI / push notifications** (N3) — out of scope.
- **Cloud deployment** (N2) — out of scope.
- **Authentication** (N1) — single-user localhost; multi-user is future scope.
- **Backfill of older Screener_original archive** (Q-2) — investigate at Phase 9; maybe-import.
- **Second Ollama instance** for queue isolation (Q-3) — only if measured contention forces it.
