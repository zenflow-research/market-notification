# Market Notification System — Quick Context

> **Read this first** at the start of any new session. ~5 minutes.
> When you finish, you should know: what we're building, why, what's done, what's next.
>
> **Doc reading order for full pickup:** `CONTEXT.md` → `STATUS.md` → `SPEC.md`
> (numbered requirements) → `PLAN.md` (sequencing) → `architecture.md`
> (interfaces) → `VERIFICATION.md` (exit criteria) → `QUESTIONS.md` (open
> items) → `design-decisions.md` (answer log).

---

## What this system is

A **standalone, microstructure-architected** Python application that:

1. **Polls BSE + NSE corporate notifications every ~60s** (24×7).
2. **Maps each filing to a company** using a master mapping (Screener_original CSV).
3. **Drops junk** via filter rules (regex, seeded from `G:\brain`).
4. **Classifies** every non-junk notification into one of 50 taxonomy categories using Gemma 4 MoE (local, Ollama).
5. **Scores priority** (important / medium / normal / ignored) using a deterministic 49-category rubric, then Gemma can override.
6. **Downloads + extracts text from attached PDFs** (pdfplumber, with Gemma vision fallback for scans). Tags long docs (annual reports, investor presentations, earnings) for a separate downstream pipeline — we don't summarize them here.
7. **Summarizes** every kept notification via Gemma 4 MoE with an extended JSON schema (summary, impact, key_figures, key_people, key_dates, deferred_doc_tags, external_links).
8. **Deep-dives every `important` notification** via `gemini-rr` with sector- and topic-aware prompts; injects all fundamentals so Gemini can reason about asset turnover, margin, etc.
9. **Surfaces all of the above in a Streamlit UI** with tabs: Today / Week / Month / Company / Earnings (parked) / Ignored / FilterRules / Health.

It is **independent** of `G:\brain` — brain may run alongside, but we never call brain's Python at runtime. We do read three things from brain (read-only): `screener_essential.db` (fundamentals), `notifications.db` (one-time backfill), and three taxonomy/KPI files.

---

## Where it lives

```
G:\market_notification\
├── docs\           — All planning, status, verification, context, decisions
├── src\market_notification\   — Source code
├── tests\          — Unit + integration + e2e tests
├── scripts\        — CLI entry points (run_poller, run_workers, run_ui, etc.)
├── data\           — SQLite DB
├── config\         — TOML config + filter rules JSON
├── logs\           — Rotating log files
└── verification\   — Phase verification artifacts
```

---

## How to run (once built)

```powershell
# Three processes, three terminals (or three Windows services later)
python -m market_notification.scripts.run_poller   # Process 1: always-on
python -m market_notification.scripts.run_workers  # Process 2: always-on, single-threaded
python -m market_notification.scripts.run_ui       # Process 3: UI on http://localhost:8501
```

External requirements at runtime:

- **Ollama** at `localhost:11434` with `gemma4-zenflow-moe:latest` loaded.
- **gemini-rr** binary at `C:\Users\user\bin\gemini-rr.cmd` (configured profiles).
- **Internet** (for BSE + NSE public APIs).

---

## Architecture in 30 seconds

- **3 processes** (poller, worker, UI) talking via the SQLite DB. No queues, no sockets, no message bus.
- **Worker is single-threaded.** It walks a state-machine per notification: `ingested → classify_pending → priority_pending_classify → attachment_pending → summarize_pending → deep_dive_pending → done`.
- **Microstructure**: every concern is a module with an Abstract Base Class and at least one default provider. Swap classifier from Gemma to Qwen by writing one new class.
- **Repository pattern** on the DB: only `db/repositories/*.py` writes SQL. When we migrate to Postgres, only those files change.
- **Sequential priority**: `important` notifications are next-in-queue but never preempt the current task. Important ones go through Gemma summarize → gemini-rr deep-dive.

---

## Key decisions to remember

| ID | Decision |
|---|---|
| D-01 | Independent system, no brain runtime dep |
| D-04 | Gemma 4 MoE for summarize/classify; gemini-rr for important deep-dive |
| D-07 | SQLite + WAL now; repository pattern allows Postgres migration |
| D-09 | Three-process model: poller (always-on), worker (single-thread always-on), UI (on demand) |
| D-11 | Gemma can override deterministic priority both up and down; reasons logged |
| D-13 | Annual reports, investor presentations, earnings, ppt, credit notes are **tagged but not summarized here** — separate pipeline handles them |
| D-16 | Inject all fundamentals into deep-dive prompt; let Gemini decide relevance |
| D-20 | No git commits during build; one consolidated commit at Phase 13 close |

Full list in `design-decisions.md` (sections A-N) and `PLAN.md` §14 (D-07..D-20).

---

## Where we are now

Check **`STATUS.md`**. It has the exact phase, the last completed step, and the next step.

---

## How phases work

`PLAN.md` §7 lists 14 phases. Each phase has:

- **Scope** — what's in
- **Deliverables** — files/code to produce
- **Dependencies** — which earlier phases must be done
- **Exit criteria** — concrete tests that must pass
- **Verification reference** — pointer to `VERIFICATION.md` for the exact protocol

We don't move to the next phase until all exit criteria are met. If a criterion can't be met, the issue goes to `QUESTIONS.md` and we either fix or descope.

---

## Important paths to remember

- **Read-only data sources** (never write):
  - `G:\Screener_original\screener_util\company_sector_mapping_master.csv` — company list
  - `G:\Screener_original\` — `check_pv_df` for prices
  - `G:\brain\screener_essential.db` — fundamentals
- **One-time imports** (read once at backfill / seeding):
  - `G:\brain\data\notifications.db` — historical notifications
  - `G:\brain\<filter rules table>` — junk-removal rules
  - `G:\brain\screener_util\basic_industry_taxonomy.json` — sector taxonomy
  - `D:\Annual_report_extract\docs\sector_metrics_kpis.md` — sector KPIs
  - `D:\claude-codex-gemini\docs\concall_extraction_taxonomy.md` — concall topics
  - `G:\brain\exchange_util\notification_priority.py` — port the rules
  - `G:\brain\exchange_util\notification_classifier.py` — port the regex patterns
  - `G:\brain\exchange_util\BSE\BSE_fetcher.py` and `NSE\NSEFetcher.py` — clean copies
- **Storage owned by us**:
  - `G:\market_notification\data\notifications.db` — our DB
  - `D:\Notification Dump\{cid}\{file}.pdf` — PDFs (per G1; may overlap with brain's archive — that's fine, MD5 dedup)

---

## Style and conventions

- Python 3.10+, type hints everywhere, `from __future__ import annotations`.
- ASCII only in code (Windows cp1252 console safety).
- Logging via `logger = logging.getLogger(__name__)`. No `print` outside scripts.
- All datetimes timezone-aware (`datetime.now(tz=timezone.utc)`); IST conversion only at UI layer.
- Pydantic v2 for DTOs and config.
- Click for script CLIs.
- One file per primary class. ABCs in `base.py`. Default impls in named files. Public façade in `service.py`.

---

## What to do when you hit a question

If you're an agent picking this up and something isn't clear:

1. Check `design-decisions.md` first — almost everything is answered there.
2. Check `QUESTIONS.md` for known open issues / deferrals.
3. Check `PLAN.md` for the architectural rationale.
4. If still unclear, **add to `QUESTIONS.md`** with `## Q-N` heading and ask the user. Don't guess.

---

## What you should never do

- Modify anything in `G:\brain` or `G:\Screener_original` or `D:\gemma-retrieval`. Read only.
- Commit to git during the build. We do one big commit at the very end.
- Use emojis or non-ASCII in code or logs.
- Run code outside the project directory unless verifying a read-only source.
- Add features, refactor, or extract abstractions beyond what the current phase requires.
- Skip the phase verification step. If exit criteria don't pass, fix or document — never silent-bypass.
