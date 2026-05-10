# market-notification

BSE/NSE corporate notification capture, classification, summarization, deep-dive, and Streamlit UI.

**Status: Phase 0 (Bootstrap) — under construction.**

## Quick start

> Not runnable yet — Phase 0 in progress. See `docs/STATUS.md` for current state.

Once Phase 13 ships:

```powershell
# 1. Setup (one-time)
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python -m market_notification.scripts.bootstrap_db
python -m market_notification.scripts.seed_filter_rules
python -m market_notification.scripts.backfill_brain_history  # optional, ~80k rows

# 2. Run (three terminals)
python -m market_notification.scripts.run_poller   # Process 1: always-on
python -m market_notification.scripts.run_workers  # Process 2: always-on, single-threaded
python -m market_notification.scripts.run_ui       # Process 3: UI on http://localhost:8501
```

## Documentation

All planning, status, and decisions live in `docs/`:

- `docs/CONTEXT.md` — start here. 5-minute orientation.
- `docs/PLAN.md` — full master plan with phases, modules, schema.
- `docs/architecture.md` — module diagrams, ABCs, data flow.
- `docs/STATUS.md` — current build state.
- `docs/QUESTIONS.md` — open issues, deferred decisions.
- `docs/VERIFICATION.md` — per-phase verification protocol.
- `docs/HANDOVER.md` — session-handoff log.
- `docs/design-decisions.md` — every architectural decision with rationale.

## External dependencies (runtime)

- **Ollama** at `localhost:11434` with `gemma4-zenflow-moe:latest` loaded.
- **gemini-rr** at `C:\Users\user\bin\gemini-rr.cmd`.
- **Internet** for BSE/NSE public APIs.

## Read-only data sources (never written)

- `G:\Screener_original\screener_util\company_sector_mapping_master.csv`
- `G:\Screener_original\` (price/volume via `check_pv_df`)
- `G:\brain\screener_essential.db` (fundamentals)

## License

Proprietary, internal use only.
