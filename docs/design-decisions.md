# Market Notification System — Design Decisions

> **Purpose.** Capture every architectural choice so we can later see where the
> built system deviated from the original intent. Fill in each `Answer:` block.
> My recommendation is shown as `Default:` when relevant — feel free to ignore.
>
> **Updated:** 2026-05-07 (created)

---

## Decisions already made

- **D-01.** **Independent system.** No runtime dependency on `G:\brain` or its
  Flask/React app. Brain may be referenced for prompt patterns, taxonomy data,
  and priority rubric, but `market_notification` runs standalone. *Reason given:
  brain is too messy.*
- **D-02.** **Project location.** `G:\market_notification\`
- **D-03.** **Exchange notifications source.** BSE corporate filings + NSE
  corporate announcements, both via their respective public/JSON APIs.
- **D-04.** **LLM stack.** Gemma 4 MoE (`gemma4-zenflow-moe:latest` via Ollama
  at localhost:11434) for low/mid/important summarization; `gemini-rr` for
  important deep-dives; QnA prompt taxonomy.
- **D-05.** **UI.** Streamlit.
- **D-06.** **Polling cadence.** ~1 minute.

---

# A. Architecture & scope

## A1. Code reuse from brain

What level of borrowing is OK from `G:\brain\exchange_util\`?

- (a) **Copy modules verbatim** (poller.py, classifier.py, priority.py, enrichers/) — fastest, but inherits "messy"
- (b) **Copy data only** (taxonomy table, 49-cat priority rubric, regex rule list, prompt templates) — rewrite logic clean
- (c) **Fresh rewrite** — read brain for inspiration, write everything new
- (d) **No look** — design only from your spec, don't read brain

**Default:** (b). Brain's taxonomy + rubric + regex patterns are the result of real labelling work on 80k+ notifications and would be expensive to redo. Logic shells are simple to write fresh.

**Answer:** copy the data and then what ever is pending redo . I agree .  Brain's taxonomy + rubric + regex patterns are the result of real labelling work on 80k+ notifications and would be expensive to redo. Logic shells are simple to write fresh. copy this inside G:\market_notification\ . dont update brain

---

## A2. BSE/NSE fetcher implementation

- (a) **Copy brain's `BSE_fetcher.py` (23 KB) and `NSEFetcher.py` (78 KB) verbatim** — they handle a lot of API quirks (cookies, retries, rate limits, date formats)
- (b) **Copy + clean** — keep the API logic, drop unrelated methods, simplify
- (c) **Write fresh against the public APIs** — cleaner code, but you'll re-discover the quirks

**Default:** (b). The fetchers deal with auth cookies and rate limits that took real effort. But they have 80+ methods, most unused for notifications.

**Answer:** yes copy and clean. and have a separate copy inside G:\market_notification\

---

## A3. Host & runtime

- (a) Same Windows machine as brain — share Ollama @ 11434
- (b) Separate Windows host
- (c) WSL/Linux

**Default:** (a). Gemma is already running there.

**Answer:** Same machine, we have gemma running. I want to create a priority system for gemma. so that our notification is evaluated at first 

---

## A4. Process model

- (a) Single process: poller + summarizer + Streamlit UI under one `python app.py`
- (b) Two processes: background worker (poller + LLM pipeline) + Streamlit UI
- (c) Three: poller, summarizer/dispatcher, UI

**Default:** (b). Streamlit reruns its script on every UI interaction; you don't want that to restart the poller. Worker writes to DB, UI reads.

**Answer:** yes , poller stays independent . I want it to be up and running all the time 

---

# B. Database

## B1. DB engine

- (a) SQLite (`data/notifications.db`)
- (b) DuckDB
- (c) Postgres

**Default:** (a). Brain uses SQLite, single-user analytical workloads run fine, no service to manage.

**Answer:** you decide, but we will in future expand the usecases around notification. Scope for multiple user as well

---

## B2. Schema design

- (a) **Copy brain's schema** (`notifications`, `notification_filter_rules`, `notification_poll_state`, `historical_symbol_map`) — tested at 80k+ rows
- (b) **Adapt brain's schema** — drop columns we won't use (e.g. `insight_status`/`insight_text` legacy from Claude integration), add new ones (`gemma_summary`, `gemini_deep_dive_json`, `earnings_metrics_json`)
- (c) **Design fresh** from current requirements

**Default:** (b). Brain's schema is good but has accreted ~30 columns; we can trim to ~20 and add what we need.

**Answer:** yes do that way. 

---

## B3. Companies table source

Where does the master company list come from?

- (a) `G:\Screener_original\screener_util\company_sector_mapping_master.csv` (5,250 rows, read-only at runtime, refreshed manually)
- (b) `G:\brain\screener_essential.db` (1.3 GB SQLite, has `companies` table with mcap, sales, sector, etc.)
- (c) Parquet files in `G:\brain\parquet_staging\`
- (d) Build own: import CSV + augment from screener.in scrape

**Default:** (a) for the static company-symbol mapping (BSE code, NSE code, name, sector, industry, ISIN). Plus (b) read-only for quarterly sales/mcap to feed priority engine and fundamentals overlay. We don't write to either.

**Answer:** yes all read only , currently use Screener_original but also work with G:\brain\screener_essential.db. Default set it to work with Screener_original

---

# C. Polling & ingestion

## C1. Polling interval

- (a) 60 seconds (brain default)
- (b) 30 seconds (faster reaction; double API load)
- (c) 120 seconds
- (d) Different: ____

**Default:** (a).60 sec

**Answer:**

---

## C2. Polling time window

- (a) 24×7 (companies file off-hours; safest)
- (b) Active window only (07:00–20:00 IST, every minute)
- (c) Market-hours intensive (60s during 09:00–15:30 IST, 5m otherwise)

**Default:** (c). Most filings come during/just-after market hours. Off-hours can be slower.

**Answer:**24×7 

---

## C3. Pre-populated history (backfill)

How much history at first launch?

- (a) None — start fresh from "now"
- (b) Last 7 days
- (c) Last 30 days
- (d) Last 12 months
- (e) All of brain's notifications.db (~80k rows) imported once

**Default:** (b). Useful as a smoke test but not so much that LLM costs balloon.

**Answer:**All of brain's notifications.db (~80k rows) imported once, also check screener_original , there also we download 

---

## C4. NSE+BSE same-event grouping

A company filing with BSE often files the same event with NSE within minutes. Brain assigns a shared `cross_exchange_group_id` UUID via heuristic (same company + announced_at within 30min + headline similarity).

- (a) Copy brain's heuristic
- (b) Stricter (exact headline match within 10min)
- (c) LLM-based deduplication (Gemma judges "is this the same event?")
- (d) Skip grouping — show both filings separately

**Default:** (a). Heuristic is good enough; LLM call per pair is expensive.yes

**Display question (C4b):** When grouped, which filing do we show?
- (a) BSE preferred (PDFs tend to be richer) 
- (b) NSE preferred
- (c) First-seen wins
- (d) Show both side-by-side, expandable

**Default:** (a) for UI; both retained in DB.

**Answer C4:**Heuristic is good enough; LLM call per pair is expensive. Run the LLM only on one . if same document is send by BSE and NSE both m take which ever arrived first and discard the second 

**Answer C4b:**- which ever is earlier, but show only one 

---

# D. Junk removal

## D1. Initial filter rules

Brain has 7 seeded `system` rules + ~10 `auto-learned` ones. Examples:
- Hide categories: `Trading Window`, `Reg. 30 Disclosures`, AGM intimation duplicates, etc.
- Hide regex: `newspaper\s+(advertisement|publication)`, certain LODR XBRL filings.

- (a) Import brain's full rule set (system + learned)
- (b) Import brain's system rules only (skip learned, build fresh from your usage)
- (c) Start empty; learn from "mark as junk" clicks in the UI

**Default:** (a). The learned rules embed your past triage; recreating them is wasted effort.

**Answer:** The learned rules embed your past triage; recreating them is wasted effort. We will deep dive later and update 

---

## D2. UI-driven rule learning

When you mark a notification as junk in the UI:
- (a) Add a category-level rule (every future notification with same category from same source = ignored)
- (b) Add a headline regex rule (extract distinctive phrase, regex-block future ones)
- (c) Just mark this one as ignored, don't learn (rules curated separately)
- (d) Combination: per-click, you choose which rule (if any) to spawn

**Default:** (d). Empowers you, avoids over-eager rule generation that's hard to undo.

**Answer:**Empowers you, avoids over-eager rule generation that's hard to undo. 

---

# E. Taxonomy & classifier

## E1. Taxonomy

- (a) Copy brain's 10 groups × 50 categories verbatim
- (b) Copy + tweak (which groups/categories do you want differently? list any)
- (c) Design fresh

**Default:** (a). The groups are sensible (`Growth & Expansion`, `Deals & Partnerships`, `Capital Actions`, etc.) and the categories are well-tested.

**Answer:**The groups are sensible (`Growth & Expansion`, `Deals & Partnerships`, `Capital Actions`, etc.) and the categories are well-tested.


---

## E2. Classifier strategy

- (a) **Hybrid** — regex rules first (~700 patterns from brain), Gemma fallback for misses → uses ~1-2% LLM calls
- (b) **Gemma-only** — every notification gets an LLM classification → uniform but expensive (more calls per day)
- (c) **Regex-only** — no LLM → fastest, but unclassifiable items become `Uncategorized`

**Default:** (a). Brain's regex layer catches >95% deterministically; Gemma fills in misses.

**Answer:**every non ignored or non junk notification gets LLM classification. if out of 1000, 400 are categorized as low, mid and important . categorize them . i also want notifications to get categorized as soon as possible there shoul be monitoring trigger if it does not get categorized withing 5 min of arrival . Start from latest and move backward to older notification

---

# F. Priority engine

## F1. Source of priority logic

- (a) Copy brain's deterministic engine (`notification_priority.py`, `priority_rubric.md`) — 49 categories, threshold rules using mcap/sales (`capex >5% mcap`, `order >20% annual rev`, etc.)
- (b) Copy + simplify — drop niche rules, keep top 20
- (c) Design fresh

**Default:** (a). Deep work, hard to redo.

**Answer:** yes lets use a , create a separate function and modularize the code so that changes are easy and also easy to read 

---

## F2. Can the LLM override priority?

- (a) Deterministic engine is authoritative; Gemma's suggestion is logged but not used
- (b) Gemma can downgrade only (e.g. mark routine quarterly result as `normal` if it's a newspaper ad)
- (c) Gemma can upgrade only (e.g. flag unusual content the rules missed)
- (d) Gemma fully overrides; rubric is just a starting hint

**Default:** (a) plus (b). Upgrades are risky (LLMs hallucinate importance); downgrades are safer.

**Answer:** Gemma fully overrides; rubric is just a starting hint

---

## F3. Priority output shape

- (a) Categorical: `important / medium / normal / ignored`
- (b) Numeric score 0-100 + categorical bucket derived
- (c) Multi-axis: priority × confidence × urgency

**Default:** (b). Brain already does this internally; UI shows categorical.

**Answer:** yes 

---

## F4. "Highest priority gets processed immediately" semantics

When a new `important` notification arrives while a `medium` is being summarized:

- (a) **Preempt** — pause the medium task, run important now, resume medium
- (b) **Jump queue** — finish current task, important runs next, ahead of waiting medium/normal
- (c) **Fast lane** — separate worker thread/process for important; runs in parallel with the standard worker
- (d) **Batch with weight** — every cycle, important first, then medium, then normal (current brain behavior — not preemptive)

**Default:** (c). Two workers: a "fast lane" for important (runs Gemma + gemini-rr deep-dive immediately) and a "standard lane" for medium/normal (batch). Avoids interrupting in-progress LLM calls.

**Answer:** worker thread is complex, multi llm call is complex, rather complete the existing task then put important one . Then again medium . Important further goes to gemini 

---

# G. PDF download & text extraction

## G1. Download location

- (a) `G:\market_notification\data\pdf\{cid}\{file}.pdf`
- (b) Brain's existing path `D:\Notification Dump\{cid}\` (re-use; no duplicate downloads if brain already pulled it)
- (c) Different: ____

**Default:** (b) if D: drive has space. Brain's downloader has dedup (MD5) — not duplicating its archive saves disk and bandwidth.

**Answer:** lets use D:\Notification Dump\{cid}\ 

---

## G2. Text extractor

- (a) `pdfplumber` (brain's choice; 20 pages cap)
- (b) `PyMuPDF` (faster, better tables)
- (c) `Unstructured` (handles scanned PDFs natively, but heavier deps)
- (d) `pdfplumber` first, fallback to vision-LLM on empty extract

**Default:** (d). Most BSE/NSE PDFs are text-PDFs and pdfplumber works; some scanned filings need OCR/vision. Fallback chain handles both.

**Answer:** default

---

## G3. Image extraction from PDFs

You said "extract images and summarize."

- (a) Extract embedded images, send to Gemma's vision capability (or qwen2.5-vl if Gemma isn't multimodal locally)
- (b) Render each PDF page to PNG, send to vision model only if text extract is empty
- (c) Skip image extraction; text-only

**Default:** (b). Cost-aware — only OCR pages we couldn't extract text from. Whole-document image extraction is overkill for 95% of filings.

**Answer:** yes use gemma MOE to extract image as well 

---

## G4. PDF max pages

- (a) 20 (brain default)
- (b) 50
- (c) Full document, no cap (annual reports can be 200+ pages)
- (d) Adaptive: 20 for routine categories, 100 for `Quarterly Results`, `Annual Results`, `Investor Presentation`

**Default:** (d).

**Answer:** For now dont process Annual report or investor presentation. We will plan that breakup later . lets have 20. I also want a summary of large documents that were not processed. Also sometimes links are provided of documenst , we should open those and check and see if it needs processing 

---

# H. Summarization (Gemma layer)

## H1. Model

- (a) `gemma4-zenflow-moe:latest` (per `GEMMA4_MOE_REFERENCE.md`, ~148 tok/s on GPU)
- (b) `gemma4-zenflow-moe:latest` for important+medium, lighter local model (qwen2.5:7b? llama3.1:8b?) for normal
- (c) Different: ____

**Default:** (a). Single model; simplest. The Gemma MoE has 3.8B active per token (cheap as 7B dense) so quality is fine for normal too.

**Answer:**Default, lets use Gemma MOE

---

## H2. JSON output schema

What does Gemma return per notification?

- (a) Brain's: `{summary, impact, priority}`
- (b) Extended: `{summary, impact, priority, key_figures (list of {label, value, unit}), key_people (list of {name, role}), key_dates (list of {label, date}), attachments_referenced}`
- (c) Custom: list what you want

**Default:** (b). For UI display + earnings tracker we'll want figures and dates structured, not just prose.

**Answer:** For earnings , tag that as earnings , similar to PPT or annual report and credit Note . we will have a separate system to handle that . We should not handle it here . But yes we need a extended list. Ideally once notification comes and I extract I dont want to use the document again 

---

## H3. Fallback when Ollama is down

- (a) Queue + retry every 30s; never lose a notification
- (b) Fallback to gemini-rr (counts against Gemini quota)
- (c) Fallback to qwen2.5:14b on Ollama (if running) — but if Ollama is down this fails too
- (d) Skip; mark `summarize_status='failed'`; surface alert in UI

**Default:** (a). Notifications aren't time-critical at second granularity; retry is fine.

**Answer:** As default

---

# I. gemini-rr deep-dive

## I1. Trigger

When does a notification get a gemini-rr deep-dive?

- (a) Only `priority='important'`
- (b) `important` + `medium` for select categories (Capex, Acquisition, Order Win, USFDA, Credit Rating, Tax/Legal — i.e. brain's enrichable categories)
- (c) On-demand only — user clicks "deep-dive" button in UI
- (d) (a) + (c) — auto for important, manual for others

**Default:** (d). Auto-deep-dive on important keeps the action automatic; manual button for medium gives you control without quota burn.

**Answer:** default 

---

## I2. Eligible categories for deep-dive

Mark which categories warrant a topic-specific deep-dive prompt:

| Category | Default | Your call |
|---|---|---|
| Capacity Expansion / Capex Update | YES | |
| Acquisition | YES | |
| Joint Venture | YES | |
| Order Win / Contract Award | YES | |
| Divestiture / Sale | YES | |
| Merger / Demerger | YES | |
| USFDA (Approval/Warning/Import Alert) | YES | |
| Credit Rating Change | YES | |
| Tax / GST Order | YES | |
| Legal / Litigation | YES | |
| SEBI Order | YES | |
| Quarterly Results | YES (special — earnings tracker) | |
| Equity Dilution (QIP/FPO/Preferential) | YES | |
| Buyback | YES | |
| OFS | YES | |
| Management Change | NO | |
| Board Meeting Outcome | NO (already pass-through reclassified) | |
| Dividend | NO | |
| Cyber Incident | YES | |
| Other Important | YES | |

**Answer / overrides:**

---

## I3. Existing prompt library

Do you have a prompt library for the topic-specific deep-dives, or do I draft from scratch using brain's enricher prompts as inspiration?

- (a) Yes, I'll share — location: ____
- (b) No, draft from brain's enrichers (`capacity_expansion_enricher.py`, `acquisition_enricher.py`, etc.) as a starting point — I review before lock-in
- (c) Draft from your spec verbatim ("if topic is capex, ask capex questions")

**Default:** (b). Brain's enrichers already prompt for the right structured fields; we adapt for gemini-rr's open-consult style.

**Answer:**lets work on a prompt for each category, consult with gemini to make a rich prompt list for each category that we come across. also if required sector taxanomy and KPI aware G:\brain\screener_util\basic_industry_taxonomy.json 
G:\brain\screener_util\basic_industry_taxonomy.json
D:\Annual_report_extract\docs\sector_metrics_kpis.md

 D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md


---

## I4. Output style of gemini-rr deep-dive

- (a) Structured JSON (similar to brain's enrichers — keys: target_size, financing, strategic_rationale, etc.)
- (b) Open consult prose (markdown, sectioned)
- (c) Both — JSON for machine consumption + prose for human reading

**Default:** (c). JSON for the metric-extraction layer, prose for the UI panel.

**Answer:** default

---

## I5. Topic prompts — what questions per topic?

Below are draft topic→question lists. Edit in-place:

### Capex / Capacity Expansion
- Expansion type? (greenfield / brownfield / debottleneck / backward-integration)
- Product / segment being expanded?
- Existing capacity / new capacity / % increase?
- Capex amount? Financing source?
- Timeline (announcement → commissioning)?
- Strategic rationale (demand / margin / market-share)?
- Risks (delay, cost over-run, demand softening)?
- Subsidiary or main entity?
- (Add more) ____

### Acquisition
- Target company name, business, geography, revenue?
- Deal size, payment mode (cash / share-swap / mix)?
- Valuation multiple paid (P/S, P/E, EV/EBITDA)?
- Strategic rationale (vertical / horizontal / geo / capability)?
- Accretion / dilution timeline (when EPS-positive)?
- Funding source, leverage impact?
- Integration risks?
- (Add more) ____

### Order Win
- Order value, customer, scope?
- Order vs. annual revenue %?
- Execution timeline (months / years)?
- Margin profile vs. business as usual?
- Order book impact?
- (Add more) ____

### USFDA
- Approval / warning / import alert / observation type?
- Molecule + market size + brand owner?
- VAI / OAI classification?
- Plant location, products affected?
- Revenue at risk (for warnings) or revenue opportunity (for approvals)?
- (Add more) ____

### Credit Rating Change
- Agency, prior rating, new rating, outlook?
- Drivers (financial / operational / sector)?
- Implications for borrowing cost?
- Cross-cutting view (other agencies' ratings)?
- (Add more) ____

### Tax / Legal
- Demand / penalty amount vs. mcap and net-worth?
- Stage (assessment / appeal / final)?
- Probable outcome / past precedent?
- Provisioned in books?
- (Add more) ____

### Quarterly Results
- Sales YoY / QoQ growth?
- Operating margin change YoY / QoQ?
- PAT YoY / QoQ?
- Segmental performance (which divisions drove change)?
- Management commentary highlights?
- Surprise vs. consensus (if known)?
- Forward guidance updated?
- (Add more) ____

### Equity Dilution / Buyback / OFS
- Issue size, price (vs. market), discount/premium, % equity?
- Use of proceeds?
- Allottees (insiders / strategic / FIIs)?
- Lock-in?
- Buyback: tender vs. open-market, premium, % of equity?
- (Add more) ____

**Answer:** Add/remove/refine the question lists above.

---

## I6. One big call vs. many small

- (a) One gemini-rr call per notification with all topic questions in a single prompt
- (b) One call per question (parallelizable but burns quota)
- (c) One call for structured JSON + one call for prose narrative

**Default:** (c). Splits the cognitive load and lets us reuse JSON for downstream metrics while prose goes to UI.

**Answer:** for gemini, we want the notification text to be sent / we send a sector specific / topic specific prompt . This is gemini specific , for example if there is a chemical we want to know as much as possible about that chemical 

---

## I7. Cache

`gemini-rr` caches by prompt-hash for 30 days by default.

- (a) Use default 30-day cache (notifications don't change after issuance, cache hits = free)
- (b) Shorter (e.g. 7 days)
- (c) `--no-cache` always (waste, but always fresh)

**Default:** (a). Notifications + their PDFs are immutable; cache is pure win.

**Answer:** Okay 

---

## I8. Daily quota / budget cap

`gemini-rr` has 6 profiles totalling ~110 calls/day (2×30 paid + 4×10 free).

- (a) Use whatever is available; failover handles exhaustion
- (b) Budget cap: process at most N important notifications per day; defer rest
- (c) Tier the categories: certain critical categories always run, others queue if quota tight

**Default:** (a). 110/day is plenty for a watchlist; if you somehow exceed, we'll add (b).

**Answer:** yah just keep going till failure 

---

# J. Fundamentals overlay

## J1. Fundamentals data source

For mcap, sales, EBITDA, PAT, ratios:

- (a) `G:\brain\screener_essential.db` SQLite (read-only) — already populated, has the `companies` table
- (b) Parquet files in `G:\brain\parquet_staging\`
- (c) Build own (scrape screener.in or import from CSV)
- (d) Hybrid: read parquet for time series, SQLite for latest snapshot

**Default:** (a). Read-only access only; we never write to brain's DB.

**Answer:** Default

---

## J2. Metrics list to surface in fundamentals overlay

Mark which to include in the per-company side panel:

| Metric | Default | Your call |
|---|---|---|
| Mcap | YES | |
| Sales (TTM, latest Q, QoQ%, YoY%) | YES | |
| EBITDA + EBITDA margin | YES | |
| PAT + PAT margin | YES | |
| EPS | YES | |
| ROCE | YES | |
| ROE | YES | |
| Debt (total + net) | YES | |
| FCF (latest, 3-yr) | YES | |
| Capex (latest, 5-yr) | YES | |
| P/E | YES | |
| P/B | NO | |
| P/S | NO | |
| Dividend yield | YES | |
| Promoter holding %, FII %, DII % | YES | |
| Sales / PAT 3-yr & 5-yr CAGR | YES | |
| Working capital days | NO | |
| Inventory turn | NO | |

**Answer / overrides:**

---

## J3. Time-series depth

- (a) Latest only (one number per metric)
- (b) Last 4 quarters
- (c) Last 8 quarters
- (d) Last 5 years quarterly + last 10 years annual

**Default:** (d) for the side panel chart; (b) injected into LLM prompts.

**Answer:** default

---

## J4. Price data

- (a) NSE bhav copy via brain's `BSE_NSE_report` / `parquet_staging` (read-only)
- (b) yfinance API
- (c) Live tick data (out of scope?)

**Default:** (a). Daily OHLCV only; intraday out of scope.

**Answer:** no here use screener_original -> check_pv_df

---

## J5. Fundamentals injection into LLM

When gemini-rr does a deep-dive, do we automatically inject company fundamentals into the prompt?

- (a) Yes — append a structured block: latest 4Q sales, mcap, sector, peer benchmarks
- (b) Only the metrics relevant to the topic (capex deep-dive gets capex history; acquisition gets sales/EBITDA/cash; USFDA gets US-revenue % if available)
- (c) No — keep prompts pure, let Gemini reason from notification text alone
- (d) Manual — UI button "include fundamentals in deep-dive prompt"

**Default:** (b). Topic-aware injection is the highest-signal-to-prompt-token ratio.

**Answer:** send all fundamental data, let gemini decie. for example if capex is added and we estimate revenue , asset turn is a good way . 

---

# K. Quarterly earnings tracker

## K1. Result extraction source

Brain has a result-extraction pipeline (S49/S50 sessions, mentioned in `pending.md` — "45 packages, 50-company backtest"). It's somewhere in `G:\brain\fundamentals\` or similar.

- (a) Locate brain's pipeline and copy/adapt
- (b) Build fresh: parse PDF tables via pdfplumber + LLM extraction prompt
- (c) Use NSE/BSE XBRL filings (more structured than PDF) — brain has an XBRL parser

**Default:** (b). Brain's pipeline produced "45 packages" — translation: it's not a single function, it's a packaged framework that's awkward to extract. Gemma-based fresh extraction with a tight schema is cleaner than fighting brain's coupling.

**Answer:** yes , lets have a separate module for this . Park for now, but tag the notification 

---

## K2. Metrics tracked per quarter

Mark which:

| Metric | Default | Your call |
|---|---|---|
| Revenue / Sales (consolidated) | YES | |
| Revenue (standalone) | YES | |
| EBITDA | YES | |
| EBITDA margin | YES | |
| PAT (consolidated) | YES | |
| PAT (standalone) | YES | |
| PAT margin | YES | |
| EPS | YES | |
| Operating expenses | NO | |
| Other income | NO | |
| Exceptional items (flag presence) | YES | |
| Tax rate | NO | |
| Segment-level revenue + profit | YES | |
| Segment commentary | YES (text only) | |

**Answer / overrides:**

---

## K3. Comparison axes

- (a) YoY only (vs. same quarter last FY)
- (b) YoY + QoQ
- (c) YoY + QoQ + TTM trend
- (d) (c) + sector-relative ("vs. sector median")

**Default:** (c).

**Answer:** also d if possible , but park it for now 

---

## K4. "Top earnings" ranking

What makes a result "top"?

- (a) Highest sales growth %
- (b) Highest PAT growth %
- (c) Biggest margin expansion (EBITDA bps change)
- (d) Composite score: weighted sales% + PAT% + margin Δ
- (e) All four metrics, user picks ranking column in UI
- (f) Surprise vs. consensus (requires consensus estimates — extra data)

**Default:** (e). Sortable table; user picks lens.

**Answer:** park this for now

---

## K5. Universe for earnings ranking

- (a) All 5,250 companies in `company_sector_mapping_master.csv`
- (b) Mcap > 1000 Cr only
- (c) Personal watchlist (define separately)
- (d) (b) + (c) toggle

**Default:** (d). Default view = mcap > 1000 Cr (cleaner signal); toggle to "watchlist only" for tracking.

**Answer (and provide watchlist if applicable):** park

---

## K6. Earnings UI placement

- (a) Separate "Earnings" tab in Streamlit
- (b) Earnings widgets embedded in Today/Week/Month feeds during result season
- (c) Both — "Earnings" tab is the dashboard; quarterly-result rows in the feed link to it

**Default:** (c). 

**Answer:** park

---

# L. Streamlit UI

## L1. Layout

- (a) Sidebar (filters: priority, category, sector, source, date range) + main pane (notification table) + bottom panel (selected detail with PDF / Gemma summary / gemini-rr deep-dive / fundamentals overlay)
- (b) Three-column: left filters, center list, right detail
- (c) Tabs at top, no detail panel — click row → modal popup
- (d) Different (describe): ____

**Default:** (a). Streamlit handles vertical-stack natively; sidebar for filters is idiomatic.

**Answer:** default

---

## L2. Top-level tabs / pages

Default set:
1. **Today** — today's notifications
2. **Week** — last 7 days
3. **Month** — last 30 days
4. **Company** — company-wise drill-down (search → notification timeline)
5. **Earnings** — quarterly earnings tracker
6. **Ignored** — review-ignored set
7. **Filter rules** — manage junk-removal rules
8. **Health** — poller status, summarizer queue, Gemma uptime, Gemini quota

**Answer:** add/remove tabs, reorder.

---

## L3. "Today" definition

- (a) `announced_at` IST date == today
- (b) `fetched_at` date == today
- (c) Both, separately tabbed

**Default:** (a). Companies file at odd hours; what matters is when *they* announced.

**Answer:**

---

## L4. Ignored toggle UX

- (a) Separate "Ignored" tab (per L2)
- (b) Filter dropdown in sidebar: `[Hide ignored] [Show all] [Show only ignored]`
- (c) Button on main view: "Review ignored set" → opens separate page

**Default:** (a) primary, (b) also present. Tab is the discoverable place; sidebar filter is for quick scoping during regular browsing.

**Answer:**

---

## L5. Notification card content (default visible fields)

Mark which:

| Field | Default | Your call |
|---|---|---|
| Headline | YES | |
| Company name + logo | YES | |
| BSE/NSE source badge | YES | |
| Priority badge | YES | |
| Category + group color | YES | |
| Announced at (IST timestamp) | YES | |
| Gemma summary (1-2 lines) | YES | |
| Gemma impact (1 line) | YES | |
| gemini-rr deep-dive snippet (when available) | YES | |
| Attachment link (PDF) | YES | |
| Fundamentals snapshot (mcap, sales TTM, P/E) | YES | |
| Mini price chart (last 30 days) | YES (small spark) | |
| Action buttons (mark read / mark junk / open detail) | YES | |
| Earnings deltas (for Quarterly Results category) | YES | |

**Answer / overrides:**

---

# M. Operations

## M1. Run mode

- (a) Foreground console (`python app.py`, you start manually each session)
- (b) Windows Service (NSSM-wrapped, auto-start on boot)
- (c) Scheduled Task (every 1 min)
- (d) Docker compose (overkill on Windows host; harder GPU access for Gemma)

**Default:** (a) for development; (b) once stable.

**Answer:**

---

## M2. Gemini quota cap (across all profiles)

- (a) No cap — use what's available
- (b) Hard daily cap: ____ calls (e.g. 80, leaving 30 reserve for ad-hoc consults)
- (c) Soft cap with UI alert at 80%

**Default:** (c) at 80%.

**Answer:**

---

## M3. Logs

- (a) stdout only
- (b) stdout + rolling file under `G:\market_notification\logs\`
- (c) Structured JSON to file (for later ingestion into Loki/Grafana)

**Default:** (b).

**Answer:**

---

# N. Out-of-scope (confirm)

## N1. Authentication / multi-user

- (a) Single-user, no auth, localhost only
- (b) Basic password
- (c) OIDC / SSO

**Default:** (a).

**Answer:**

---

## N2. Cloud deployment

- (a) Out of scope — local only
- (b) In scope — note hosting target

**Default:** (a).

**Answer:**

---

## N3. Mobile UI / push notifications

- (a) Out of scope
- (b) In scope — Pushover / email / browser push for `important` notifications

**Default:** (a) for now; revisit after MVP.

**Answer:**

---

# O. Open additional questions

Anything I haven't asked that you want captured? Add here:

**Answer:**

---

# Phasing — confirm or change

Proposed sequence (~10-12 sessions):

| Phase | Scope | Sessions | Depends on |
|---|---|---|---|
| 0 | Repo bootstrap, env, deps, schema migration | 1 | A,B answers |
| 1 | Data plumbing — companies + notifications schema, smoke seed | 1-2 | B,C answers |
| 2 | Poller (BSE+NSE, dedup, junk filter, classifier) + priority engine | 2 | C,D,E,F answers |
| 3 | Gemma summarizer + PDF text+image pipeline | 1-2 | G,H answers |
| 4 | gemini-rr deep-dive layer with topic prompts + fundamentals injection | 1-2 | I,J answers |
| 5 | Quarterly earnings extraction + tracker | 1-2 | K answers |
| 6 | Streamlit UI (Today/Week/Month/Company/Earnings/Ignored/Health) | 2-3 | L answers |
| 7 | Operations (service, logs, alerts) | 1 | M answers |

**Confirm or rearrange:**

---

# Notes / context for future deviation analysis

When the built system diverges from these answers, append a `## Deviation N` section here noting:
- Which decision deviated
- Why (forced by a discovered constraint? optimization? scope change?)
- Impact

This file is the contract; deviations are documented exceptions.

---

## Deviation 1 — REVERTED (2026-05-07)

**Status.** Initially proposed but reverted same day after user pointed to the
correct file `G:\Screener_original\screener_util\pv_df_check.py`. The function
exists; my earlier grep used the wrong name (`check_pv_df` vs `pv_df_check`).
J4 stands: prices come from `G:\Screener_original\stockDirectory\{id}\{id}_PV.csv`
via a clean port of `pv_df_check`'s logic.

---

## Deviation 2 (Phase 1 entry, 2026-05-07)

**From.** `J1 — Fundamentals: brain's screener_essential.db (read-only)`.
**To.** Fundamentals come from `G:\Screener_original\stockDirectory\{data_company_id}\` per-company folders. Brain's `screener_essential.db` is NOT used for fundamentals (its fundamentals tables are empty: mcap/key_metrics/margins/financials/segments all 0 rows).
**Why.** Discovery during Phase 1 entry — see Q-11. Screener_original's stockDirectory contains the full per-company dataset: PV.csv (price+mcap+shares), PV_metrics.csv (60+ derived metrics), mcap.csv, annual_report, concall, corporate_action.json, credit_rating, plus subdirs for balance-sheet/profit-loss/cash-flow/ratios/quarters/shareholding. This is the canonical source.
**Impact.**
- Phase 1: `ScreenerOriginalCompanyProvider` reads PV.csv for prices and mcap.
- Phase 6 (priority): inspect `quarters/` subdir for quarterly sales. SPEC §FR-PRIORITY-006 thresholds remain achievable.
- Phase 11 (deep-dive): inject full fundamentals block from the various subdirs.
- Brain's screener_essential.db role is reduced to: corporate_actions table (1,761 rows) and possibly notifications backfill (Phase 9). Other usage dropped.
**Reversibility.** Adding brain DB back as a fallback is mechanical — provider classes inherit the same ABC.
