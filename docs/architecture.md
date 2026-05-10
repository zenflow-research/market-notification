# Market Notification System — Architecture

> Companion to `PLAN.md`. This file zooms into module-level design, ABCs,
> data flow, and the swappability matrix. Update when interfaces change.

---

## 1. Component map

```
+-------------------------------- Process: poller_proc ----------------------------+
|                                                                                 |
|   +--------+   +-----------+   +-----------+   +---------+   +------+   +------+
|   |Settings|-->|exchange   |-->|normalizer |-->|company  |-->|filter|-->|repo: |
|   +--------+   |.bse_fetch |   |           |   |.mapper  |   |.engine|   |notif|
|                |.nse_fetch |   |           |   +---------+   +------+   +------+
|                +-----------+   +-----------+        |
|                                                     v
|                                              +---------------+
|                                              | dedup +       |
|                                              | cross_exchange|
|                                              +---------------+
+---------------------------------------------------------------------------------+

+----------------------- Process: worker_proc (single thread) --------------------+
|                                                                                 |
|   +--------+   +-----------+                                                    |
|   |Settings|-->|pipeline.  |                                                    |
|   +--------+   |dispatcher |--+--+--+--+--+--+                                  |
|                +-----------+  |  |  |  |  |  |                                  |
|                               v  v  v  v  v  v                                  |
|         +----------+ +-------+ +-----+ +------+ +---------+ +--------+         |
|         |classifier| |priority| |attch| |summa.| |deep_dive| |sla_mon |         |
|         |.gemma    | |.det    | |.dl  | |.gemma| |.gemini  | |        |         |
|         |          | |.llm_ovr| |.ext | |      | |         | |        |         |
|         +----------+ +-------+ +-----+ +------+ +---------+ +--------+         |
|              |          |        |       |          |                          |
|              v          v        v       v          v                          |
|         +----------------------------------+                                    |
|         | repositories (single change point|                                    |
|         |  for SQLite -> Postgres later)   |                                    |
|         +----------------------------------+                                    |
|                          |                                                      |
|                          v                                                      |
|                +-------------------+         +-----------------+                |
|                | data/notifications.db <--   | Ollama:11434    |                |
|                +-------------------+         | gemma-rr.cmd    |                |
|                                              +-----------------+                |
+---------------------------------------------------------------------------------+

+--------------------------- Process: streamlit_proc :8501 -----------------------+
|                                                                                 |
|   pages/ today, week, month, company, earnings(parked), ignored,                |
|          filter_rules, health                                                   |
|       \                                                                         |
|        \--> components: notif_card, fundamentals_panel, price_chart,            |
|                          filter_sidebar, deep_dive_viewer                        |
|                |                                                                |
|                v                                                                |
|         +-----------------------------------+                                   |
|         | repositories (read-only views)    |                                   |
|         +-----------------------------------+                                   |
+---------------------------------------------------------------------------------+
```

---

## 2. Abstract base classes (ABCs) — the swappable interfaces

### 2.1 `exchange.base.ExchangeFetcher`

```python
class ExchangeFetcher(ABC):
    source: ClassVar[str]          # 'BSE' or 'NSE'

    @abstractmethod
    def fetch_latest(self, n: int = 50) -> list[RawNotification]: ...

    @abstractmethod
    def fetch_for_date(self, date: str) -> list[RawNotification]: ...

    @abstractmethod
    def fetch_attachment(self, url: str) -> bytes: ...
```

**Default impls.** `BseFetcher`, `NseFetcher` (cleaned copies of brain's).
**Swap with.** A mock fetcher for tests; a different exchange (e.g. NSE-IFSC) when needed.

### 2.2 `companies.base.CompanyProvider`

```python
class CompanyProvider(ABC):
    @abstractmethod
    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]: ...

    @abstractmethod
    def get_price_series(self, company_id: int, days: int = 90) -> Optional[PriceSeriesDTO]: ...
```

**Default impls.** `ScreenerOriginalCsvProvider`, `BrainScreenerEssentialProvider`, `CompositeCompanyProvider` (chains them).
**Swap with.** A direct screener.in API provider, or a yfinance-based one for prices.

### 2.3 `filter.base.FilterEngineBase`

```python
class FilterEngineBase(ABC):
    @abstractmethod
    def is_junk(self, raw: RawNotification) -> Optional[FilterMatch]: ...

    @abstractmethod
    def reload_rules(self) -> None: ...
```

**Default.** `RegexFilterEngine` from `config/filter_rules.json`.

### 2.4 `classifier.base.Classifier`

```python
class Classifier(ABC):
    @abstractmethod
    def classify(self, notification_id: int) -> ClassificationResult: ...
```

**Default.** `GemmaLlmClassifier` (Ollama, gemma4-zenflow-moe).
**Swap with.** A pure-regex fallback `RegexClassifier` (uses brain's RULES list); future `Qwen2_5Classifier` if Gemma is unavailable.

### 2.5 `priority.base.PriorityEngine`

Composite of two stages:

```python
class DeterministicPriority(ABC):
    @abstractmethod
    def score(self, notif: NotificationDTO, company: CompanyDTO) -> PriorityResult: ...

class LlmPriorityOverride(ABC):
    @abstractmethod
    def override(self, notif: NotificationDTO,
                 deterministic: PriorityResult) -> PriorityResult: ...
```

**Defaults.** `RubricDeterministicPriority` (port of brain's `notification_priority.py`),
`GemmaPriorityOverride` (re-prompts Gemma to upgrade/downgrade with reason).

### 2.6 `attachments.base`

```python
class AttachmentDownloader(ABC):
    @abstractmethod
    def download(self, notification_id: int) -> DownloadResult: ...

class PdfTextExtractor(ABC):
    @abstractmethod
    def extract(self, pdf_path: Path, max_pages: int) -> ExtractionResult: ...

class PdfImageExtractor(ABC):
    @abstractmethod
    def extract_with_vision(self, pdf_path: Path,
                            max_pages: int) -> ExtractionResult: ...

class LinkResolver(ABC):
    @abstractmethod
    def resolve(self, pdf_text: str) -> list[ExternalLinkSummary]: ...
```

**Defaults.** `HttpAttachmentDownloader`, `PdfPlumberExtractor`, `GemmaVisionExtractor`,
`HttpHeadLinkResolver`.

### 2.7 `summarizer.base.Summarizer`

```python
class Summarizer(ABC):
    @abstractmethod
    def summarize(self, notification_id: int) -> SummaryResult: ...
```

**Default.** `GemmaSummarizer`.
**Swap with.** `Qwen25Summarizer` (lighter), `GeminiRrSummarizer` (more accurate, paid).

### 2.8 `deep_dive.base.DeepDive`

```python
class DeepDive(ABC):
    @abstractmethod
    def deep_dive(self, notification_id: int) -> DeepDiveResult: ...

class PromptBuilder(ABC):
    @abstractmethod
    def build(self, notification_id: int, category: str) -> str: ...
```

**Default.** `GeminiRrDeepDive`, `CategoryAwarePromptBuilder`.

### 2.9 Repository ABCs

```python
class NotificationRepo(ABC):
    @abstractmethod
    def insert(self, notif_dict: dict) -> int: ...

    @abstractmethod
    def claim_next_for_classify(self) -> Optional[NotificationDTO]: ...

    @abstractmethod
    def claim_next_for_summarize(self) -> Optional[NotificationDTO]: ...

    @abstractmethod
    def claim_next_for_deep_dive(self) -> Optional[NotificationDTO]: ...

    @abstractmethod
    def update_pipeline_status(self, nid: int,
                               from_status: str,
                               to_status: str,
                               error: Optional[str] = None) -> bool: ...

    @abstractmethod
    def update_classification(self, nid: int,
                              result: ClassificationResult) -> None: ...

    # ...
```

**Default.** `SqliteNotificationRepo` (uses SQLAlchemy + WAL).
**Swap with.** `PostgresNotificationRepo` later (same methods, different SQLAlchemy URL + dialect tweaks).

---

## 3. Data flow — read paths

### 3.1 UI page reads

```
streamlit page (e.g. today.py)
    -> NotificationRepo.list_today(filters: ListFilters)
       -> SELECT * FROM notifications WHERE
              announced_at::date = today_ist
              AND ai_priority != 'ignored'  (unless show_ignored=True)
              AND pipeline_status NOT IN ('imported_legacy')
              [+ user filters: priority, category, source, search]
              ORDER BY ai_priority_score DESC, announced_at DESC
              LIMIT 200
    -> map rows to NotificationViewModel (DTO)
    -> NotificationCard component renders
```

### 3.2 Worker dispatcher reads

```
dispatcher.run_one_step()
    -> NotificationRepo.claim_next_for_<step>()
       -> single transaction:
            SELECT id FROM notifications
            WHERE pipeline_status = '<step>_pending'
              [AND ai_priority IN ('important')]   -- when picking deep_dive
            ORDER BY ai_priority_score DESC, announced_at DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED;   -- (Postgres later; SQLite uses WAL)
            UPDATE notifications SET pipeline_status='<step>_active' WHERE id=?;
    -> work the step
    -> repo.update_pipeline_status(...)
```

`FOR UPDATE SKIP LOCKED` doesn't exist in SQLite, but our worker is single-threaded, so a plain SELECT + UPDATE in one BEGIN IMMEDIATE transaction suffices. The Postgres migration upgrades this naturally.

---

## 4. Configuration

`config/default.toml` example:

```toml
[db]
url = "sqlite:///data/notifications.db"
wal = true

[ollama]
url = "http://localhost:11434"
gemma_model = "gemma4-zenflow-moe:latest"
qwen_fallback_model = "qwen2.5:14b"

[gemini_rr]
binary = "C:/Users/user/bin/gemini-rr.cmd"
default_timeout_s = 600

[paths]
pdf_dump_root = "D:/Notification Dump"
log_dir = "G:/market_notification/logs"
brain_screener_essential_db = "G:/brain/screener_essential.db"
screener_original_root = "G:/Screener_original"

[poller]
interval_s = 60
window_24x7 = true

[pipeline]
sla_classify_minutes = 5
pdf_max_pages_default = 20

[backfill]
brain_history = true
screener_original_history = true

[ui]
port = 8501

[health]
port = 8502
```

`config/local.toml` (gitignored) overrides any of the above.
`MN_*` env vars override both.

---

## 5. Logging

Single root logger configured in `ops/logging.py`:

- Format: `%(asctime)s %(levelname)s %(name)s %(message)s`
- Handlers: rotating file (10MB × 10) + stdout
- Per-module loggers via `logger = logging.getLogger(__name__)`
- Worker step boundaries log at INFO with `notif_id=...` `step=...` `duration_ms=...`
- LLM calls log prompt-hash + response-length + latency at INFO; full prompt at DEBUG only.

---

## 6. Testing strategy

### 6.1 Unit tests (`tests/unit/`)

- One file per module.
- Mock all external services (Ollama, gemini-rr, BSE/NSE APIs, brain DB).
- Pytest fixtures for: empty DB, populated DB, sample raw notifications, sample DTOs.

### 6.2 Integration tests (`tests/integration/`)

- Live Ollama for `_live` tests (skipped if Ollama down).
- Live BSE/NSE for `_smoke` tests (skipped if no internet).
- Recorded fixtures (`tests/fixtures/raw_notifications/*.json`) for offline runs.

### 6.3 End-to-end (`tests/e2e/`)

- `test_full_pipeline.py` — inject 1 fixture notification, walk all states, assert `done`.

---

## 7. Swappability matrix

| Module | Default | Easy swap | Medium swap | Hard swap |
|---|---|---|---|---|
| Classifier | Gemma LLM | Regex (no LLM) | Qwen LLM | Multi-step ensemble |
| Summarizer | Gemma | Qwen | Gemini-rr | Cloud Claude |
| DeepDive | Gemini-rr | Gemma deep-mode | Claude API | Multi-LLM ensemble |
| PriorityDet | Brain rubric port | Simplified ruleset | ML model | Ensemble |
| PriorityOverride | Gemma | None (bypass) | Claude | Brain's broker route |
| PdfText | pdfplumber | PyMuPDF | Unstructured | Cloud OCR |
| PdfImage | Gemma vision | qwen2.5-vl | OpenAI Vision | Cloud OCR |
| CompanyProvider | Composite (CSV + brain DB) | CSV-only | Screener.in API | DuckDB on parquet |
| Repository | SQLite | (none) | DuckDB | Postgres |

"Easy" = same ABC, drop-in. "Hard" = ABC needs extension first.

---

## 8. Naming conventions

- Module names: `snake_case_singular` (e.g. `classifier`, not `classifiers`).
- Class names: `PascalCase`. ABC suffix is `Base` only when needed for collision (e.g. `FilterEngineBase` because `FilterEngine` is the public façade).
- DTO suffix: `DTO` (e.g. `NotificationDTO`). Result classes: `Result` (e.g. `ClassificationResult`).
- File names match the primary class they export.

---

## 9. Failure modes and the journal

Every state transition writes to `pipeline_journal`:

```
notif_id | from        | to                | at         | actor    | dur_ms | err_kind | err_msg
   1234  | ingested    | classify_pending  | 16:02:01   | poller   |    35  |          |
   1234  | classify_pending | classify_active | 16:02:30   | worker   |     2  |          |
   1234  | classify_active  | summarize_pending| 16:02:45  | gemma    | 14820 |          |
   1234  | summarize_pending| summarize_active | 16:02:50  | worker   |     1  |          |
   1234  | summarize_active | summarize_failed | 16:03:55  | gemma    | 65000 | timeout  | "ConnectionError"
   1234  | summarize_failed | summarize_pending | 16:05:55 | dispatcher|    1 |          |  (retry)
   1234  | summarize_pending| summarize_active | 16:05:56  | worker   |     1 |          |
   1234  | summarize_active | done              | 16:06:30  | gemma    | 33800 |          |
```

Forensic queries become trivial:

- "Which notifications spent >10 min in classify_pending?" — SQL on journal.
- "What's the average latency for the deep-dive step?" — aggregate.

---

## 10. UI component contract

Each Streamlit page imports only from `repositories/` and `components/`. Pages never query the DB directly.

```
ui/pages/today.py
    └── ui.repositories.NotificationViewRepo.list_today(filters)
    └── ui.components.NotificationCard(notification_view_model)
```

The `NotificationViewRepo` returns view models tailored for the UI (pre-formatted dates, joined company name, joined fundamentals snapshot). It's a *separate* repo from the worker's `NotificationRepo` — different concerns, different methods, both backed by the same DB.

---

*End of architecture.md.*
