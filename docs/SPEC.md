# Market Notification System — Specification

> **Version.** v1.0 · 2026-05-07
> **Status.** Approved — frozen for v1.0. Future amendments append as `## Amendment N`.
> **Owner.** research@zenflow.finance
>
> **Purpose.** The formal, numbered, testable contract for what the system MUST
> do. Each requirement has a unique ID (e.g. `FR-INGEST-001`), a verification
> reference, and an owning phase. This file is what we audit the build against.
>
> **Companions:**
> - `PLAN.md` — sequencing and tool-call budget (the *how/when*)
> - `architecture.md` — module diagrams and ABC details (the *shape*)
> - `design-decisions.md` — answer log and rationale (the *why*)
> - `VERIFICATION.md` — per-phase exit-criteria checklists (the *audit*)
> - `STATUS.md` — running state of the build (the *now*)
>
> **Conventions.** RFC 2119 keywords (MUST, SHOULD, MAY) carry their standard
> meaning. Unprefixed statements describe context, not requirements.

---

## 0. Document control

| Field | Value |
|---|---|
| Spec ID | MN-SPEC-v1.0 |
| Created | 2026-05-07 |
| Last updated | 2026-05-07 |
| Frozen for | Phases 0-13 of `PLAN.md` |
| Amendment process | Append `## Amendment N (date)` at end with FR-IDs touched and rationale; do not edit existing FR text |

---

## 1. Glossary

| Term | Definition |
|---|---|
| **Notification** | One corporate filing emitted by BSE or NSE (announcement, results, board outcome, etc.) |
| **Source** | `BSE` or `NSE` — the exchange that disseminated the filing |
| **Cross-exchange duplicate** | The same corporate event filed with both BSE and NSE within ~10 min, with substantially-similar headlines |
| **Junk** | A filing that matches a filter rule and SHOULD NOT consume LLM resources or appear in default UI views |
| **Category** | One of the 50 taxonomy slots (e.g. `Capacity Expansion`, `USFDA (Approval/Warning/Import Alert)`) |
| **Group** | One of the 10 taxonomy super-buckets (e.g. `Growth & Expansion`, `Regulatory & Compliance`) |
| **Priority** | One of `important`, `medium`, `normal`, `ignored` |
| **Pre-priority (deterministic)** | Priority computed by rubric+threshold rules without LLM |
| **Final priority** | Priority after Gemma override step (Gemma may upgrade or downgrade) |
| **Pipeline status** | A row's position in the state machine (see §8) |
| **Deferred-doc-type** | A tag meaning "this filing carries a document type we do NOT summarize here" — `annual_report` / `investor_presentation` / `earnings` / `ppt` / `credit_note` / `large_misc` |
| **Deep-dive** | A `gemini-rr` consult that produces topic-aware Q&A on an `important` notification |
| **Backfill** | One-time historical import (~80k rows) from `G:\brain\data\notifications.db` |
| **SLA** | A measurable timing constraint (e.g. "classified within 5 min of arrival") |
| **Worker** | The single-threaded process that drains the pipeline state machine |
| **Poller** | The always-on process that fetches BSE+NSE filings every 60s |

---

## 2. System overview (context)

```
                 +-------------------+
                 |   BSE / NSE       |
                 |   public APIs     |
                 +--------+----------+
                          | HTTPS, ~60s polling
                          v
+------------------ Market Notification System ----------------------+
|                                                                    |
|   poller_proc  --> [ingest, filter, dedup, cross-exchange]         |
|                       |                                            |
|                       v                                            |
|                 notifications.db (SQLite, WAL)                     |
|                       ^                                            |
|                       |                                            |
|   worker_proc  --> [classify, prioritize, dl+extract,              |
|                     summarize, deep-dive] (single-threaded)        |
|                       |                                            |
|                       v                                            |
|   streamlit_proc <-- read-only views, user actions (mark junk,    |
|                       add filter rule, mark read, notes)           |
|                                                                    |
|   External:  Ollama (Gemma), gemini-rr binary                     |
|   Read-only: Screener_original CSV, brain screener_essential.db    |
|              Screener_original price helper, taxonomy/KPI files    |
+--------------------------------------------------------------------+
```

The system is independent (per `D-01`) — no runtime dependency on `G:\brain`'s
Flask/React app or its broker. Data inputs from `G:\brain` and
`G:\Screener_original` are accessed read-only; one-time copies are made for
seeding (filter rules, taxonomy, KPIs).

---

## 3. Functional requirements

> Every FR has: **ID**, **Title**, **Description**, **Source** (which decision drives it),
> **Owner phase**, **Verification**, **Priority** (must/should/may).

### 3.1 Notification ingestion (FR-INGEST-*)

#### FR-INGEST-001 — 1-minute polling cadence
**Description.** The poller MUST attempt one BSE fetch and one NSE fetch per
poll cycle, and the cycle MUST repeat with a configurable interval defaulting
to 60 seconds.
**Source.** User opening request; design-decisions.md `C1` (60s default).
**Owner phase.** 3.
**Verification.** Phase 3 — observed log lines every 60±5 seconds across a
10-minute run.
**Priority.** must.

#### FR-INGEST-002 — 24×7 polling window
**Description.** Polling MUST continue around the clock, regardless of market
hours.
**Source.** design-decisions.md `C2` ("24×7").
**Owner phase.** 3.
**Verification.** Phase 13 soak — 24-hour run with non-zero off-hours fetch
attempts.
**Priority.** must.

#### FR-INGEST-003 — Company resolution
**Description.** Each ingested filing MUST be mapped to a company by either
BSE code or NSE symbol or ISIN. Failures (unmapped symbol, delisted entity)
MUST be logged but not block ingestion; the row MUST be persisted with
`company_id=0` and `pipeline_status='ingested'` so it remains visible in the
"unmapped" UI bucket.
**Source.** User opening ("Then i want to map the notification with
companies"); design-decisions.md `B3`.
**Owner phase.** 3.
**Verification.** Phase 3 — sample of 100 ingested rows ≥95% mapped to
company_id>0; unmapped rows visible via SQL query.
**Priority.** must.

#### FR-INGEST-004 — Historical-symbol fallback
**Description.** If a current symbol does not match any company, the resolver
MUST consult `historical_symbol_map` for renamed/merged/demerged entities
before giving up.
**Source.** Brain's existing approach + best practice.
**Owner phase.** 3.
**Verification.** Phase 3 — fixture symbol "HDFC" (delisted post-merger)
resolves to successor `HDFCBANK`'s `company_id`.
**Priority.** should.

#### FR-INGEST-005 — Cross-exchange grouping
**Description.** When the same company files an event with both BSE and NSE
within ±10 minutes AND headline cosine similarity ≥ 0.85, the system MUST
assign both filings the same `cross_exchange_group_id` UUID.
**Source.** User opening ("combine BSE / NSE notifications, because some
companies give notification in both exchanges"); design-decisions.md `C4`.
**Owner phase.** 3.
**Verification.** Phase 3 fixture — paired BSE+NSE filing produces shared
`cross_exchange_group_id`.
**Priority.** must.

#### FR-INGEST-006 — Cross-exchange duplicate dropping
**Description.** Of two filings sharing a `cross_exchange_group_id`, the
SECOND-arrived MUST be marked `cross_exchange_role='duplicate_dropped'` and
MUST NOT be sent to the LLM pipeline (no classify, no summarize, no deep-dive).
The first-arrived MUST proceed normally as `cross_exchange_role='primary'`.
**Source.** design-decisions.md `C4` ("take whichever arrived first and
discard the second"); `C4b` ("show only one").
**Owner phase.** 3.
**Verification.** Phase 3 — fixture pair has 1 row in `primary` proceeding to
`classify_pending`, 1 row in `duplicate_dropped` terminal-equivalent.
**Priority.** must.

#### FR-INGEST-007 — Dedup on natural key
**Description.** The unique key
`(source, company_id, announced_at, headline)` MUST prevent duplicate
insertion. Re-polling the same window MUST result in zero new rows.
**Source.** Brain's existing approach + correctness.
**Owner phase.** 3.
**Verification.** Phase 3 — re-poll same 10-min window twice; second poll
inserts 0 rows.
**Priority.** must.

#### FR-INGEST-008 — Watermark persistence
**Description.** Per-source poll watermark (`last_poll_at`, `last_seq_id`,
`last_date`) MUST persist in `notification_poll_state` so the poller resumes
correctly after restart.
**Source.** Operational reliability.
**Owner phase.** 3.
**Verification.** Phase 3 — kill+restart poller; watermark non-empty before
restart, no re-fetched duplicates after restart.
**Priority.** must.

#### FR-INGEST-009 — Backfill from brain
**Description.** A one-shot script MUST import all rows from
`G:\brain\data\notifications.db` `notifications` table into our DB with
`pipeline_status='imported_legacy'`. Imported rows MUST NOT be picked up by
the live worker by default; they MAY be re-classified via a manual flag.
**Source.** design-decisions.md `C3` ("All of brain's notifications.db (~80k
rows) imported once").
**Owner phase.** 9.
**Verification.** Phase 9 — row count matches brain's within 1% tolerance;
sample 10 rows compare 1:1.
**Priority.** must.

#### FR-INGEST-010 — Backfill from Screener_original (best-effort)
**Description.** If older notifications exist under
`G:\Screener_original\stockDirectory\{id}\notification\*.csv` not present in
brain's history, those MUST also be imported with
`pipeline_status='imported_legacy'`. If the data is fully covered by brain,
this is a no-op.
**Source.** design-decisions.md `C3` ("also check screener_original, there
also we download").
**Owner phase.** 9.
**Verification.** Phase 9 — count of rows older than brain's earliest is
either 0 or imported.
**Priority.** should.

---

### 3.2 Junk filtering (FR-FILTER-*)

#### FR-FILTER-001 — Negative-list rule engine
**Description.** Before an ingested row enters the LLM pipeline, the
filter engine MUST check it against an active rule set. Matching rows MUST be
marked `is_useless=1` AND `pipeline_status='ignored'` AND
`junk_rule_id=<matched rule id>`.
**Source.** User opening ("Then i want to remove the useless notifications");
design-decisions.md `D1`, `D2`.
**Owner phase.** 4.
**Verification.** Phase 4 — sample 200 raw notifications; ≥10% flagged.
**Priority.** must.

#### FR-FILTER-002 — Rule storage
**Description.** Filter rules MUST live in `notification_filter_rules` with
fields: `rule_type` (`category|subcategory|headline_regex|keyword`),
`pattern`, optional `source` (`BSE|NSE|null=both`), `action` (`hide|block`),
`created_by` (`system|user|auto`), `confidence`, `reason`, `is_active`.
**Source.** Brain's schema + design-decisions.md `D2`.
**Owner phase.** 0 (schema), 4 (loader).
**Verification.** Phase 0 — schema matches; Phase 4 — rules load.
**Priority.** must.

#### FR-FILTER-003 — Initial rule seed from brain
**Description.** The initial rule set MUST include the system-seeded rules
AND the auto-learned rules from brain's `notification_filter_rules` table,
exported once into `config/filter_rules.json` and loaded at first-run.
**Source.** design-decisions.md `D1` (default + user "We will deep dive later
and update").
**Owner phase.** 4.
**Verification.** Phase 4 — count of seeded rules ≈ count from brain export.
**Priority.** must.

#### FR-FILTER-004 — UI-driven rule learning
**Description.** When a user marks a notification as junk in the UI, the
system MUST present a per-action choice of: (a) add a category-level rule,
(b) add a headline-regex rule, (c) just mark this single notification ignored
without learning. The chosen action MUST be persisted with
`created_by='user'`.
**Source.** design-decisions.md `D2` (default — user empowered, avoids
over-eager rules).
**Owner phase.** 12.
**Verification.** Phase 12 — UI walkthrough exercises each branch.
**Priority.** must.

#### FR-FILTER-005 — Soft filter (recoverable)
**Description.** Filtered notifications MUST remain in the database with
`pipeline_status='ignored'`. They MUST be visible in the "Ignored" tab. Bulk
un-ignoring MUST be possible from the UI.
**Source.** User opening ("Do not show me the ignored ones. ( provide a
button for me to review ignored notification set )"); design-decisions.md
`L4`.
**Owner phase.** 12.
**Verification.** Phase 12 — Ignored tab lists ignored rows; un-ignore button
returns row to active processing.
**Priority.** must.

#### FR-FILTER-006 — Performance budget
**Description.** Filter-engine evaluation MUST process ≥1000 notifications
per second on a single CPU core (regex compilation cached at load).
**Source.** Operational sanity — filter must not slow ingest.
**Owner phase.** 4.
**Verification.** Phase 4 — perf benchmark.
**Priority.** should.

---

### 3.3 Classification (FR-CLASSIFY-*)

#### FR-CLASSIFY-001 — Taxonomy
**Description.** The taxonomy MUST consist of 10 groups containing 50
categories. The exact list MUST be the version copied from brain's
`notification_classifier.py` `TAXONOMY` constant at Phase 5 start, with
version tag `taxonomy_v1`.
**Source.** design-decisions.md `E1`.
**Owner phase.** 5.
**Verification.** Phase 5 — `classifier/taxonomy.py` `TAXONOMY` matches
brain export; `taxonomy_version` table has `v1` row.
**Priority.** must.

#### FR-CLASSIFY-002 — Every non-junk gets LLM classification
**Description.** Every notification with `pipeline_status='classify_pending'`
MUST be classified by the LLM (Gemma). No regex shortcut for the
classification step (regex is reserved for filter only). Classification MUST
return `(category, group, confidence, reasoning)`.
**Source.** design-decisions.md `E2` (user override of default —
"every non ignored or non junk notification gets LLM classification").
**Owner phase.** 5.
**Verification.** Phase 5 — 20 sample classifications, all have non-null
`ai_category` from `source='gemma'`.
**Priority.** must.

#### FR-CLASSIFY-003 — Output validation
**Description.** Classifier output MUST be validated against the active
taxonomy. If the LLM returns a category not in the taxonomy, the result MUST
be rejected, the notification MUST be re-tried up to 2 times with a stricter
prompt, and on final failure MUST be tagged `ai_category='Uncategorized'`
with `ai_category_source='fallback'`.
**Source.** Robustness; risk register `Gemma classifier hallucinates`.
**Owner phase.** 5.
**Verification.** Phase 5 — fixture forcing invalid response triggers
fallback path.
**Priority.** must.

#### FR-CLASSIFY-004 — Latest-first processing
**Description.** When multiple notifications are in `classify_pending`, the
worker MUST claim the row with the most recent `announced_at` first.
**Source.** design-decisions.md `E2` ("Start from latest and move backward").
**Owner phase.** 5, 10.
**Verification.** Phase 5/10 — burst test of mixed-age rows; observed claim
order is newest-first.
**Priority.** must.

#### FR-CLASSIFY-005 — 5-minute SLA monitor
**Description.** Any notification remaining in `classify_pending` for >5
minutes after ingestion MUST trigger a SLA-breach event (logged at WARNING +
visible in Health UI).
**Source.** design-decisions.md `E2` ("monitoring trigger if it does not get
categorized within 5 min of arrival").
**Owner phase.** 5.
**Verification.** Phase 5 — injected fixture with delay >5min produces alert.
**Priority.** must.

#### FR-CLASSIFY-006 — Confidence threshold
**Description.** Classifier confidence below 0.5 MUST tag the row for manual
review (UI flag) but MUST NOT block downstream priority/summarize/deep-dive.
**Source.** Quality safety net.
**Owner phase.** 5.
**Verification.** Phase 5 — fixture with low-confidence response sets the
flag.
**Priority.** should.

---

### 3.4 Priority assignment (FR-PRIORITY-*)

#### FR-PRIORITY-001 — Deterministic pre-priority
**Description.** Every classified notification MUST first receive a
deterministic priority via the 49-category rubric and threshold rules
(amount-vs-mcap, amount-vs-sales, stage detection, etc.) ported from brain's
`notification_priority.py`. Output MUST include `det_priority`, `det_score`
(0-100), and `det_reasons` (JSON list).
**Source.** User opening ("sort the notification based on rules");
design-decisions.md `F1`.
**Owner phase.** 6.
**Verification.** Phase 6 — 1000 backfilled rows scored; distribution matches
expected envelope (important 5-15%, medium 20-30%, normal 50-65%, ignored
5-10%).
**Priority.** must.

#### FR-PRIORITY-002 — LLM priority override
**Description.** After deterministic scoring, Gemma MUST receive (notification
text, det_priority, det_reasons) and produce a final priority. Gemma MAY
upgrade OR downgrade. Final result MUST be stored in `ai_priority`,
`ai_priority_score`, `ai_priority_reasons`. Both deterministic and LLM
results MUST be retained for audit.
**Source.** design-decisions.md `F2` (user override of default — "Gemma fully
overrides; rubric is starting hint").
**Owner phase.** 6.
**Verification.** Phase 6 — fixture where deterministic says `medium` and
Gemma overrides to `important` (or vice versa); both columns populated.
**Priority.** must.

#### FR-PRIORITY-003 — Output shape
**Description.** Priority MUST be expressed both as a categorical bucket
(`important|medium|normal|ignored`) and a numeric score (0-100). UI MUST show
the bucket; sorting MAY use the score for finer-grained ranking.
**Source.** design-decisions.md `F3` (default).
**Owner phase.** 6.
**Verification.** Phase 6 — schema has both columns; UI uses score for
secondary sort.
**Priority.** must.

#### FR-PRIORITY-004 — Newspaper-ad rule
**Description.** Notifications matching `Quarterly Results` category AND
headline regex `newspaper|advertisement|publication` MUST receive `det_score=0`
and `det_priority='ignored'`.
**Source.** Brain's existing rule (priority_rubric.md, Quarterly Results
section).
**Owner phase.** 6.
**Verification.** Phase 6 — fixture matching both criteria produces ignored.
**Priority.** must.

#### FR-PRIORITY-005 — Auditor-qualification red flag
**Description.** `Auditor Change / Qualification` notifications with
keyword `qualification|disclaimer` MUST receive `det_score>=85` (i.e.
`important`).
**Source.** Brain's existing rule.
**Owner phase.** 6.
**Verification.** Phase 6 — fixture; `det_priority='important'`.
**Priority.** must.

#### FR-PRIORITY-006 — Capex / acquisition / order-win threshold rules
**Description.** Capex/Capacity Expansion: amount >= 5% mcap OR >= 1000 Cr
absolute MUST score +20 (>= 5000 Cr +30 instead). Acquisition: deal size >= 30%
of FY revenue MUST upgrade to `important`. Order Win: order >= 20% of annual
revenue MUST upgrade to `important`. Detailed thresholds per
`docs/PLAN.md` §5.x and brain's `notification_priority.py`.
**Source.** Brain's existing rule.
**Owner phase.** 6.
**Verification.** Phase 6 — unit tests covering each rule with synthetic
fundamentals.
**Priority.** must.

#### FR-PRIORITY-007 — Modular per-category functions
**Description.** Each priority rule MUST live in its own clearly-named
function (e.g. `_score_capex(notif, fundamentals) -> ScoreContribution`).
Adding/removing a rule MUST be a single-file edit with a single new test.
**Source.** design-decisions.md `F1` ("create a separate function and
modularize the code so that changes are easy and also easy to read").
**Owner phase.** 6.
**Verification.** Phase 6 — code review confirms each rubric category has its
own function; no monolithic `if/elif` ladder.
**Priority.** must.

---

### 3.5 Attachment processing (FR-ATTACH-*)

#### FR-ATTACH-001 — Download to D:\Notification Dump\{cid}\
**Description.** PDFs and other attachments MUST be downloaded to
`D:\Notification Dump\{company_id}\{filename}`. MD5 dedup MUST avoid
re-downloading the same file.
**Source.** design-decisions.md `G1`.
**Owner phase.** 7.
**Verification.** Phase 7 — re-running poll for same notification doesn't
grow disk; MD5 logged.
**Priority.** must.

#### FR-ATTACH-002 — Text extraction (default 20 pages)
**Description.** PDF text extraction MUST use `pdfplumber` with a default cap
of 20 pages. Extracted text MUST be stored in `pdf_extracted_text`.
**Source.** design-decisions.md `G2` default + `G4`.
**Owner phase.** 7.
**Verification.** Phase 7 — sample 50 PDFs; ≥90% have non-empty text or a
deferred-doc tag or vision summary.
**Priority.** must.

#### FR-ATTACH-003 — Vision fallback for empty text
**Description.** If `pdfplumber` returns empty (or near-empty) text after the
20-page pass, the system MUST fall back to a vision pass (Gemma 4 MoE
multimodal if available, else a configured VLM) that generates an
`pdf_image_summary`.
**Source.** design-decisions.md `G2` (default) + `G3` ("yes use gemma MOE to
extract image as well").
**Owner phase.** 7.
**Verification.** Phase 7 — fixture scanned-PDF triggers vision; text empty,
image summary populated. (Gemma multimodal capability verified per Q-4.)
**Priority.** must.

#### FR-ATTACH-004 — Deferred-doc-type tagging
**Description.** PDFs that are annual reports, investor presentations,
quarterly-result PDFs, concall PPTs, credit-rating reports, OR documents >20
pages without a known type MUST be tagged with `deferred_doc_type` AND MUST
NOT have their body text fed into the summarizer. Headline + metadata still
flow into summary.
**Source.** design-decisions.md `G4` ("For now don't process Annual report or
investor presentation. We will plan that breakup later. Lets have 20. I also
want a summary of large documents that were not processed.").
**Owner phase.** 7, 8.
**Verification.** Phase 7/8 — annual-report fixture sets
`deferred_doc_type='annual_report'`; summarizer notes "deferred body" in its
summary.
**Priority.** must.

#### FR-ATTACH-005 — Embedded link follow-up
**Description.** When extracted PDF text contains URLs to additional
documents, the system SHOULD HEAD-fetch the URL, infer doc type, and either
(a) produce a brief external_link_summary if it's an HTML page or small PDF,
or (b) tag it as deferred for the downstream pipeline.
**Source.** design-decisions.md `G4` ("sometimes links are provided of
documents, we should open those and check and see if it needs processing").
**Owner phase.** 7.
**Verification.** Phase 7 — fixture PDF with outbound link produces
`external_links` entry with summary.
**Priority.** should.

#### FR-ATTACH-006 — Attachment status tracking
**Description.** Each notification MUST have a `download_status` column
transitioning through `pending → downloading → done | failed | skipped`.
**Source.** Pipeline state-machine clarity.
**Owner phase.** 7.
**Verification.** Phase 7 — journal entries match transitions.
**Priority.** must.

---

### 3.6 Summarization (FR-SUMM-*)

#### FR-SUMM-001 — All non-ignored, non-deferred notifications get a Gemma summary
**Description.** Every notification with `pipeline_status='summarize_pending'`
AND `deferred_doc_type IS NULL` MUST receive a Gemma summary using
`gemma4-zenflow-moe:latest` via Ollama at `localhost:11434`.
**Source.** User opening ("For low mid and important notification we
summarize using Gemma"); design-decisions.md `H1`.
**Owner phase.** 8.
**Verification.** Phase 8 — 20 fixtures; all have `gemma_summarized_at` set.
**Priority.** must.

#### FR-SUMM-002 — Extended JSON output schema
**Description.** Gemma's response MUST conform to the schema:
```
{
  "summary": str,
  "impact": str,
  "key_figures": [{"label": str, "value": str, "unit": str}],
  "key_people": [{"name": str, "role": str}],
  "key_dates": [{"label": str, "iso_date": str, "certainty": str}],
  "attachments_referenced": [str],
  "deferred_doc_tags": [str],   // 'earnings'|'ppt'|'annual_report'|'credit_note'|'large_misc'
  "external_links": [{"url": str, "referenced_as": str, "target_summary": str}],
  "confidence": float
}
```
Invalid output MUST be retried up to 2 times with stricter prompts.
**Source.** design-decisions.md `H2` (user — "we need a extended list").
**Owner phase.** 8.
**Verification.** Phase 8 — JSON-schema validator passes on 20 samples.
**Priority.** must.

#### FR-SUMM-003 — Figures preserved exactly
**Description.** All numeric figures, percentages, dates, and rupee amounts
appearing in the source notification MUST be preserved verbatim in
`key_figures` (no paraphrase, no rounding, no unit conversion). Temperature
MUST be 0.1 to enforce determinism.
**Source.** GEMMA4_MOE_REFERENCE.md guidance for financial summarization;
risk mitigation for hallucination.
**Owner phase.** 8.
**Verification.** Phase 8 — manual diff of 5 summary outputs against source
PDFs; figures match exactly.
**Priority.** must.

#### FR-SUMM-004 — Deferred-doc tagging at summarize time
**Description.** If during summarization Gemma identifies the notification as
referring to an `earnings` / `ppt` / `annual_report` / `credit_note` doc type,
the corresponding tag MUST be added to `gemma_deferred_tags`. The summary
MUST still be produced from headline + body + (if available) PDF text;
deferred-doc *body* extraction is out of scope here.
**Source.** design-decisions.md `H2` ("For earnings, tag that as earnings,
similar to PPT or annual report and credit Note. we will have a separate
system to handle that. We should not handle it here.").
**Owner phase.** 8.
**Verification.** Phase 8 — quarterly-results fixture has
`gemma_deferred_tags=['earnings']`.
**Priority.** must.

#### FR-SUMM-005 — One-shot extraction (no document re-read)
**Description.** Once a notification has been summarized successfully, the
PDF extraction (`pdf_extracted_text`) and the structured summary MUST capture
everything needed. The downstream pipeline (deep-dive) MUST be able to work
from these stored fields without re-opening the source PDF.
**Source.** design-decisions.md `H2` ("Ideally once notification comes and I
extract I dont want to use the document again").
**Owner phase.** 8.
**Verification.** Phase 8 — deep-dive Phase 11 verification confirms it
operates on stored fields only.
**Priority.** must.

#### FR-SUMM-006 — Ollama-down fallback
**Description.** When Ollama is unreachable, summarization MUST fail with
status `summarize_failed` and a 30-second retry. After `retry_max=3`, the row
moves to `summarize_dead` and surfaces in Health UI for manual intervention.
**Source.** design-decisions.md `H3` (default — queue + retry).
**Owner phase.** 8.
**Verification.** Phase 8 — Ollama-down simulation produces retries +
eventual `summarize_dead` after 3 attempts.
**Priority.** must.

#### FR-SUMM-007 — Latency budget
**Description.** P95 summarize latency for a typical (1-2 page) notification
SHOULD be under 30 seconds.
**Source.** Operational target informed by GEMMA4_MOE_REFERENCE.md (~148
tok/s on GPU).
**Owner phase.** 8.
**Verification.** Phase 8 — latency histogram from 20 samples; p95 < 30s.
**Priority.** should.

---

### 3.7 Deep-dive via gemini-rr (FR-DIVE-*)

#### FR-DIVE-001 — Auto-trigger on important
**Description.** Every notification reaching `pipeline_status='deep_dive_pending'`
because `ai_priority='important'` AND `ai_category` is in the eligible-list
(see FR-DIVE-002) MUST be deep-dived via `gemini-rr` automatically.
**Source.** User opening ("important ones I want to further evaluate using
gemini-rr"); design-decisions.md `I1` (default — auto for important).
**Owner phase.** 11.
**Verification.** Phase 11 — 5 important fixtures across 5 categories; all
deep-dived.
**Priority.** must.

#### FR-DIVE-002 — Eligible categories
**Description.** Deep-dive MUST run for these categories:
`Capacity Expansion`, `Capex Update`, `Acquisition`, `Joint Venture`,
`Order Win`, `Contract Award`, `Divestiture / Sale`, `Merger`,
`Demerger / Spinoff`, `USFDA (Approval/Warning/Import Alert)`,
`Credit Rating Change`, `Tax / GST Order`, `Legal / Litigation`,
`SEBI Order`, `Quarterly Results`, `Equity Dilution (QIP/FPO/Preferential)`,
`Buyback`, `OFS (Offer for Sale)`, `Cyber Incident`, `Other Important`.
Deep-dive MUST NOT run automatically for: `Management Change`,
`Board Meeting Outcome`, `Dividend`.
**Source.** design-decisions.md `I2` defaults.
**Owner phase.** 11.
**Verification.** Phase 11 — config table matches; non-eligible importants
get a manual button.
**Priority.** must.

#### FR-DIVE-003 — Manual deep-dive button
**Description.** For ANY medium or normal (non-ignored) notification, a UI
button MUST allow on-demand deep-dive even outside the eligible categories.
**Source.** design-decisions.md `I1` (user-affirmed — "default" includes
manual for medium).
**Owner phase.** 12.
**Verification.** Phase 12 — UI button triggers deep-dive on a non-eligible
medium fixture.
**Priority.** must.

#### FR-DIVE-004 — Per-category prompt taxonomy
**Description.** Each eligible category MUST have its own prompt module
(`deep_dive/prompts/{category}.py`) that asks topic-specific questions. The
prompt MUST be designed by consulting `gemini-rr` itself, refined, and
checked into source control with a `prompt_version` tag.
**Source.** User opening ("define a taxonomy of prompts, for example if topic
is capex, we will ask capex related questions"); design-decisions.md `I3`
(user — "lets work on a prompt for each category, consult with gemini to
make a rich prompt list").
**Owner phase.** 11.
**Verification.** Phase 11 — each eligible category has a non-empty prompt
file; sample outputs reference the right topic questions.
**Priority.** must.

#### FR-DIVE-005 — Sector / KPI-aware prompts
**Description.** Prompts MUST incorporate sector-specific context loaded
from:
- `G:\brain\screener_util\basic_industry_taxonomy.json` (industry hierarchy)
- `D:\Annual_report_extract\docs\sector_metrics_kpis.md` (sector KPIs)
- `D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md` (concall topics)

A capex notification for a chemicals company SHOULD include questions about
the specific chemical, market size, asset turn, capacity utilization.
**Source.** design-decisions.md `I3` (user — file paths listed) and `I6`
(user — "for example if there is a chemical we want to know as much as
possible about that chemical").
**Owner phase.** 11.
**Verification.** Phase 11 — chemicals capex fixture produces output that
references chemical-specific KPIs; pharma USFDA fixture references molecule +
market size.
**Priority.** must.

#### FR-DIVE-006 — Full-fundamentals injection
**Description.** Every deep-dive prompt MUST include the full company
fundamentals block (mcap, last 4Q sales, EBITDA, PAT, margins, ROCE/ROE,
debt, FCF, capex, P/E, dividend yield, promoter/FII/DII holdings, 3y/5y CAGRs,
sector, peer set if available). The system MUST hint at relevant analytical
relationships (e.g. for capex: asset turn for revenue projection).
**Source.** design-decisions.md `J5` (user override of default — "send all
fundamental data, let gemini decide. for example if capex is added and we
estimate revenue, asset turn is a good way").
**Owner phase.** 11.
**Verification.** Phase 11 — captured prompt for a capex deep-dive contains
all listed metrics; output references at least one fundamentals-derived
calculation.
**Priority.** must.

#### FR-DIVE-007 — Output: structured JSON + prose
**Description.** Each deep-dive MUST produce TWO outputs:
1. **Structured JSON** with category-specific keys (e.g. for capex:
   `{expansion_type, capacity_change_pct, capex_cr, financing, timeline,
   strategic_rationale, demand_drivers, risks}`).
2. **Prose narrative** (markdown, sectioned) for human reading.

Both MUST be persisted in `gemini_deep_dive_json` and `gemini_deep_dive_prose`.
**Source.** design-decisions.md `I4` default + `I6` (one-shot call yielding
both).
**Owner phase.** 11.
**Verification.** Phase 11 — sample outputs have both fields populated and
schema-valid.
**Priority.** must.

#### FR-DIVE-008 — gemini-rr invocation contract
**Description.** Calls MUST shell out to the absolute path
`C:\Users\user\bin\gemini-rr.cmd -p -` with the prompt on stdin. The first
line of stdout (`# orchestrator: ...`) MUST be parsed for `cache=hit|miss`,
`profile=`, `latency=`, and stripped before persisting the response.
**Source.** gemini-aliases-usage.md §16; design-decisions.md `I6`.
**Owner phase.** 11.
**Verification.** Phase 11 — captured stdout shows orchestrator line; DB
fields `gemini_cache_hit`, `gemini_profile_used`, `gemini_latency_s` set.
**Priority.** must.

#### FR-DIVE-009 — Cache enabled
**Description.** `gemini-rr`'s default 30-day prompt-hash cache MUST be left
on (no `--no-cache` flag). Cache hits MUST flag `gemini_cache_hit=1`.
**Source.** design-decisions.md `I7` (user — "Okay" to default).
**Owner phase.** 11.
**Verification.** Phase 11 — same notification deep-dived twice; second has
cache_hit=1.
**Priority.** must.

#### FR-DIVE-010 — Quota policy
**Description.** No daily quota cap. The system MUST run deep-dives until
`gemini-rr` returns failure (rc=3, all profiles burnt). On failure, the row
MUST move to `deep_dive_failed`, retry per FR-PIPE-005 backoff, and finally
to `deep_dive_dead` after 3 attempts.
**Source.** design-decisions.md `I8` (user — "yah just keep going till
failure").
**Owner phase.** 11.
**Verification.** Phase 11 — simulated all-burnt response triggers failed
state.
**Priority.** must.

#### FR-DIVE-011 — Outer timeout budgeting
**Description.** Subprocess outer timeout MUST be at least
`gemini_rr_inner_timeout × 2 + 30s` to allow `gemini-rr` to perform internal
failover (per gemini-aliases-usage.md §16.11 Gotcha C).
**Source.** Mitigation against §16.11 Gotcha C.
**Owner phase.** 11.
**Verification.** Phase 11 — code review of subprocess.run timeout.
**Priority.** must.

---

### 3.8 Pipeline orchestration (FR-PIPE-*)

#### FR-PIPE-001 — State machine
**Description.** Every notification MUST transition through the state machine
defined in `pipeline/states.py` and `PLAN.md` §6. Terminal states are
`ignored`, `done`, `done_deferred`, `imported_legacy`, and the `*_dead`
states. No invalid transitions are permitted; the
`update_pipeline_status(from, to)` repository method MUST enforce
conditional updates.
**Source.** Pipeline correctness.
**Owner phase.** 0 (states), 10 (dispatcher).
**Verification.** Phase 10 — invariant: every row's `to_status` only follows
the state-graph; journal proves this.
**Priority.** must.

#### FR-PIPE-002 — Sequential single-threaded worker
**Description.** A single worker process with a single thread MUST drain the
state machine. Concurrent LLM calls within the worker are NOT permitted.
Multiple notifications SHALL NOT be processed in parallel.
**Source.** design-decisions.md `F4` (user override of default — "worker
thread is complex, multi llm call is complex, rather complete the existing
task then put important one. Then again medium. Important further goes to
gemini").
**Owner phase.** 10.
**Verification.** Phase 10 — process inspector confirms one thread; injected
burst processed sequentially.
**Priority.** must.

#### FR-PIPE-003 — Priority order in dispatcher
**Description.** When picking the next task, the dispatcher MUST follow this
order:
1. `ai_priority='important'` AND `pipeline_status='deep_dive_pending'`
2. `ai_priority='important'` AND `pipeline_status='summarize_pending'`
3. `pipeline_status='classify_pending'` (newest first per FR-CLASSIFY-004)
4. `pipeline_status='attachment_pending'`
5. `ai_priority='medium'` AND `pipeline_status='summarize_pending'`
6. `ai_priority='normal'` AND `pipeline_status='summarize_pending'`
7. `pipeline_status='deep_dive_pending'` for any priority (manual triggers)
8. Failed states with `next_retry_at <= now()` (retry candidates)
**Source.** design-decisions.md `F4` user statement. Important sequenced
ahead of medium ahead of normal — but never preempting a running task.
**Owner phase.** 10.
**Verification.** Phase 10 — burst test confirms claim order matches.
**Priority.** must.

#### FR-PIPE-004 — No preemption
**Description.** A running step MUST run to completion (or timeout) before
the dispatcher claims the next row. Arrival of a higher-priority notification
MUST NOT interrupt a running summarize / deep-dive call.
**Source.** design-decisions.md `F4` (user — "rather complete the existing
task then put important one").
**Owner phase.** 10.
**Verification.** Phase 10 — inject important notification while medium is
mid-summarize; medium finishes first.
**Priority.** must.

#### FR-PIPE-005 — Retry policy
**Description.** Failed steps (`*_failed`) MUST be retried with exponential
backoff `[2, 4, 8]` minutes. After `retry_count >= 3`, the status MUST move
to `*_dead` and require manual reset via UI.
**Source.** Operational reliability; PLAN.md §6.
**Owner phase.** 10.
**Verification.** Phase 10 — retry timing observed; dead state reached after
3 failures.
**Priority.** must.

#### FR-PIPE-006 — Crash recovery
**Description.** Worker restart MUST find any rows in `*_active` states and
demote them back to `*_pending` (with a journal note) so processing resumes
without manual intervention.
**Source.** Operational reliability.
**Owner phase.** 10.
**Verification.** Phase 10 — `kill -9` mid-task; restart resumes; no row
stuck in `_active` after startup.
**Priority.** must.

#### FR-PIPE-007 — State journal
**Description.** Every state transition MUST append a row to
`pipeline_journal` with `from_status`, `to_status`, `at`, `actor`,
`duration_ms`, and (if applicable) `error_kind`, `error_message`.
**Source.** Auditability.
**Owner phase.** 0 (schema), 10 (use).
**Verification.** Phase 10 — sample row's full journey reconstructable from
journal.
**Priority.** must.

---

### 3.9 UI (FR-UI-*)

#### FR-UI-001 — Time-bucket views
**Description.** The UI MUST provide tabs `Today`, `This Week`, `This Month`
that filter notifications by `announced_at` IST date.
**Source.** User opening; design-decisions.md `L2`.
**Owner phase.** 12.
**Verification.** Phase 12 — each tab shows correct date range; counts
match SQL.
**Priority.** must.

#### FR-UI-002 — Company drill-down
**Description.** A `Company` tab MUST allow searching by name / NSE symbol /
BSE code, and on selection show that company's notification timeline plus a
fundamentals overlay panel.
**Source.** User opening ("company wise notification");
design-decisions.md `L2`.
**Owner phase.** 12.
**Verification.** Phase 12 — search RELIANCE, see notification list +
fundamentals.
**Priority.** must.

#### FR-UI-003 — Default-hide ignored, with review path
**Description.** Default views (Today/Week/Month/Company) MUST hide rows with
`pipeline_status='ignored'`. A separate `Ignored` tab MUST list ignored rows;
an "un-ignore" / bulk un-ignore action MUST be available.
**Source.** User opening ("Do not show me the ignored ones. provide a button
for me to review ignored notification set").
**Owner phase.** 12.
**Verification.** Phase 12 — Today shows no ignored rows; Ignored tab lists
them.
**Priority.** must.

#### FR-UI-004 — Notification card content
**Description.** Each notification card MUST show: headline, company name,
source (BSE/NSE), priority badge, category + group color, announced_at (IST),
Gemma summary (1-2 lines), Gemma impact (1 line), gemini-rr deep-dive snippet
(when available), attachment link (PDF), fundamentals snapshot (mcap, sales
TTM, P/E), mini 30-day price chart, action buttons (mark read, mark junk,
open detail).
**Source.** design-decisions.md `L5` defaults.
**Owner phase.** 12.
**Verification.** Phase 12 — sample card screenshots show every field.
**Priority.** must.

#### FR-UI-005 — Fundamentals overlay
**Description.** Selecting a notification MUST show a side panel with the
metrics listed in design-decisions.md `J2`: mcap, sales (TTM/Q/QoQ%/YoY%),
EBITDA + margin, PAT + margin, EPS, ROCE, ROE, debt (total + net),
FCF (latest, 3y), capex (latest, 5y), P/E, dividend yield, promoter/FII/DII
holdings, sales/PAT 3y/5y CAGRs.
**Source.** User opening ("hook up the screener fundamental data, price
data"); design-decisions.md `J2`.
**Owner phase.** 12.
**Verification.** Phase 12 — selected notification renders all listed
metrics.
**Priority.** must.

#### FR-UI-006 — Filter rules editor
**Description.** A `Filter Rules` tab MUST list all active rules and allow
adding new ones (per FR-FILTER-004), deactivating, and viewing rule
provenance (`created_by`, `confidence`, `reason`).
**Source.** Manageability.
**Owner phase.** 12.
**Verification.** Phase 12 — rule edit cycle works.
**Priority.** must.

#### FR-UI-007 — Health dashboard
**Description.** A `Health` tab MUST show: poller status (last poll time,
rows ingested), worker queue depths per `pipeline_status`, Gemma uptime
(reachable/unreachable), `gemini-rr` quota (remaining per profile), throughput
in last 1h/24h, current SLA breaches.
**Source.** Operability.
**Owner phase.** 12.
**Verification.** Phase 12 — Health tab live during a 1-hour run.
**Priority.** must.

#### FR-UI-008 — Earnings tab placeholder
**Description.** An `Earnings` tab MUST exist with a placeholder banner
indicating earnings tracking is parked. Quarterly-result rows MUST still
appear in Today/Week/Month feeds with their `gemma_deferred_tags=['earnings']`
flag visible.
**Source.** design-decisions.md `K1-K6` (parked); `L2`.
**Owner phase.** 12.
**Verification.** Phase 12 — placeholder visible; quarterly-result rows
display the tag.
**Priority.** must.

#### FR-UI-009 — Single-user, localhost
**Description.** The UI MUST run on `localhost:8501` without authentication.
Multi-user is out of scope for v1.0.
**Source.** design-decisions.md `N1`.
**Owner phase.** 12.
**Verification.** Phase 12 — opens at http://localhost:8501.
**Priority.** must.

#### FR-UI-010 — Page load latency
**Description.** Each tab SHOULD load within 2 seconds on a populated DB
(50k+ rows).
**Source.** UX target.
**Owner phase.** 12.
**Verification.** Phase 12 — timed loads.
**Priority.** should.

---

### 3.10 Operations (FR-OPS-*)

#### FR-OPS-001 — Three-process model
**Description.** The system MUST run as three processes: `poller`, `worker`,
`ui`. Each MUST be independently startable, stoppable, and restartable.
**Source.** design-decisions.md `A4` and `D-09`.
**Owner phase.** 13.
**Verification.** Phase 13 — kill any one process; the other two continue.
**Priority.** must.

#### FR-OPS-002 — Logging
**Description.** All processes MUST log to a rotating file (10 MB × 10
backups) under `G:\market_notification\logs\` AND to stdout. INFO+ to file by
default; WARNING+ to stdout. DEBUG enabled via `MN_DEBUG=1`.
**Source.** design-decisions.md `M3` (default).
**Owner phase.** 0 (logger), 13 (verify rotation).
**Verification.** Phase 13 — log rotates after 10MB; older files kept.
**Priority.** must.

#### FR-OPS-003 — Configuration layering
**Description.** Configuration MUST be layered: defaults from
`config/default.toml`, overlay `config/local.toml` (gitignored), overlay
`.env`, overlay `MN_*` env vars. Higher layers override lower.
**Source.** design-decisions.md `D-09` and standard practice.
**Owner phase.** 0.
**Verification.** Phase 0 — selftest shows env-var override works.
**Priority.** must.

#### FR-OPS-004 — Idempotent bootstrap
**Description.** `python -m scripts.bootstrap_db` MUST be safe to run
multiple times; it MUST create missing tables and not destroy data on
re-run. A `--drop` flag MAY exist for full reset.
**Source.** Operational hygiene.
**Owner phase.** 0.
**Verification.** Phase 0 — run twice; second run is no-op.
**Priority.** must.

#### FR-OPS-005 — Schema migrations via Alembic
**Description.** All post-Phase-0 schema changes MUST be implemented as
Alembic revisions in `src/market_notification/db/migrations/versions/`.
Direct ALTER from app code is forbidden.
**Source.** Maintainability; D-07.
**Owner phase.** 0+.
**Verification.** Spot check: any post-0 schema change has a migration.
**Priority.** must.

#### FR-OPS-006 — Restart policy
**Description.** Each process MUST handle SIGINT cleanly (graceful shutdown
within 10s) and crashes MUST be auto-restartable via the supervisor of the
operator's choice (Windows Task Scheduler, NSSM, manual).
**Source.** PLAN.md §9.2.
**Owner phase.** 13.
**Verification.** Phase 13 — kill+restart.
**Priority.** should.

#### FR-OPS-007 — 24-hour soak
**Description.** Phase 13 MUST include a 24-hour run in which no unhandled
exception occurs and at least 1 notification flows fully through ingestion →
classification → priority → attachment → summarize → (deep-dive if important)
→ done.
**Source.** PLAN.md Phase 13 exit.
**Owner phase.** 13.
**Verification.** Phase 13 — soak report attached.
**Priority.** must.

---

## 4. Non-functional requirements

### 4.1 Performance (NFR-PERF-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-PERF-001 | Filter engine: ≥1000 rows/sec/core | FR-FILTER-006 | Phase 4 |
| NFR-PERF-002 | Classify p95 < 15s on 1-page filing | Operational | Phase 5 |
| NFR-PERF-003 | Summarize p95 < 30s on 1-page filing | FR-SUMM-007 | Phase 8 |
| NFR-PERF-004 | Deep-dive p95 < 90s | gemini-rr §16 latency table | Phase 11 |
| NFR-PERF-005 | UI page load < 2s on 50k-row DB | FR-UI-010 | Phase 12 |
| NFR-PERF-006 | Poll cycle wall-clock < 30s (so 60s budget has headroom) | Operational | Phase 3 |
| NFR-PERF-007 | DB writes: poller MUST sustain 100 rows/min insert without UI lag | Capacity | Phase 13 |

### 4.2 Reliability (NFR-REL-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-REL-001 | No data loss across worker restart | FR-PIPE-006 | Phase 10 |
| NFR-REL-002 | Pipeline status invariant: every row eventually reaches a terminal or `*_dead` state | FR-PIPE-001 | Phase 13 |
| NFR-REL-003 | Backfill is one-shot and idempotent (re-run = no-op) | FR-INGEST-009 | Phase 9 |
| NFR-REL-004 | Read-only sources never mutated (verified via fs lock check) | D-01 | Phase 1 |
| NFR-REL-005 | SLA breach generates alert within 60s of threshold | FR-CLASSIFY-005 | Phase 5 |

### 4.3 Maintainability (NFR-MAINT-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-MAINT-001 | Every concern has an ABC; default and alternative provider classes inherit it | P1 in PLAN.md §2 | Phase 0 |
| NFR-MAINT-002 | Repository pattern enforced; no SQL outside `db/repositories/` | P5 | Code review |
| NFR-MAINT-003 | Per-category priority rules in separate functions | FR-PRIORITY-007 | Phase 6 |
| NFR-MAINT-004 | Per-category deep-dive prompts in separate files | FR-DIVE-004 | Phase 11 |
| NFR-MAINT-005 | All datetimes stored as tz-naive UTC; IST conversion in UI layer only | Coding standard | Code review |
| NFR-MAINT-006 | Docstrings on all public functions/classes | Coding standard | Code review |
| NFR-MAINT-007 | Type hints on all public APIs | Coding standard | mypy |
| NFR-MAINT-008 | Module swap (e.g. classifier provider) requires no caller-side changes | P1 | Phase 11 swap test |

### 4.4 Observability (NFR-OBS-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-OBS-001 | Every state transition journaled | FR-PIPE-007 | Phase 10 |
| NFR-OBS-002 | Every LLM call logs prompt-hash + response length + latency at INFO | Coding standard | Phase 5/8/11 |
| NFR-OBS-003 | Health endpoint shows live queue depths and external-service health | FR-UI-007 | Phase 12 |
| NFR-OBS-004 | Per-step duration_ms in journal | FR-PIPE-007 | Phase 10 |

### 4.5 Security & data hygiene (NFR-SEC-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-SEC-001 | No credentials in code or default config; .env gitignored | Standard | Code review |
| NFR-SEC-002 | No prompt content logged at INFO (only hashes); DEBUG is the only level allowed for prompt-text logging | Privacy | Phase 5/8 |
| NFR-SEC-003 | DB and PDFs gitignored | Standard | Repo state check |
| NFR-SEC-004 | UI listens on localhost only by default | FR-UI-009 | Phase 12 |

### 4.6 Resource budgets (NFR-RES-*)

| ID | Requirement | Source | Verify |
|---|---|---|---|
| NFR-RES-001 | DB size growth ≤ 100 MB / 10k notifications (after backfill) | Capacity | Phase 13 |
| NFR-RES-002 | Log directory rotates and never exceeds 100 MB total | FR-OPS-002 | Phase 13 |
| NFR-RES-003 | PDF dump root: per-company dedup; no growth on re-poll | FR-ATTACH-001 | Phase 7 |
| NFR-RES-004 | Memory: each process under 1 GiB resident, excluding Streamlit cache | Capacity | Phase 13 |

---

## 5. Data specifications

### 5.1 Database schema

The authoritative schema is defined by `src/market_notification/db/models.py`
and described in `PLAN.md` §5. Any change MUST be paired with an Alembic
migration AND an update to `PLAN.md` §5 AND an entry in `design-decisions.md`
§P (in-build decisions).

### 5.2 RawNotification (the canonical input shape)

Defined in `src/market_notification/exchange/base.py`. Fields:

| Field | Type | Notes |
|---|---|---|
| source | `str` | 'BSE' \| 'NSE' |
| seq_id | `Optional[str]` | NSE seq_id; None for BSE |
| headline | `str` | Required, non-empty |
| category | `Optional[str]` | Source-provided raw category |
| subcategory | `Optional[str]` | BSE sub-category; None for NSE |
| body | `Optional[str]` | BSE MORE field (HTML); None for NSE |
| announced_at | `datetime` | Tz-naive UTC |
| exchange_disseminated_at | `Optional[datetime]` | NSE only |
| attachment_url | `Optional[str]` | Full URL ready for HTTP GET |
| attachment_name | `Optional[str]` | Original filename |
| attachment_size | `Optional[str]` | Free-text from API |
| is_critical | `bool` | BSE flag |
| has_xbrl | `bool` | NSE flag |
| symbol | `Optional[str]` | NSE symbol or BSE SCRIP_CD |
| company_name_raw | `Optional[str]` | Source-provided company name |
| isin | `Optional[str]` | NSE-provided ISIN |
| industry_raw | `Optional[str]` | NSE smIndustry |
| raw_json | `str` | Full original API record, JSON-serialized |

### 5.3 SummaryResult (Gemma output)

Defined in `src/market_notification/summarizer/base.py`. Schema in FR-SUMM-002.

### 5.4 ClassificationResult (Gemma classifier output)

Defined in `src/market_notification/classifier/base.py`:

```
{
  "category": str (must be in TAXONOMY),
  "group": str (derived),
  "confidence": float (0..1),
  "reasoning": str,
  "used_taxonomy_version": str,
  "used_prompt_version": str,
  "source": "gemma" | "regex" | "fallback"
}
```

### 5.5 PriorityResult

Defined in `src/market_notification/priority/base.py`:

```
{
  "bucket": "important" | "medium" | "normal" | "ignored",
  "score": int (0..100),
  "reasons": list[str],
  "source": "deterministic" | "llm_override",
  "extracted_amount_cr": Optional[float]
}
```

### 5.6 DeepDiveResult

Defined in `src/market_notification/deep_dive/base.py`:

```
{
  "structured_json": str (valid JSON, schema per category),
  "prose": str (markdown),
  "sector_kpi_findings": list[str],
  "used_prompt_version": str,
  "cache_hit": bool,
  "profile_used": str,
  "latency_s": float,
  "error": Optional[str]
}
```

### 5.7 Filter rule format

JSON object with: `rule_type`, `pattern`, `source` (or null=both), `action`,
`created_by`, `confidence`, `reason`. See `config/filter_rules.json.example`.

---

## 6. Interface specifications

### 6.1 Abstract base classes

The contract for each ABC is defined in its `base.py` file and documented in
`architecture.md` §2. Specifically:

| Module | ABC | Default implementation | File |
|---|---|---|---|
| exchange | `ExchangeFetcher` | `BseFetcher`, `NseFetcher` | `exchange/{bse,nse}_fetcher.py` |
| companies | `CompanyProvider` | `CompositeCompanyProvider` | `companies/composite.py` |
| filter | `FilterEngineBase` | `RegexFilterEngine` | `filter/regex_engine.py` |
| classifier | `Classifier` | `GemmaLlmClassifier` | `classifier/llm_classifier.py` |
| priority | `DeterministicPriority` | `RubricDeterministicPriority` | `priority/deterministic.py` |
| priority | `LlmPriorityOverride` | `GemmaPriorityOverride` | `priority/llm_override.py` |
| attachments | `AttachmentDownloader` | `HttpAttachmentDownloader` | `attachments/downloader.py` |
| attachments | `PdfTextExtractor` | `PdfPlumberExtractor` | `attachments/pdfplumber_extractor.py` |
| attachments | `PdfImageExtractor` | `GemmaVisionExtractor` | `attachments/gemma_vision_extractor.py` |
| attachments | `LinkResolver` | `HttpHeadLinkResolver` | `attachments/link_resolver.py` |
| summarizer | `Summarizer` | `GemmaSummarizer` | `summarizer/gemma_summarizer.py` |
| deep_dive | `DeepDive` | `GeminiRrDeepDive` | `deep_dive/gemini_rr_provider.py` |
| deep_dive | `PromptBuilder` | `CategoryAwarePromptBuilder` | `deep_dive/prompt_builder.py` |
| db | `NotificationRepoBase` | `SqliteNotificationRepo` | `db/repositories/notification_repo.py` |

Replacing a default with an alternative provider MUST require no changes
outside that module's directory and its factory wiring.

### 6.2 Configuration interface

Settings is loaded once via `get_settings()` and is the only legitimate way
to read configuration values. Direct env-var reads (`os.environ[...]`) and
TOML reads outside `config/settings.py` are forbidden.

### 6.3 Logging interface

All modules MUST acquire their logger via:
```python
from market_notification.ops.logging import get_logger
log = get_logger(__name__)
```

`print()` is forbidden in committed code outside the `scripts/` directory.

### 6.4 External service contracts

#### 6.4.1 Ollama (Gemma)

- URL: `http://localhost:11434`
- Models: `gemma4-zenflow-moe:latest` (primary), `qwen2.5:14b` (configured fallback only)
- Endpoint: `POST /api/generate` with `stream=false`, `options.temperature=0.1`, `options.num_predict` per call type
- Timeout: 300s default
- Health probe: `GET /api/tags` returns 200

#### 6.4.2 gemini-rr

- Binary: `C:\Users\user\bin\gemini-rr.cmd` (configurable)
- Invocation: `gemini-rr -p -` with prompt on stdin
- Output convention: first line `# orchestrator: ...`, rest is response
- Cache: 30-day default (no `--no-cache`)
- Outer timeout: `inner * 2 + 30s` per FR-DIVE-011
- Failure modes: rc=0 OK; rc=2 OAuth expired; rc=3 all profiles burnt; rc=4 state lock issue

#### 6.4.3 BSE/NSE APIs

- BSE notifications:
  `GET https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?...`
- NSE notifications:
  `POST https://www.nseindia.com/api/.../getCorporateInfo` (with cookies)
- Both require User-Agent, Referer headers; NSE requires session cookie warm-up
- BSE attachments: `https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}` with `AttachHis/` fallback
- NSE attachments: from `attchmntFile` field, hosted at `nsearchives.nseindia.com`
- Rate limit guidance: BSE ~2s sleep between attachment downloads; NSE ~3s

#### 6.4.4 Read-only data sources

- `G:\Screener_original\screener_util\company_sector_mapping_master.csv`:
  ASCII CSV, ~5,250 rows. Column names: `tags, geography, sector_original,
  Company Name, mcap, URL, BSE Code, NSE Code, DataCompanyID, DataWarehouseID,
  ISIN, Macro, Sector, Industry, BasicIndustry, CompanyFullName, Short_id,
  Concall, credit_report, PPT`. (See actual header in §1 prior survey.)
- `G:\brain\screener_essential.db`: SQLite, 1.3 GB. `companies` table is the
  primary read target.
- `check_pv_df` (location TBD per Q-7): Python helper returning a price-volume
  DataFrame.

---

## 7. State machine specification

The pipeline state machine is defined in
`src/market_notification/pipeline/states.py`. Visual representation in
`PLAN.md` §6.

### 7.1 States and transitions

| From | To | Actor | Notes |
|---|---|---|---|
| (insert) | `ingested` | poller | Initial state |
| `ingested` | `ignored` | poller | Junk filter matched |
| `ingested` | `classify_pending` | poller | Filter passed |
| `classify_pending` | `classify_active` | dispatcher | Worker claim |
| `classify_active` | `priority_pending` | classifier | Success |
| `classify_active` | `classify_failed` | classifier | Error |
| `classify_failed` | `classify_pending` | dispatcher | Retry timer fired |
| `classify_failed` | `classify_dead` | dispatcher | retry_count >= 3 |
| `priority_pending` | `priority_active` | dispatcher | Worker claim |
| `priority_active` | `attachment_pending` | priority | Has attachment |
| `priority_active` | `summarize_pending` | priority | No attachment |
| `priority_active` | `priority_failed` | priority | Error |
| `attachment_pending` | `attachment_active` | dispatcher | Worker claim |
| `attachment_active` | `summarize_pending` | attachment | Success |
| `attachment_active` | `done_deferred` | attachment | Deferred-doc-type set |
| `attachment_active` | `attachment_failed` | attachment | Error |
| `summarize_pending` | `summarize_active` | dispatcher | Worker claim |
| `summarize_active` | `deep_dive_pending` | summarizer | priority=important AND eligible cat |
| `summarize_active` | `done` | summarizer | Otherwise |
| `summarize_active` | `summarize_failed` | summarizer | Error |
| `deep_dive_pending` | `deep_dive_active` | dispatcher | Worker claim |
| `deep_dive_active` | `done` | deep_dive | Success |
| `deep_dive_active` | `deep_dive_failed` | deep_dive | Error |
| `*_failed` | `*_pending` | dispatcher | After backoff |
| `*_failed` | `*_dead` | dispatcher | After retry_max |

### 7.2 Invariants

- I-1: A row's `pipeline_status` MUST always belong to the enum.
- I-2: No row in `*_active` after worker startup (FR-PIPE-006 enforces).
- I-3: `*_dead` rows do not auto-retry.
- I-4: `imported_legacy` rows are not picked by the dispatcher.
- I-5: `cross_exchange_role='duplicate_dropped'` rows do not transition past
  `ingested`.

---

## 8. Error handling specification

### 8.1 Error classes

| Class | Source | Worker action |
|---|---|---|
| `TransientError` | Network timeout, Ollama hiccup, gemini-rr rc=2 | Retry per FR-PIPE-005 |
| `PermanentError` | Schema violation, malformed PDF, gemini-rr all-profiles-burnt continuing past 3 retries | Status to `*_dead` |
| `ConfigError` | Settings missing required values | Process exit at startup |
| `ExternalServiceUnavailable` | Ollama or gemini-rr down for >5 min | Health alert; queue continues |

### 8.2 Logging on error

- `last_error` column populated with truncated error message.
- `pipeline_journal` row appended with `error_kind`, `error_message`.
- WARN log line with `notif_id=...` `step=...` `error_class=...`.

---

## 9. Testing requirements

### 9.1 Unit tests

- One test file per module (`tests/unit/<module>/test_*.py`).
- ABCs covered indirectly via concrete implementations.
- All deterministic priority rules covered with at least one positive and one
  negative case (FR-PRIORITY-006).

### 9.2 Integration tests

- Live Ollama tests under `tests/integration/test_*_live.py`, marked
  `@pytest.mark.live_llm`. Skipped if Ollama unreachable.
- Live BSE/NSE tests marked `@pytest.mark.live_internet`.

### 9.3 End-to-end tests

- `tests/e2e/test_full_pipeline.py` exercises one fixture notification through
  every state, asserting it terminates at `done`.

### 9.4 Coverage target

- ≥80% line coverage of `src/market_notification/` excluding `ui/` and
  per-category prompt files.

---

## 10. Acceptance criteria (per phase)

| Phase | Phase title | Anchor FRs |
|---|---|---|
| 0 | Bootstrap & Foundation | FR-OPS-002, FR-OPS-003, FR-OPS-004, FR-OPS-005, NFR-MAINT-001 |
| 1 | Companies & Read-Only Data | NFR-REL-004 (read-only), schema joins for fundamentals |
| 2 | Exchange Fetchers | FR-INGEST-001 (per-cycle fetch contract), §6.4.3 protocols |
| 3 | Poller, Dedup, Cross-Exchange | FR-INGEST-001..008 |
| 4 | Junk Filter | FR-FILTER-001..003, FR-FILTER-006 |
| 5 | Taxonomy + Classifier | FR-CLASSIFY-001..006 |
| 6 | Priority Engine | FR-PRIORITY-001..007 |
| 7 | PDF Pipeline | FR-ATTACH-001..006 |
| 8 | Summarizer | FR-SUMM-001..007 |
| 9 | Backfill from Brain | FR-INGEST-009..010, NFR-REL-003 |
| 10 | Pipeline Dispatcher | FR-PIPE-001..007 |
| 11 | gemini-rr Deep-Dive | FR-DIVE-001..011 |
| 12 | Streamlit UI | FR-UI-001..010, FR-FILTER-004, FR-FILTER-005 |
| 13 | Operations & Soak | FR-OPS-006, FR-OPS-007, all NFRs |

`VERIFICATION.md` provides the exact test commands and manual checks per
phase.

---

## 11. Traceability matrix (FR → Phase → File)

| FR | Phase | Primary file(s) |
|---|---|---|
| FR-INGEST-001 | 3 | `poller/poller.py` |
| FR-INGEST-002 | 3 | `poller/poller.py` (no time gating) |
| FR-INGEST-003 | 3 | `poller/company_mapper.py` |
| FR-INGEST-004 | 3 | `poller/company_mapper.py` + `historical_symbol_map` |
| FR-INGEST-005 | 3 | `poller/cross_exchange.py` |
| FR-INGEST-006 | 3 | `poller/cross_exchange.py` |
| FR-INGEST-007 | 0/3 | `db/models.py` UNIQUE + `poller/dedup.py` |
| FR-INGEST-008 | 3 | `poller/poller.py` + `notification_poll_state` |
| FR-INGEST-009 | 9 | `scripts/backfill_brain_history.py` |
| FR-INGEST-010 | 9 | `scripts/backfill_screener_original.py` |
| FR-FILTER-001 | 4 | `filter/regex_engine.py` |
| FR-FILTER-002 | 0/4 | `db/models.py` + `filter/regex_engine.py` |
| FR-FILTER-003 | 4 | `scripts/seed_filter_rules.py` + `config/filter_rules.json` |
| FR-FILTER-004 | 12 | `ui/components/filter_sidebar.py` + `ui/pages/filter_rules.py` |
| FR-FILTER-005 | 12 | `ui/pages/ignored.py` |
| FR-FILTER-006 | 4 | `filter/regex_engine.py` (compiled cache) |
| FR-CLASSIFY-001 | 5 | `classifier/taxonomy.py` |
| FR-CLASSIFY-002 | 5 | `classifier/llm_classifier.py` |
| FR-CLASSIFY-003 | 5 | `classifier/llm_classifier.py` (validator) |
| FR-CLASSIFY-004 | 5/10 | `pipeline/dispatcher.py` `claim_next` |
| FR-CLASSIFY-005 | 5 | `pipeline/sla_monitor.py` |
| FR-CLASSIFY-006 | 5 | `classifier/llm_classifier.py` (confidence flag) |
| FR-PRIORITY-001 | 6 | `priority/deterministic.py` + `priority/rubric.py` |
| FR-PRIORITY-002 | 6 | `priority/llm_override.py` |
| FR-PRIORITY-003 | 0/6 | `db/models.py` (both columns) |
| FR-PRIORITY-004 | 6 | `priority/rubric.py` (`_score_quarterly_results`) |
| FR-PRIORITY-005 | 6 | `priority/rubric.py` (`_score_auditor`) |
| FR-PRIORITY-006 | 6 | `priority/thresholds.py` |
| FR-PRIORITY-007 | 6 | `priority/rubric.py` modular layout |
| FR-ATTACH-001 | 7 | `attachments/downloader.py` |
| FR-ATTACH-002 | 7 | `attachments/pdfplumber_extractor.py` |
| FR-ATTACH-003 | 7 | `attachments/gemma_vision_extractor.py` |
| FR-ATTACH-004 | 7/8 | `attachments/deferred_tagger.py` + summarizer |
| FR-ATTACH-005 | 7 | `attachments/link_resolver.py` |
| FR-ATTACH-006 | 7 | `db/models.py` `download_status` |
| FR-SUMM-001 | 8 | `summarizer/gemma_summarizer.py` |
| FR-SUMM-002 | 8 | `summarizer/schema.py` + `summarizer/prompts/summarize_v1.py` |
| FR-SUMM-003 | 8 | `summarizer/prompts/summarize_v1.py` (system prompt) |
| FR-SUMM-004 | 8 | `summarizer/gemma_summarizer.py` (deferred tag merge) |
| FR-SUMM-005 | 8/11 | Caller side; deep-dive reads stored fields |
| FR-SUMM-006 | 8 | `summarizer/queue_retry.py` |
| FR-SUMM-007 | 8 | (latency budget) |
| FR-DIVE-001 | 11 | `deep_dive/gemini_rr_provider.py` + dispatcher trigger |
| FR-DIVE-002 | 11 | `config/default.toml` `[deep_dive].eligible_categories` |
| FR-DIVE-003 | 12 | `ui/components/deep_dive_viewer.py` button |
| FR-DIVE-004 | 11 | `deep_dive/prompts/{category}.py` |
| FR-DIVE-005 | 11 | `deep_dive/sector_kpi_loader.py` |
| FR-DIVE-006 | 11 | `deep_dive/fundamentals_injector.py` |
| FR-DIVE-007 | 11 | `deep_dive/output_parser.py` |
| FR-DIVE-008 | 11 | `deep_dive/gemini_rr_provider.py` (subprocess + parser) |
| FR-DIVE-009 | 11 | (no `--no-cache` flag) |
| FR-DIVE-010 | 11 | `deep_dive/gemini_rr_provider.py` (rc handling) |
| FR-DIVE-011 | 11 | `deep_dive/gemini_rr_provider.py` (timeout calc) |
| FR-PIPE-001 | 0/10 | `pipeline/states.py` + `pipeline/dispatcher.py` |
| FR-PIPE-002 | 10 | `pipeline/dispatcher.py` (single-thread loop) |
| FR-PIPE-003 | 10 | `pipeline/dispatcher.py` `_pick_next` |
| FR-PIPE-004 | 10 | (no preemption — single-threaded) |
| FR-PIPE-005 | 10 | `pipeline/dispatcher.py` retry scheduler |
| FR-PIPE-006 | 10 | `pipeline/dispatcher.py` startup demote |
| FR-PIPE-007 | 0/10 | `db/models.py` `pipeline_journal` + repos |
| FR-UI-001 | 12 | `ui/pages/{today,week,month}.py` |
| FR-UI-002 | 12 | `ui/pages/company.py` |
| FR-UI-003 | 12 | `ui/pages/ignored.py` + sidebar filter |
| FR-UI-004 | 12 | `ui/components/notification_card.py` |
| FR-UI-005 | 12 | `ui/components/fundamentals_panel.py` |
| FR-UI-006 | 12 | `ui/pages/filter_rules.py` |
| FR-UI-007 | 12 | `ui/pages/health.py` |
| FR-UI-008 | 12 | `ui/pages/earnings.py` (placeholder) |
| FR-UI-009 | 12 | `scripts/run_ui.py` (server.address localhost) |
| FR-UI-010 | 12 | (latency budget) |
| FR-OPS-001 | 13 | `scripts/run_*.py` |
| FR-OPS-002 | 0/13 | `ops/logging.py` |
| FR-OPS-003 | 0 | `config/settings.py` |
| FR-OPS-004 | 0 | `scripts/bootstrap_db.py` |
| FR-OPS-005 | 0+ | `db/migrations/` |
| FR-OPS-006 | 13 | `scripts/run_*.py` SIGINT handler |
| FR-OPS-007 | 13 | `verification/phase_13_results/soak_24h_summary.md` |

---

## 12. Out-of-scope for v1.0 (parked)

These requirements are explicitly NOT delivered by v1.0. They MAY be added in
a future spec amendment.

| ID | Item | Source |
|---|---|---|
| OOS-001 | Quarterly earnings extraction (figures parsing, YoY/QoQ deltas) | design-decisions.md K1-K6 (parked) — only tagging in v1.0 |
| OOS-002 | Annual report + investor presentation summarization | design-decisions.md G4 + H2 — separate downstream pipeline |
| OOS-003 | Earnings tracker UI (top-rankings, surprise vs consensus) | design-decisions.md K4-K6 |
| OOS-004 | Multi-user authentication / authorization | design-decisions.md N1 |
| OOS-005 | Cloud deployment | design-decisions.md N2 |
| OOS-006 | Mobile UI / push notifications | design-decisions.md N3 |
| OOS-007 | Real-time intraday price tick data | design-decisions.md J4 |
| OOS-008 | Postgres migration (architecture is ready; physical migration deferred) | D-07 |
| OOS-009 | Second Ollama instance for queue isolation | Q-3 (only if measured contention forces it) |
| OOS-010 | Sector-relative comparison axes for earnings | design-decisions.md K3 ("park for now") |

---

## 13. Open requirements (deferred decisions)

These are documented in `QUESTIONS.md` and will be resolved during the build.
If their resolution materially changes a requirement above, this spec is
amended.

- **Q-1** Brain filter rules export mechanism (Phase 4 entry).
- **Q-2** Screener_original older history scope (Phase 9 entry).
- **Q-3** Ollama queue contention measurement and mitigation (Phase 8 entry).
- **Q-4** Gemma-MoE multimodal capability or alternative VLM (Phase 7 entry).
- **Q-6** Sector taxonomy file existence verification (Phase 11 entry).
- **Q-7** `check_pv_df` location and signature (Phase 1 entry).
- **Q-8** gemini-rr cache-hit metadata parsing (Phase 11 entry).
- **Q-9** Multi-user-ready schema additions (Phase 0 already includes
  `user_id` default 'system' on user-action columns).
- **Q-10** Annual-report downstream pipeline ownership / contract (Phase 13
  forward).

---

## 14. Amendments

(none yet — this section grows as the spec evolves post-v1.0)

```
## Amendment N (YYYY-MM-DD)
**Touches.** FR-... , NFR-...
**Reason.** ...
**New text.**
  > ...
**Backwards compat.** ...
```

---

*End of SPEC.md. This file is the contract — every phase is verified against
its anchor FRs from §10.*
