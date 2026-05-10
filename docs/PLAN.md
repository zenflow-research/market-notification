# Market Notification System — Master Plan

> **Version.** v1.0 · 2026-05-07
> **Owner.** research@zenflow.finance
> **Status.** Approved for execution. Built sequentially per `Phase` table in §7.
> **Source of truth for build sequence, module boundaries, schema, and phase exit criteria.**

---

## 0. Reading order for any new session

1. `docs/CONTEXT.md` — 5-minute orientation (what is this, where are we)
2. `docs/STATUS.md` — current phase, last completed step, next step
3. `docs/SPEC.md` — numbered functional + non-functional requirements (the contract)
4. `docs/PLAN.md` (this file) — full plan if you need to look ahead
5. `docs/architecture.md` — module diagrams + ABCs + swappability
6. `docs/QUESTIONS.md` — open issues / deferred decisions
7. `docs/VERIFICATION.md` — how to validate any phase
8. `docs/design-decisions.md` — every architectural choice with rationale
9. `docs/HANDOVER.md` — only when picking up from an interrupted session

---

## 1. Goals & non-goals

### 1.1 Goals (in priority order)

1. **Capture every BSE/NSE corporate filing within ~60s** of public release.
2. **Identify and discard junk** so the user only sees actionable notifications.
3. **Sort by importance** so the user spends time on what moves needles.
4. **Summarize each kept notification with Gemma 4 MoE** (local, free, fast).
5. **Deep-dive every `important` notification with `gemini-rr`** (sector- and topic-aware Q&A).
6. **Surface in a Streamlit UI** — Today / Week / Month / Company / Ignored / Health.
7. **Modular, swappable, extensible** — each LLM, fetcher, classifier, priority engine, summarizer can be replaced without rewriting callers.
8. **Multi-user-ready data layer** — design schema and access pattern so a future Postgres migration is a localized change.

### 1.2 Non-goals (this scope)

- Quarterly earnings tracker — **parked**, only tagged at this stage (D-K1 through K6).
- Annual reports / investor presentations / concall PDFs — **tagged but not summarized** here. They go to a separate downstream pipeline (out-of-scope).
- Mobile UI / push notifications — **parked**.
- Multi-tenant authentication — **parked**, single-user localhost.
- Cloud deployment — **parked**, runs on the same Windows machine as `G:\brain`.
- Rewriting brain's React UI — `G:\brain` continues to do whatever it does; we don't touch it.

---

## 2. Architectural principles

| # | Principle | Implication |
|---|-----------|-------------|
| P1 | **Microstructure: every concern is a module with a stable interface (ABC) and at least one default provider.** | Replacing Gemma with another LLM = write a new provider class, no other code changes. |
| P2 | **Two-process model (poller + worker), plus the UI process.** | Poller is always-on, never blocked by LLM work. Worker drains a priority queue from the DB. UI is pull-only. |
| P3 | **Single-threaded LLM worker, priority queue in DB.** | Per F4: complete current → important next → medium next. Avoids race conditions and double-billing of Gemini quota. |
| P4 | **Database is the only IPC.** | No inter-process queues, sockets, message buses. Postgres-migration-ready. |
| P5 | **Repository pattern for data access.** | One file changes when migrating from SQLite → Postgres. |
| P6 | **Idempotent operations.** | Any worker can crash mid-processing and restart without corruption. State transitions are journalled in the row. |
| P7 | **Deterministic-first, LLM-fallback.** | Regex/rules/thresholds are cheap, fast, and audited. LLM is for what those can't do. |
| P8 | **No commits / no PRs during build.** | Per user instruction: one big commit at the end. No git overhead during construction. |
| P9 | **Documentation and verification per phase.** | Each phase has an exit checklist; we don't move on until verified. |
| P10 | **Brain is a data source, not infrastructure.** | We read brain's `screener_essential.db`, `notifications.db` (one-time history import), and copy their taxonomy/rubric/regex JSON. We never call brain's Python code at runtime. |

---

## 3. System architecture overview

### 3.1 Process topology

```
                            G:\market_notification\
                            
+-----------------------------+   60s    +----------------------------------+
| poller_proc (always-on)     |--------->|  data\notifications.db (SQLite)  |
|   - BSE fetcher             |  insert  |    notifications                 |
|   - NSE fetcher             |          |    notification_filter_rules    |
|   - normalizer              |          |    notification_poll_state      |
|   - dedup + cross-exchange  |          |    historical_symbol_map        |
|   - junk filter (regex)     |          |    pipeline_journal             |
|   - mark pipeline_status='classify_pending'                                |
+-----------------------------+          +----------------------------------+
                                              ^                ^
                                              | rd/wr          | rd
                                              |                |
+-----------------------------+               |                |
| worker_proc (single-thread) |---------------+                |
|   - dispatcher (pick task)  |   priority queue: status,     |
|   - classifier (Gemma)      |   priority, age                |
|   - priority engine (det.)  |                                |
|   - attachment_dl + extract |                                |
|   - summarizer (Gemma)      |                                |
|   - deep_dive (gemini-rr)   |                                |
|     for important only      |                                |
+-----------------------------+                                |
              |                                                 |
              | calls                                           |
              v                                                 |
   +---------------------------+                                |
   | external services         |                                |
   |  - Ollama @11434 (Gemma)  |                                |
   |  - gemini-rr (subprocess) |                                |
   +---------------------------+                                |
                                                                 |
+-----------------------------+                                  |
| streamlit_proc (UI, 8501)   |---------------------------------+
|   - Today / Week / Month   |   read-only queries
|   - Company / Ignored      |   + user actions (mark junk, learn rule, etc.)
|   - Health / FilterRules   |
+-----------------------------+

Read-only data sources (never written to):
  - G:\Screener_original\screener_util\company_sector_mapping_master.csv
  - G:\Screener_original\... check_pv_df  (price/volume)
  - G:\brain\screener_essential.db  (companies, fundamentals, financials)

One-time imports (read once at backfill):
  - G:\brain\data\notifications.db  (~80k historical rows)
  - G:\brain\<filter_rules tables>  (system + auto-learned rules)
  - G:\brain\screener_util\basic_industry_taxonomy.json
  - D:\Annual_report_extract\docs\sector_metrics_kpis.md
  - D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md
```

### 3.2 Pipeline (per notification)

```
inserted_by_poller
    |
    v
[junk_filter]  --(rule match)--> pipeline_status = 'ignored'
    |  (no match)
    v
pipeline_status = 'classify_pending'
    |
    v  (worker picks up)
[deterministic_pre_priority]   <-- runs without LLM, fast
    |
    v
pipeline_status = 'priority_pending_classify'
priority_pre = important|medium|normal|ignored (deterministic)
    |
    v
[gemma_classifier_and_override]
    |
    v
ai_category, ai_priority (final, gemma override)
pipeline_status = 'attachment_pending' (if has_pdf) or 'summarize_pending' (else)
    |
    v
[attachment_downloader]  --> D:\Notification Dump\{cid}\{file}.pdf
    |
    v
[pdf_extractor]  --(text empty)--> [vision_extractor (Gemma MoE multimodal)]
    |
    v
pdf_text + pdf_images_summary stored
pipeline_status = 'summarize_pending'
    |
    v
[gemma_summarizer]  --> {summary, impact, key_figures, key_people, key_dates,
                         attachments_referenced, deferred_doc_tags[], external_links[]}
    |
    v
pipeline_status = 'deep_dive_pending' (if priority='important' and category in I2 list)
                  | 'done' (otherwise)
    |
    v  (only for important)
[gemini_rr_deep_dive]  --> {structured_json, prose, sector_kpi_findings}
    |
    v
pipeline_status = 'done'

Failure paths: every step has a 'failed_<step>' status with retry_count, last_error.
The dispatcher picks up failed-with-retries-remaining after exponential backoff.
```

### 3.3 Module map (microstructure)

```
src/market_notification/
├── config/             # config loader (TOML), env vars
├── db/                 # ORM models, sessions, migrations, repositories
├── companies/          # read-only company + fundamentals data
├── exchange/           # BSE+NSE API fetchers (cleaned copies of brain's)
├── poller/             # always-on poll daemon
├── filter/             # junk-removal rule engine
├── classifier/         # taxonomy + Gemma classifier
├── priority/           # deterministic priority + LLM override
├── attachments/        # PDF download, text extract, vision extract, link follow
├── summarizer/         # Gemma summarizer (extended JSON schema)
├── deep_dive/          # gemini-rr per-category prompts + sector KPIs
├── pipeline/           # dispatcher + state machine + 5-min SLA monitor
├── ui/                 # Streamlit pages + components
└── ops/                # logging, health, supervisor
```

Each module has:
- `base.py` — Abstract Base Class defining the contract.
- `<provider>.py` — at least one concrete implementation.
- `service.py` — public façade that callers import.
- `prompts/` — for LLM-using modules.

---

## 4. Module design detail

### 4.1 `config`

Loads `config/default.toml`, overlays `config/local.toml` (gitignored), exposes a typed `Settings` dataclass. Env vars (`MN_*` prefix) win over file config.

```python
# Pseudocode
class Settings:
    db_url: str                     # default: "sqlite:///data/notifications.db"
    ollama_url: str                 # default: "http://localhost:11434"
    gemma_model: str                # default: "gemma4-zenflow-moe:latest"
    gemini_rr_path: Path            # default: Path("C:/Users/user/bin/gemini-rr.cmd")
    pdf_dump_root: Path             # default: Path("D:/Notification Dump")
    poll_interval_s: int            # default: 60
    pdf_max_pages: int              # default: 20
    sla_classify_minutes: int       # default: 5
    screener_original_root: Path
    brain_screener_essential_db: Path
    backfill_brain_history: bool    # default: True (one-shot)
    log_dir: Path
```

### 4.2 `db`

- `models.py` — SQLAlchemy ORM (NotifBase). See §5 for schema.
- `session.py` — `get_session()` context manager, WAL mode for SQLite.
- `migrations/` — Alembic. Initial migration creates schema; later migrations are versioned.
- `repositories/` — one repo class per aggregate (Notification, FilterRule, Company, PipelineJournal). Public methods are intent-named (`mark_classified`, `claim_next_for_summarize`, etc.). Repositories never leak SQLAlchemy objects above this layer; they return plain dataclasses or dicts.

**Why repository pattern?** When we move to Postgres, only the inside of these classes changes. Service layer stays untouched.

### 4.3 `companies`

Read-only multi-source provider:

```python
class CompanyProvider(ABC):
    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]
    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]
    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]
    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]
    def get_price_series(self, company_id: int, days: int) -> Optional[PriceSeriesDTO]

class ScreenerOriginalCsvProvider: ...      # primary symbol mapping
class BrainScreenerEssentialProvider: ...   # fundamentals + extra metadata
class CompositeCompanyProvider: ...         # tries primary → fallback
```

**Default chain:** ScreenerOriginal CSV (symbol mapping, sector, ISIN) → Brain SQLite (mcap, sales, EBITDA, PAT, ratios). No writes to either source.

### 4.4 `exchange`

```python
class ExchangeFetcher(ABC):
    def fetch_latest(self, n: int) -> list[RawNotification]
    def fetch_for_date(self, date: str) -> list[RawNotification]
    def fetch_attachment(self, url: str) -> bytes

class BseFetcher(ExchangeFetcher): ...   # cleaned copy from brain
class NseFetcher(ExchangeFetcher): ...   # cleaned copy from brain
```

`RawNotification` is a normalized dict with: `source`, `seq_id`, `headline`, `category`, `subcategory`, `body`, `announced_at`, `attachment_url`, `symbol`, `isin`, `is_critical`, `raw_json`.

### 4.5 `poller`

Single class `Poller` with a `start()` / `stop()` / `_run_loop()` daemon.

- Polls NSE then BSE every `poll_interval_s`.
- Normalizes via fetcher.
- Maps `symbol` → `company_id` via `CompanyProvider` + `historical_symbol_map`.
- Computes `headline_hash` for dedup.
- Detects cross-exchange duplicate: same `company_id`, both exchanges, `announced_at` within 10 min, headline cosine ≥ 0.85 → skip the second arrival (per C4).
- Runs `FilterEngine.is_junk()` synchronously (regex only, fast).
- Inserts row with `pipeline_status='classify_pending'` (or `'ignored'` if junk).
- Updates `notification_poll_state` watermark.

### 4.6 `filter`

```python
class FilterEngine:
    def is_junk(self, raw: RawNotification) -> Optional[FilterMatch]
    def add_rule(self, rule: FilterRule, source: str)  # for UI-driven learning
```

Rules are JSON-loaded into memory at startup, refreshed on UI rule edits via SIGHUP-style reload.

### 4.7 `classifier`

```python
class Classifier(ABC):
    def classify(self, notification_id: int) -> ClassificationResult

class GemmaLlmClassifier(Classifier): ...
```

`ClassificationResult = {category, group, confidence, reasoning, used_taxonomy_version}`

The classifier reads notification + (if available) PDF text from DB. Prompts the model with the 10-group/50-cat taxonomy and asks for category + group + confidence. Per E2: every non-junk notification gets LLM classification, latest first.

### 4.8 `priority`

Two-pass:

```python
class DeterministicPriority:
    """Fast, no-LLM, runs in poller or right after classification."""
    def score(self, notification: NotificationDTO,
              company: CompanyDTO) -> PriorityResult

class LlmPriorityOverride:
    """Per F2: Gemma fully overrides; rubric is hint."""
    def override(self, notification: NotificationDTO,
                 deterministic: PriorityResult) -> PriorityResult
```

`PriorityResult = {bucket: 'important'|'medium'|'normal'|'ignored', score: int, reasons: list[str], source: 'deterministic'|'llm_override'}`

Deterministic engine is brain's logic ported to clean Python (modular per-category functions, easy to read).

### 4.9 `attachments`

```python
class AttachmentDownloader:
    def download(self, notification_id: int) -> DownloadResult

class PdfExtractor(ABC):
    def extract(self, pdf_path: Path, max_pages: int) -> ExtractionResult

class PdfPlumberExtractor(PdfExtractor): ...
class GemmaVisionExtractor(PdfExtractor): ...   # fallback when text empty

class LinkResolver:
    """Find URLs inside PDF text; if a link points to another doc,
    decide whether to follow + extract per the deferred-doc-tag rule."""
    def resolve_and_summarize(self, pdf_text: str) -> list[ExternalLinkSummary]
```

PDFs >20 pages: don't process body text here. Tag them as `deferred_doc_type` (annual_report / investor_presentation / large_misc) and store the metadata only. Future pipeline handles them.

### 4.10 `summarizer`

```python
class Summarizer(ABC):
    def summarize(self, notification_id: int) -> SummaryResult

class GemmaSummarizer(Summarizer): ...

@dataclass
class SummaryResult:
    summary: str                     # 1-3 sentences
    impact: str                      # 1 sentence
    key_figures: list[KeyFigure]     # [{label, value, unit}]
    key_people: list[KeyPerson]      # [{name, role}]
    key_dates: list[KeyDate]         # [{label, date, certainty}]
    attachments_referenced: list[str]
    deferred_doc_tags: list[str]     # 'earnings'|'ppt'|'annual_report'|'credit_note'
    external_links: list[ExternalLink]
    confidence: float
    used_model: str
    used_prompt_version: str
```

Per H2: tag deferred-doc types (earnings/ppt/AR/credit-note). The downstream module handles them; this module records the tag and moves on.

### 4.11 `deep_dive` (gemini-rr)

```python
class DeepDive(ABC):
    def deep_dive(self, notification_id: int) -> DeepDiveResult

class GeminiRrDeepDive(DeepDive): ...

class PromptBuilder:
    """Picks a per-category prompt from prompts/<category>.py and
    fills in: notification text, company fundamentals (full set per J5),
    sector taxonomy (basic_industry_taxonomy.json), KPIs
    (sector_metrics_kpis.md), price snapshot, asset-turnover hints."""
    def build(self, notification_id: int) -> str
```

Per category, the prompt asks topic-specific questions (see I5). Per I3, prompts are crafted via gemini-rr consult (we ask gemini "design a great prompt for capex notifications" then save & version it).

Output per I4: `{structured_json, prose, sector_kpi_findings}`.

Per J5: inject ALL fundamental data; gemini decides what's relevant. We hint at asset-turn relationship for capex (so revenue-projection becomes natural).

### 4.12 `pipeline`

```python
class Dispatcher:
    """Single-threaded loop that picks the next task in order:
       1. priority='important' AND status='deep_dive_pending'  (gemini-rr)
       2. priority='important' AND status='summarize_pending'  (Gemma)
       3. status='classify_pending'  (Gemma classify, latest first)
       4. status='attachment_pending'  (download + extract)
       5. status='summarize_pending'  (medium then normal)
       6. failed_* with retry_count<3 and elapsed_since_fail >= backoff
    """
    def run_one_step(self) -> StepResult
    def run_loop(self): ...

class SlaMonitor:
    """Background sub-thread of worker_proc.
    Every 60s: query for any 'classify_pending' rows with age > 5min.
    Logs warning + writes to pipeline_journal."""
```

Per F4: sequential, no preemption. Important always next-up — but never interrupts mid-task.

### 4.13 `ui`

8 Streamlit pages:

1. **Today** — `announced_at` IST date == today, hide ignored, sort: priority desc → time desc.
2. **Week** — last 7 days.
3. **Month** — last 30 days.
4. **Company** — search + select → notification timeline + fundamentals overlay.
5. **Earnings** — placeholder ("parked, see roadmap").
6. **Ignored** — show only `priority='ignored'` rows; bulk un-ignore button.
7. **FilterRules** — view, edit, deactivate seeded + learned rules.
8. **Health** — poller status, queue depth per status, Gemma uptime, gemini-rr quota, last 24h throughput, SLA breaches.

Each page reads via repositories (no direct SQL).

### 4.14 `ops`

- `logging.py` — central logger config; rotating file in `G:\market_notification\logs\` (M3 default).
- `health.py` — exposes a `/health` endpoint via tiny aiohttp server on port 8502.
- `service.py` — bootstrap entry points: `python -m market_notification.poller`, `python -m market_notification.worker`, `python -m market_notification.ui`.

---

## 5. Database schema

SQLite first, WAL mode, with column choices that translate cleanly to Postgres. **Decision D-07: SQLite now, repository pattern + alembic for future Postgres migration.**

### 5.1 `notifications` (primary table)

```
id                          INTEGER PRIMARY KEY AUTOINCREMENT
company_id                  INTEGER NOT NULL          -- = DataCompanyID
source                      TEXT NOT NULL CHECK(source IN ('BSE','NSE'))
seq_id                      TEXT
headline                    TEXT NOT NULL
category                    TEXT
subcategory                 TEXT
body                        TEXT
announced_at                DATETIME NOT NULL
exchange_disseminated_at    DATETIME
attachment_name             TEXT
attachment_url              TEXT
attachment_size             TEXT
is_critical                 INTEGER DEFAULT 0
has_xbrl                    INTEGER DEFAULT 0
symbol                      TEXT
company_name_raw            TEXT
isin                        TEXT
industry_raw                TEXT

-- Cross-exchange grouping
cross_exchange_group_id     TEXT
cross_exchange_role         TEXT  -- 'primary' | 'duplicate_dropped'
                                  -- (we drop duplicates per C4 but log for audit)

-- Pipeline state machine
pipeline_status             TEXT NOT NULL DEFAULT 'ingested'
                            -- one of: ingested, classify_pending, classify_failed,
                            -- priority_pending_classify, attachment_pending, attachment_failed,
                            -- summarize_pending, summarize_failed,
                            -- deep_dive_pending, deep_dive_failed,
                            -- ignored, done
retry_count                 INTEGER DEFAULT 0
last_error                  TEXT
last_status_change_at       DATETIME

-- Junk filter
is_useless                  INTEGER DEFAULT 0
junk_rule_id                INTEGER  -- FK to notification_filter_rules.id (logical)

-- Deterministic priority
det_priority                TEXT          -- 'important'|'medium'|'normal'|'ignored'
det_score                   INTEGER       -- 0-100
det_reasons                 TEXT          -- JSON list[str]

-- LLM classification + override
ai_category                 TEXT
ai_category_group           TEXT
ai_category_confidence      REAL
ai_category_source          TEXT          -- 'gemma'|'fallback'
ai_priority                 TEXT          -- final priority after LLM override
ai_priority_score           INTEGER
ai_priority_reasons         TEXT          -- JSON

-- Attachment
download_status             TEXT DEFAULT 'pending'   -- pending/downloading/done/failed/skipped
local_path                  TEXT
pdf_extracted_text          TEXT
pdf_image_summary           TEXT          -- Gemma MoE vision pass for embedded images
pdf_pages                   INTEGER
deferred_doc_type           TEXT          -- annual_report|investor_presentation|large_misc|earnings|ppt|credit_note

-- Gemma summary (extended schema per H2)
gemma_summary               TEXT          -- prose
gemma_impact                TEXT
gemma_key_figures           TEXT          -- JSON list[{label,value,unit}]
gemma_key_people            TEXT          -- JSON list[{name,role}]
gemma_key_dates             TEXT          -- JSON list[{label,date,certainty}]
gemma_attachments_referenced TEXT         -- JSON list[str]
gemma_deferred_tags         TEXT          -- JSON list[str]
gemma_external_links        TEXT          -- JSON list[{url,target_summary}]
gemma_summarized_at         DATETIME
gemma_model_version         TEXT
gemma_prompt_version        TEXT

-- gemini-rr deep dive
gemini_deep_dive_json       TEXT          -- structured JSON output
gemini_deep_dive_prose      TEXT          -- prose narrative
gemini_sector_kpi_findings  TEXT          -- JSON
gemini_dive_at              DATETIME
gemini_prompt_version       TEXT
gemini_cache_hit            INTEGER

-- User workflow
is_read                     INTEGER DEFAULT 0
selected_for_report         INTEGER DEFAULT 0
read_at                     DATETIME
user_notes                  TEXT

-- Metadata
fetched_at                  DATETIME DEFAULT CURRENT_TIMESTAMP
raw_json                    TEXT

-- Indexes
UNIQUE(source, company_id, announced_at, headline)   -- dedup
INDEX(pipeline_status)
INDEX(announced_at DESC)
INDEX(ai_priority, announced_at DESC)
INDEX(company_id, announced_at DESC)
INDEX(cross_exchange_group_id)
INDEX(pipeline_status, ai_priority, announced_at DESC)   -- dispatcher
```

### 5.2 `notification_filter_rules`

```
id, rule_type, pattern, source, action ('hide'|'block'),
created_by ('system'|'user'|'auto'), confidence, reason,
is_active, created_at
UNIQUE(rule_type, pattern, source)
```

### 5.3 `notification_poll_state`

```
id, source UNIQUE, last_poll_at, last_seq_id, last_date,
records_fetched, status, error_message, updated_at
```

### 5.4 `historical_symbol_map`

```
id, old_symbol, source, successor_company_id, mapping_type, old_company_name, notes
UNIQUE(old_symbol, source)
```

### 5.5 `pipeline_journal`

Audit log; one row per state transition.

```
id, notification_id, from_status, to_status, at, actor, duration_ms,
error_kind, error_message
INDEX(notification_id, at)
INDEX(at, actor)
```

### 5.6 `taxonomy_version`

Tracks which taxonomy version was used to classify each notification (so re-classification is possible).

```
id, version_tag, taxonomy_json, created_at
```

### 5.7 `prompt_version`

Same idea for prompts (Gemma summarizer, gemini-rr per-category).

```
id, scope ('classifier'|'summarizer'|'deep_dive_<category>'),
version_tag, prompt_text, created_at, retired_at
```

---

## 6. Pipeline state machine

```
ingested
   |  (poller computes filter)
   v
[ junk? ]
   | yes                    | no
   v                        v
ignored               classify_pending
   |                        |
   |                  (worker pulls latest first)
   |                        v
   |                  [Gemma classify]
   |                        |
   |                        v
   |                  ai_category, ai_category_group set
   |                  priority_pending_classify
   |                        |
   |                  [DeterministicPriority]
   |                  [LlmPriorityOverride]
   |                        v
   |                  ai_priority set
   |                        |
   |                  attachment available?
   |                       /\
   |                  yes /  \ no
   |                     v    v
   |       attachment_pending   summarize_pending
   |             |
   |             v
   |       [Downloader → Extractor]
   |       deferred_doc?
   |        /        \
   |       yes        no
   |        |          |
   |        v          v
   |  done(deferred)  summarize_pending
   |                        |
   |                        v
   |                  [GemmaSummarizer]
   |                        |
   |                  ai_priority == important?
   |                       /\
   |                      / no
   |                     v   v
   |        deep_dive_pending  done
   |                |
   |                v
   |          [GeminiRrDeepDive]
   |                |
   |                v
   |              done
```

**Terminal states:** `ignored`, `done`, `done(deferred)`.
**Failure states:** `<step>_failed` — dispatcher retries with exponential backoff (`2,4,8` minutes), max 3 retries, then `<step>_dead` (manual intervention required).

**SLA monitor:** Any row in `classify_pending` for >5 minutes → log a warning + emit `Health` page alert (per E2 instruction).

---

## 7. Phasing

Each phase has: **Scope · Deliverables · Dependencies · Exit criteria · Verification reference.**

Each phase ends with VERIFICATION before moving to the next. If a phase can't pass verification, the issue goes to `QUESTIONS.md` and we either fix or descope.

### Phase 0 — Bootstrap & Foundation

**Scope.** Project skeleton, dependencies, env config, logging, alembic, schema migration #1, repository pattern, smoke unit tests.

**Deliverables.**
- Directory tree per §3.3 with `__init__.py` and ABC stubs.
- `pyproject.toml` (uv-managed; deps: sqlalchemy, alembic, pdfplumber, requests, beautifulsoup4, streamlit, ollama, click, pydantic, pytest).
- `.env.example`, `config/default.toml`.
- `src/market_notification/db/models.py` complete per §5.
- `migrations/versions/0001_init.py` runs cleanly.
- `src/market_notification/db/repositories/notification_repo.py` with stub methods + tests.
- `src/market_notification/ops/logging.py` writes to file + stdout.
- `scripts/bootstrap_db.py` creates the DB and applies migrations.

**Dependencies.** Only the answers in §A, §B, §M of `design-decisions.md`.

**Exit criteria.**
- `python -m market_notification.scripts.bootstrap_db` exits 0 and creates `data/notifications.db` with all tables present.
- `pytest tests/unit/test_db_models.py` passes (3 tests minimum: insert one Notification, query by company, transition pipeline_status).
- `python -m market_notification.ops.logging --selftest` writes a test line to log file.
- All ABCs defined for: ExchangeFetcher, Classifier, Summarizer, DeepDive, PdfExtractor, CompanyProvider.

**Verification reference.** `docs/VERIFICATION.md` §Phase 0.

**Estimated tool calls.** ~80.

---

### Phase 1 — Companies & Read-Only Data Sources

**Scope.** `companies/` module: CSV provider (Screener_original) as primary, brain SQLite as fallback for fundamentals, price provider via `check_pv_df`.

**Deliverables.**
- `companies/csv_source.py` — load 5,250 rows, lookup by BSE code / NSE symbol / ISIN.
- `companies/brain_db_source.py` — read-only connect to `G:\brain\screener_essential.db`, expose fundamentals.
- `companies/price_source.py` — wrap `check_pv_df` from Screener_original.
- `companies/composite.py` — multi-source provider per ABC.
- `tests/unit/test_company_provider.py`.

**Dependencies.** Phase 0.

**Exit criteria.**
- Load 5,250 companies from CSV.
- Round-trip lookup: BSE code 533022 → company_id 11 → fundamentals (mcap, sales) → price last 30 days.
- Fundamentals are not null for at least 90% of companies in CSV (otherwise we have a join issue).

**Verification reference.** `docs/VERIFICATION.md` §Phase 1.

**Estimated tool calls.** ~50.

---

### Phase 2 — Exchange Fetchers

**Scope.** Cleaned BSE + NSE fetchers, normalizer.

**Deliverables.**
- `exchange/bse_fetcher.py` — copy + clean of brain's `BSE_fetcher.py`, only the methods we need (notification fetch, attachment fetch, dedup helpers).
- `exchange/nse_fetcher.py` — same for `NSEFetcher.py`.
- `exchange/normalizer.py` — convert raw API response → `RawNotification` dataclass.
- `tests/integration/test_fetchers_smoke.py` — live call to BSE + NSE, count > 0, schema valid.

**Dependencies.** Phase 0.

**Exit criteria.**
- `bse_fetcher.fetch_latest_for_today()` returns ≥10 rows during market hours.
- `nse_fetcher.fetch_latest_announcements(50)` returns ≥10 rows.
- Each row has: source, headline, announced_at, attachment_url (or empty), symbol or BSE_CD.
- Normalizer handles all date formats observed in brain's `_NSE_DATE_FMTS` and `_BSE_DATE_FMTS`.

**Verification reference.** `docs/VERIFICATION.md` §Phase 2.

**Estimated tool calls.** ~80.

---

### Phase 3 — Poller, Dedup, Cross-Exchange Grouping

**Scope.** Always-on poller daemon, symbol→company mapping, dedup, cross-exchange grouping.

**Deliverables.**
- `poller/poller.py` — `Poller` class with `start/stop/_run_loop/poll_once`.
- `poller/dedup.py` — `(source, company_id, announced_at, headline)` uniqueness check.
- `poller/cross_exchange.py` — per C4: heuristic (same company + ±10min + headline cosine ≥0.85), drop second arrival, mark with role.
- `poller/company_mapper.py` — symbol/BSE_CD → company_id with cache + historical_symbol_map fallback.
- `scripts/run_poller.py` — entry point with graceful SIGINT.
- `tests/integration/test_poller_short_run.py` — runs poller for 90s, asserts rows inserted.

**Dependencies.** Phase 0, 1, 2.

**Exit criteria.**
- Run poller 10 minutes with no exception. Log shows BSE+NSE poll cycles.
- ≥1 cross-exchange duplicate detected and marked `cross_exchange_role='duplicate_dropped'` (run during a busy period; if no duplicates seen, simulate via fixture).
- `notification_poll_state` updated for both BSE and NSE.
- `pipeline_journal` shows `ingested → classify_pending` transitions.

**Verification reference.** `docs/VERIFICATION.md` §Phase 3.

**Estimated tool calls.** ~120.

---

### Phase 4 — Junk Filter

**Scope.** Filter rule engine; seeded rules from brain (system + auto-learned).

**Deliverables.**
- `filter/filter_engine.py`.
- `config/filter_rules.json` — bootstrapped from brain (D1 default).
- `scripts/seed_filter_rules.py` — one-time loader.
- `tests/unit/test_filter_engine.py` — assert rule matches, rule misses.

**Dependencies.** Phase 0, 3.

**Exit criteria.**
- Rules loaded.
- Sample 200 ingested notifications — engine flags ≥10% as junk (sanity, not exact %).
- Filter performance: 1000 notifications classified in <1s (regex-only path).

**Verification reference.** `docs/VERIFICATION.md` §Phase 4.

**Estimated tool calls.** ~50.

---

### Phase 5 — Taxonomy + Gemma Classifier

**Scope.** 10×50 taxonomy in code, Gemma LLM classifier (per E2: every non-junk notification gets LLM classification, latest-first).

**Deliverables.**
- `classifier/taxonomy.py` — `TAXONOMY` const (copied from brain) with derived lookups.
- `classifier/llm_classifier.py` — Gemma via Ollama, JSON-structured output.
- `classifier/prompts/classify_v1.py` — prompt with the 50 categories enumerated.
- `pipeline/sla_monitor.py` — 5-min SLA with alert hook.
- Worker integration: `dispatcher` claims `classify_pending` rows latest-first.
- `tests/unit/test_classifier_prompt.py` — golden prompt snapshot.
- `tests/integration/test_classifier_live.py` — classify 20 known notifications, assert ≥80% match expected category (using brain's labels as ground truth).

**Dependencies.** Phase 0, 3, 4 + Ollama running with `gemma4-zenflow-moe:latest`.

**Exit criteria.**
- 20 sample classifications correct ≥80%.
- SLA monitor logs warning when a notification stays in `classify_pending` >5min (verified by injecting one).
- Order-by-newest verified via journal inspection.

**Verification reference.** `docs/VERIFICATION.md` §Phase 5.

**Estimated tool calls.** ~120.

---

### Phase 6 — Priority Engine

**Scope.** Deterministic engine (modular, per-category functions) + LLM override step.

**Deliverables.**
- `priority/rubric.py` — copied data: `CATEGORY_PRIORITY` dict + per-category modular functions (one per stage rule, easy-read).
- `priority/thresholds.py` — `_extract_amount_cr`, mcap%, sales%, etc.
- `priority/deterministic.py` — composes rubric + thresholds.
- `priority/llm_override.py` — uses summary input to allow Gemma to upgrade/downgrade per F2.
- `tests/unit/test_priority_*.py` — at least 10 cases covering every category in the rubric.

**Dependencies.** Phase 0, 5.

**Exit criteria.**
- 1000 backfilled rows scored. Distribution roughly: important 5-15%, medium 20-30%, normal 50-65%, ignored 5-10% (matches brain's history loosely).
- Newspaper-ad rule kicks in (priority=ignored).
- Auditor qualification rule kicks in (score ≥85).
- Override fires on ≥1 case where LLM disagrees with deterministic.

**Verification reference.** `docs/VERIFICATION.md` §Phase 6.

**Estimated tool calls.** ~100.

---

### Phase 7 — PDF Pipeline (Download, Extract, Vision, Link Follow)

**Scope.** Attachment download, pdfplumber primary, Gemma MoE vision fallback, link resolver.

**Deliverables.**
- `attachments/downloader.py` — to `D:\Notification Dump\{cid}\` per G1, MD5 dedup.
- `attachments/pdfplumber_extractor.py` — first 20 pages.
- `attachments/gemma_vision_extractor.py` — render PDF page → PNG → Gemma MoE vision call (per G3).
- `attachments/link_resolver.py` — find URLs, fetch HEAD, decide if doc-type warrants follow.
- `attachments/deferred_tagger.py` — annual_report / investor_presentation / earnings / ppt / credit_note (G4 instruction).
- `tests/integration/test_pdf_pipeline.py`.

**Dependencies.** Phase 0, 3.

**Exit criteria.**
- 50 sample notifications: ≥90% have non-empty `pdf_extracted_text` OR a `pdf_image_summary`, OR `deferred_doc_type` set.
- Vision fallback fires on ≥1 known scanned PDF (use a fixture).
- Link resolver finds ≥1 outbound URL on ≥1 fixture PDF.

**Verification reference.** `docs/VERIFICATION.md` §Phase 7.

**Estimated tool calls.** ~150.

---

### Phase 8 — Gemma Summarizer (extended schema)

**Scope.** Full summarization step with extended JSON schema per H2.

**Deliverables.**
- `summarizer/gemma_summarizer.py`.
- `summarizer/prompts/summarize_v1.py` — extended schema instructions.
- `summarizer/schema.py` — Pydantic models for `SummaryResult`.
- `summarizer/queue_retry.py` — H3 default behavior (queue + retry on Ollama down).
- `tests/integration/test_summarizer_live.py` — 20 sample notifications, assert all required JSON keys present.

**Dependencies.** Phase 0, 5, 7 + Ollama.

**Exit criteria.**
- 20 summaries produced, all schema-valid.
- Retry mechanism verified by simulating Ollama down for 30s.
- Per-call latency p95 < 30s for typical 1-page notification.
- Deferred-doc tag set when category is `Quarterly Results` etc.

**Verification reference.** `docs/VERIFICATION.md` §Phase 8.

**Estimated tool calls.** ~100.

---

### Phase 9 — Backfill from Brain

**Scope.** One-time historical import (~80k rows) from brain's `notifications.db` per C3.

**Deliverables.**
- `scripts/backfill_brain_history.py` — read-only attach to brain's DB, transform → our schema, bulk insert.
- Mark imported rows with `pipeline_status='imported_legacy'` (excluded from worker by default; manual re-classify possible later).
- Also import any older history present in `G:\Screener_original` if found (per user's note in C3).
- `scripts/backfill_screener_original.py` (if applicable).

**Dependencies.** Phase 0, 1.

**Exit criteria.**
- Row count in our DB ≈ row count in brain's DB (allow tolerance for rows with corrupt data).
- Sample 10 imported rows: same headline, announced_at, source as brain's source.
- `pipeline_status='imported_legacy'` for all imported.

**Verification reference.** `docs/VERIFICATION.md` §Phase 9.

**Estimated tool calls.** ~80.

---

### Phase 10 — Pipeline Dispatcher (sequential priority queue)

**Scope.** The single-threaded worker that drives the state machine per F4.

**Deliverables.**
- `pipeline/dispatcher.py` — picks next task per priority order in §4.12.
- `pipeline/states.py` — Enum.
- `scripts/run_workers.py` — bootstrap.
- `tests/integration/test_dispatcher_order.py` — burst of mixed priorities, assert process order.

**Dependencies.** Phases 0, 4, 5, 6, 7, 8.

**Exit criteria.**
- Inject a burst of 20 notifications (5 important, 10 medium, 5 normal). Worker processes important first (after current task), then medium, then normal.
- No race conditions: kill+restart worker mid-task, no duplicate processing.

**Verification reference.** `docs/VERIFICATION.md` §Phase 10.

**Estimated tool calls.** ~100.

---

### Phase 11 — gemini-rr Deep-Dive

**Scope.** Per-category prompts, sector taxonomy + KPI awareness, gemini-rr subprocess invocation, JSON+prose output.

**Deliverables.**
- `deep_dive/gemini_rr_provider.py` — `subprocess.run` to `C:/Users/user/bin/gemini-rr.cmd -p -`.
- `deep_dive/prompts/{capex,acquisition,order_win,usfda,credit_rating,tax_legal,jv,divestiture,merger,sebi_order,equity_dilution,buyback,ofs,cyber_incident,other_important,quarterly_results}.py` — 16 per-category prompt builders per I2 list.
- `deep_dive/sector_kpi_loader.py` — loads `basic_industry_taxonomy.json`, `sector_metrics_kpis.md`, `concall_extraction_taxonomy.md`.
- `deep_dive/fundamentals_injector.py` — full fundamentals block per J5.
- `deep_dive/output_parser.py` — split JSON + prose, persist both.
- `tests/integration/test_deep_dive_live.py` — 5 fixture notifications across 5 categories.

**Dependencies.** Phase 0, 5, 6, 8 + gemini-rr installed.

**Exit criteria.**
- 5 deep-dives across 5 categories produce both JSON + prose, schema-valid.
- Cache hit observed when same notification re-deep-dived.
- Sector KPIs surface in the output (e.g. "asset turnover" mentioned in capex deep-dive output).

**Verification reference.** `docs/VERIFICATION.md` §Phase 11.

**Estimated tool calls.** ~150.

---

### Phase 12 — Streamlit UI

**Scope.** All 8 pages, sidebar filters, notification card, fundamentals overlay.

**Deliverables.**
- `ui/app.py`.
- `ui/pages/{today, week, month, company, earnings, ignored, filter_rules, health}.py`.
- `ui/components/{notification_card, fundamentals_panel, filter_sidebar, price_chart, deep_dive_viewer}.py`.
- `scripts/run_ui.py`.

**Dependencies.** Phase 0, 1, 6, 8, 11 (have data to display).

**Exit criteria.**
- Open Streamlit on localhost:8501. Each tab loads <2s.
- Today tab shows ≥1 notification (run after poller has been on a few minutes).
- Mark a notification as junk → filter rule editor reflects.
- Health tab shows queue depth, Gemma uptime, gemini quota.
- Ignored tab visible by default, hidden behind sidebar toggle on other tabs.

**Verification reference.** `docs/VERIFICATION.md` §Phase 12.

**Estimated tool calls.** ~250.

---

### Phase 13 — Operations & End-to-End Soak

**Scope.** Logging polish, health endpoint, supervisor scripts, README, soak test, final commit.

**Deliverables.**
- `ops/logging.py` polished.
- `ops/health.py` — HTTP `/health` per process.
- `scripts/{run_poller, run_workers, run_ui}.py` — production-ish entry points.
- `README.md` updated with run instructions.
- 24-hour soak test report in `verification/phase_13_soak.md`.
- `git init` + one big commit.

**Dependencies.** All previous phases.

**Exit criteria.**
- Run poller + worker + UI for 24h. Zero unhandled exceptions in logs.
- Throughput: ≥X classified per hour, ≥Y summarized per hour, ≥Z deep-dived per day.
- All exit criteria of all prior phases still hold.
- README run instructions verified by clean restart.

**Verification reference.** `docs/VERIFICATION.md` §Phase 13.

**Estimated tool calls.** ~100.

---

### Phase budget summary

| Phase | ~Tool calls | Notes |
|---|---:|---|
| 0 | 80 | Bootstrap |
| 1 | 50 | Companies |
| 2 | 80 | Fetchers |
| 3 | 120 | Poller |
| 4 | 50 | Filter |
| 5 | 120 | Classifier |
| 6 | 100 | Priority |
| 7 | 150 | PDF |
| 8 | 100 | Summarizer |
| 9 | 80 | Backfill |
| 10 | 100 | Dispatcher |
| 11 | 150 | Deep-dive |
| 12 | 250 | UI |
| 13 | 100 | Ops + soak |
| **Total** | **~1530** | Spans ~2 sessions of 1000 tool calls each. |

**Session-break strategy:** When `STATUS.md` shows a session crossing ~700 tool calls, write `HANDOVER.md` and stop in the middle of the current phase's verification (never mid-implementation) so the next session can pick up cleanly.

---

## 8. Coding standards & conventions

- **Python 3.10+** (decision D-08).
- **Type hints everywhere** including `from __future__ import annotations`.
- **Docstrings on every public function/class** (Google style).
- **No emojis in code.** Logs, comments, identifiers — ASCII only (Windows cp1252 console safety).
- **`from <module>.base import <ABC>`** at all caller sites; concrete imports only in factories.
- **Repository methods are intent-named:** `claim_next_for_classify()`, not `select_one()`.
- **No raw SQL outside repositories.**
- **Unit tests live alongside code** (`tests/unit/<module>/`); integration in `tests/integration/`.
- **Logging at WARNING+ goes to console; INFO+ goes to file; DEBUG only to file when `MN_DEBUG=1`.**
- **All datetimes are timezone-aware** (`datetime.now(tz=timezone.utc)`); IST conversion only at UI layer.
- **No `print()`** in committed code outside `scripts/`. Use logger.
- **Pydantic v2** for config and DTOs.
- **Click** for script CLIs.

---

## 9. Operational notes

### 9.1 Running the system

```powershell
# Process 1 (always-on)
python -m market_notification.scripts.run_poller

# Process 2 (always-on, single-threaded worker)
python -m market_notification.scripts.run_workers

# Process 3 (UI, on demand)
python -m market_notification.scripts.run_ui
# Open http://localhost:8501
```

### 9.2 Restart policy

- Poller: auto-restart with exponential backoff on crash (2s, 4s, 8s, 16s, max 60s).
- Worker: same.
- UI: manual restart, Streamlit handles its own reload.

### 9.3 Monitoring

- `Health` Streamlit tab.
- Log files in `G:\market_notification\logs\` (rotating, 10MB × 10).
- `pipeline_journal` table for forensics.

### 9.4 Backups

- SQLite DB: nightly copy to `G:\market_notification\backups\YYYY-MM-DD\`.
- Out of MVP scope; mentioned for future.

---

## 10. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Ollama gets queued behind brain's concall workload, our notifications stall | M | Plan §A3 option (c): accept FIFO; Gemma is fast (~148 tok/s). If problematic, dedicate a second Ollama instance on different port. |
| Gemini-rr profile burnout on busy days | L | gemini-rr handles failover automatically; 110 calls/day is plenty for `important` notifications. |
| Schema drift between our DB and brain's during backfill | M | Backfill is one-shot read-only at Phase 9. Brain's schema doesn't change after that. Mark imports clearly. |
| BSE/NSE API change breaks fetchers | H | Live integration tests in CI; alarms if 0 rows fetched for 60+ min during market hours. |
| PDF extraction fails on novel formats | M | Vision fallback (Gemma MoE). Worst case: tag as `large_misc` and skip body text. |
| Gemma classifier hallucinates non-existent categories | M | Prompt enumerates the 50 categories; output validator rejects non-taxonomy answers and falls back to `Uncategorized`. |
| Deterministic priority + LLM override conflict semantics confusing in UI | L | Show both: "Det: medium → Gemma override: important — reason: ...". User trusts override per F2 but can audit. |
| 5-min SLA monitor noisy if Gemma is slow | M | Tune SLA to 10min if needed; configurable. |

---

## 11. External dependencies

### 11.1 Read-only data sources

- `G:\Screener_original\screener_util\company_sector_mapping_master.csv` (5,250 rows)
- `G:\Screener_original\` — `check_pv_df` for price/volume (per J4)
- `G:\brain\screener_essential.db` — fundamentals (per J1)

### 11.2 One-time copies

- `G:\brain\data\notifications.db` — historical backfill (Phase 9)
- `G:\brain\<filter rules table>` — seeded filter rules (Phase 4)
- `G:\brain\screener_util\basic_industry_taxonomy.json` — sector/KPI taxonomy
- `D:\Annual_report_extract\docs\sector_metrics_kpis.md` — sector KPIs
- `D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md` — concall topic taxonomy
- Brain's `notification_priority.py` — for porting CATEGORY_PRIORITY + threshold logic
- Brain's `notification_classifier.py` — for porting RULES (regex patterns)
- Brain's `BSE_fetcher.py` + `NSEFetcher.py` — for cleaned copy (Phase 2)

### 11.3 Runtime services

- Ollama @ `localhost:11434` with `gemma4-zenflow-moe:latest` loaded.
- gemini-rr binary at `C:\Users\user\bin\gemini-rr.cmd`.
- BSE / NSE public APIs (no key required).

### 11.4 Python deps (initial)

```
sqlalchemy >= 2.0
alembic >= 1.13
pdfplumber >= 0.10
PyMuPDF >= 1.24            # for vision fallback render-to-PNG
requests >= 2.31
beautifulsoup4 >= 4.12
streamlit >= 1.32
pydantic >= 2.5
click >= 8.1
ollama >= 0.1.7
python-dotenv >= 1.0
pytest >= 8.0
pytest-asyncio >= 0.23
ruff >= 0.4 (dev)
mypy >= 1.10 (dev)
```

---

## 12. Sequence diagrams

### 12.1 Happy path: important capex notification

```
poller             worker              gemma            gemini-rr
  |                   |                   |                  |
  | poll BSE/NSE      |                   |                  |
  | dedup, junk-check |                   |                  |
  | INSERT row        |                   |                  |
  | status=classify_pending                |                  |
  |                   |                   |                  |
  |                   | claim newest      |                  |
  |                   | classify          |                  |
  |                   |------------------>| {category, conf} |
  |                   |<------------------|                  |
  |                   | det priority      |                  |
  |                   | llm override      |                  |
  |                   |------------------>| {priority, ...}  |
  |                   |<------------------|                  |
  |                   | status=attachment_pending             |
  |                   | dl pdf, extract   |                  |
  |                   | status=summarize_pending              |
  |                   | summarize         |                  |
  |                   |------------------>| {SummaryResult}  |
  |                   |<------------------|                  |
  |                   | priority=important?                   |
  |                   | status=deep_dive_pending              |
  |                   | build prompt with sector KPIs +       |
  |                   | full fundamentals                     |
  |                   |---------------------->| {JSON+prose}  |
  |                   |<----------------------|               |
  |                   | status=done                            |
  |                   |                                        |
```

### 12.2 Failure with retry

```
poller          worker        gemma
  |               |             |
  |               | summarize   |
  |               |------------>| (timeout)
  |               | <----X------|
  |               | status=summarize_failed
  |               | retry_count=1
  |               | last_error="ConnectionError"
  |               |
  |               | (next loop)
  |               | dispatcher: retry candidate?
  |               | elapsed >= 2min? yes
  |               | status=summarize_pending
  |               | retry...
```

---

## 13. Reference index

- `docs/CONTEXT.md` — quick re-entry context
- `docs/STATUS.md` — current state of the build
- `docs/QUESTIONS.md` — deferred decisions, open issues
- `docs/VERIFICATION.md` — per-phase verification protocol
- `docs/HANDOVER.md` — session-handoff template
- `docs/design-decisions.md` — every architectural choice with rationale
- `docs/architecture.md` — module diagrams + data flow + ABCs
- `docs/sessions/S<N>_*.md` — per-session activity log

External references:
- `G:\brain\docs\memory\notifications\priority_rubric.md` — source of priority rules
- `G:\brain\docs\memory\notifications\context.md` — historical context
- `D:\gemma-retrieval\docs\GEMMA4_MOE_REFERENCE.md` — Gemma tuning + perf
- `G:\valuechain\docs\learnings\gemini-aliases-usage.md` §16 — gemini-rr usage

---

## 14. Decisions made *while writing this plan* (extending design-decisions.md)

| ID | Decision | Why |
|---|---|---|
| D-07 | DB engine: **SQLite + WAL** for now; Postgres-migration via repository pattern. | User said "you decide, but expand for multi-user later." SQLite WAL handles single-writer + multi-reader well for the MVP; repo pattern makes the migration mechanical. |
| D-08 | Python **3.10+** (matches user's `C:\Users\user\AppData\Local\Programs\Python\Python310`). | Brain uses 3.10; consistent toolchain. |
| D-09 | Process model: **3 processes** — poller, worker, UI. Worker is single-threaded. | A4 default + F4 ("complete current → important next"). Single-threaded worker gives deterministic order without locking complexity. |
| D-10 | UI port **8501**, health endpoint port **8502**. | Streamlit defaults; no conflicts with brain (5000). |
| D-11 | LLM-priority override semantics: Gemma can both upgrade and downgrade. Reason logged in `ai_priority_reasons` JSON. UI shows both deterministic and LLM verdict. | F2 chose "Gemma fully overrides; rubric is starting hint." |
| D-12 | Cross-exchange duplicate handling: keep first-arrival, mark second as `cross_exchange_role='duplicate_dropped'`. Never sent to LLM. | C4 + C4b. |
| D-13 | Deferred-doc types tagged but NOT summarized: `annual_report`, `investor_presentation`, `large_misc`, `earnings`, `ppt`, `credit_note`. | G4 + H2 + K1 (earnings parked). |
| D-14 | Backfill `pipeline_status='imported_legacy'`; excluded from active worker pickup; available for manual re-process. | C3 + safety. |
| D-15 | Sector + KPI knowledge sources for deep-dive: `basic_industry_taxonomy.json`, `sector_metrics_kpis.md`, `concall_extraction_taxonomy.md`. | I3 user instruction. |
| D-16 | Inject **all** company fundamentals into deep-dive prompt; let Gemini decide what's relevant. Hint at asset-turn for capex. | J5. |
| D-17 | One-shot deep-dive call per notification (not per question). Output is `JSON + prose` in single response. | I4 default + I6 user constraint. |
| D-18 | gemini-rr cache: 30-day default. | I7. |
| D-19 | Quota: no cap; "keep going till failure" per I8. | User. |
| D-20 | No git commits during build. `git init` and one consolidated commit at Phase 13 close. | User instruction this turn. |

---

*End of PLAN.md. Update this file only when phasing or architecture changes meaningfully. Update `STATUS.md` continuously as we progress.*
