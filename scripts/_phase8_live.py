"""Phase 8 LIVE smoke — calls real Ollama with `gemma4-zenflow-moe:latest`.

Verifies:
  * FR-SUMM-001 — every fixture gets a Gemma summary
  * FR-SUMM-002 — envelope passes the schema validator (no fallback)
  * FR-SUMM-003 — figures appear verbatim in `key_figures` values
  * FR-SUMM-004 — deferred-doc fixture lands a `deferred_doc_tags` entry
  * FR-SUMM-007 — captures p50/p95 wall-clock latency

Runs against a fresh in-memory DB so it does NOT touch the prod
notifications. Prints a summary table and writes:
  verification/phase_8_results/live_summary.md
  verification/phase_8_results/live_per_row_envelope.jsonl

This script intentionally exercises the inner ``GemmaLlmSummarizer``
(no queue retry) so an Ollama outage surfaces immediately rather than
sleeping for 90s. The queue-retry path is covered by the offline smoke
``_phase8_smoke.py``.

Usage
-----
    python scripts/_phase8_live.py                      # 4 default fixtures
    MN_PHASE8_LIVE_BUDGET=2 python scripts/_phase8_live.py   # cap to 2 calls

The model is shared with `D:\\gemma-retrieval`, so each call may queue
behind a concurrent gemma-retrieval reduce. Per-row latency is logged
so the contention is visible.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from market_notification.db.models import Base, Notification  # noqa: E402
from market_notification.summarizer.gemma_summarizer import (  # noqa: E402
    GemmaLlmSummarizer,
    OllamaUnavailableError,
)
from market_notification.summarizer.schema import is_fatal, validate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("phase8_live")

ART_DIR = ROOT / "verification" / "phase_8_results"
ART_DIR.mkdir(parents=True, exist_ok=True)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures — chosen to exercise FR-SUMM-003 figure preservation
# ---------------------------------------------------------------------------
FIXTURES: list[dict[str, Any]] = [
    {
        "key": "acquisition_with_figures",
        "headline": "Acquisition of XYZ Pvt Ltd for INR 102.75 Cr",
        "body": (
            "The Board of Directors at its meeting held today approved "
            "the acquisition of 100% equity stake in XYZ Pvt Ltd for a "
            "total consideration of INR 102.75 Cr. The transaction is "
            "expected to close by 30-Jun-2026. Mr. Rajiv Mehta, CEO, "
            "stated that the acquisition strengthens the company's "
            "presence in the Hosur cluster."
        ),
        "pdf_text": (
            "Total consideration: INR 102.75 Cr (one hundred two crore "
            "seventy five lakh). Stake: 100%. Expected closing: "
            "30-Jun-2026. Acquiree FY25 revenue: INR 48.20 Cr. EBITDA "
            "margin: 18.5%. Synergies expected: INR 12 Cr over 24 months."
        ),
        "deferred_doc_type": None,
        "ai_category": "Acquisition",
        "ai_priority": "important",
        "ai_priority_score": 85,
        # Figures we expect to see preserved exactly in key_figures values.
        "expected_verbatim": ["102.75", "100", "48.20", "18.5", "12"],
    },
    {
        "key": "capex_commissioning",
        "headline": "Commissioning of 10 MW solar plant; capex INR 250.5 Cr",
        "body": (
            "The Company has commissioned a 10 MW solar power plant at "
            "its Hosur facility on 05-May-2026. Total capex was INR "
            "250.5 Cr funded through internal accruals. Annual energy "
            "generation expected: ~16.5 GWh."
        ),
        "pdf_text": (
            "Capacity: 10 MW. Capex: INR 250.5 Cr. Commissioning date: "
            "05-May-2026. Annual generation (expected): 16.5 GWh. "
            "Funding: 100% internal accruals."
        ),
        "deferred_doc_type": None,
        "ai_category": "Capex Update",
        "ai_priority": "important",
        "ai_priority_score": 80,
        "expected_verbatim": ["10", "250.5", "16.5"],
    },
    {
        "key": "dividend",
        "headline": "Final dividend of INR 7.50 per share recommended",
        "body": (
            "The Board has recommended a final dividend of INR 7.50 "
            "per equity share of face value INR 10 each for FY26. "
            "Record date: 12-Aug-2026. Payment after AGM."
        ),
        "pdf_text": None,
        "deferred_doc_type": None,
        "ai_category": "Dividend",
        "ai_priority": "medium",
        "ai_priority_score": 50,
        "expected_verbatim": ["7.50", "10"],
    },
    {
        "key": "earnings_deferred",
        "headline": "Quarterly Results — Q1FY26 to be considered on 12-Aug-2026",
        "body": (
            "Pursuant to Regulation 29 of SEBI (LODR), notice is hereby "
            "given that a meeting of the Board of Directors will be held "
            "on 12-Aug-2026 inter alia to consider the Unaudited "
            "Financial Results for the quarter ended 30-Jun-2026."
        ),
        "pdf_text": None,
        "deferred_doc_type": "earnings",
        "ai_category": "Quarterly Results",
        "ai_priority": "medium",
        "ai_priority_score": 50,
        "expected_verbatim": [],  # deferred path; figures may legitimately be empty
    },
]


# ---------------------------------------------------------------------------
# DB harness
# ---------------------------------------------------------------------------
def _make_engine():
    eng = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _rec):  # noqa: ANN001, ARG001
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    _ = _pragmas

    Base.metadata.create_all(eng)
    return eng


def _factory(engine):
    Maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        sess = Maker()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    return factory


def _seed(engine, fixtures: list[dict[str, Any]]) -> dict[str, int]:
    Maker = sessionmaker(bind=engine, expire_on_commit=False)
    sess = Maker()
    ids: dict[str, int] = {}
    for fx in fixtures:
        n = Notification(
            company_id=1,
            source="BSE",
            headline=fx["headline"],
            category=fx["ai_category"],
            body=fx["body"],
            pdf_extracted_text=fx.get("pdf_text"),
            announced_at=_utc_now_naive(),
            pipeline_status="summarize_pending",
            ai_category=fx["ai_category"],
            ai_category_group="LiveSmoke",
            ai_priority=fx["ai_priority"],
            ai_priority_score=fx["ai_priority_score"],
            deferred_doc_type=fx.get("deferred_doc_type"),
        )
        sess.add(n)
        sess.commit()
        ids[fx["key"]] = n.id
    sess.close()
    return ids


# ---------------------------------------------------------------------------
# Verbatim-figure check (FR-SUMM-003)
# ---------------------------------------------------------------------------
def _check_verbatim(envelope: dict[str, Any], expected: list[str]) -> tuple[int, int, list[str]]:
    """Return (matched, total, missing) for expected verbatim figures.

    A figure is matched if it appears as a substring of *any* value in
    `key_figures`. We do substring (not equality) because "100" is a
    legitimate match against "100%" and against "INR 100 Cr".
    """
    if not expected:
        return (0, 0, [])
    values = [str(kf.get("value", "")) for kf in envelope.get("key_figures", [])]
    haystack = " | ".join(values)
    missing = [exp for exp in expected if exp not in haystack]
    matched = len(expected) - len(missing)
    return (matched, len(expected), missing)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    budget = int(os.environ.get("MN_PHASE8_LIVE_BUDGET", "0") or "0")
    chosen = FIXTURES if budget == 0 else FIXTURES[:budget]
    log.info("running %d live fixtures against gemma4-zenflow-moe:latest", len(chosen))

    engine = _make_engine()
    factory = _factory(engine)
    ids = _seed(engine, chosen)

    summarizer = GemmaLlmSummarizer(
        model="gemma4-zenflow-moe:latest",
        base_url="http://127.0.0.1:11434",
        request_timeout_s=300,
        keep_alive="24h",
        session_factory=factory,
    )

    rows: list[dict[str, Any]] = []
    envelopes_path = ART_DIR / "live_per_row_envelope.jsonl"
    envelopes_path.write_text("", encoding="utf-8")

    for fx in chosen:
        nid = ids[fx["key"]]
        log.info("--- fixture=%s id=%d", fx["key"], nid)
        t0 = time.monotonic()
        try:
            run = summarizer.summarize_with_meta(nid)
        except OllamaUnavailableError as e:
            log.error("OLLAMA UNAVAILABLE on %s: %r", fx["key"], e)
            rows.append({
                "key": fx["key"],
                "id": nid,
                "status": "ollama_unavailable",
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": repr(e),
            })
            continue
        elapsed = time.monotonic() - t0

        envelope = {
            "summary": run.summary.summary,
            "impact": run.summary.impact,
            "key_figures": [
                {"label": kf.label, "value": kf.value, "unit": kf.unit}
                for kf in run.summary.key_figures
            ],
            "key_people": [
                {"name": kp.name, "role": kp.role}
                for kp in run.summary.key_people
            ],
            "key_dates": [
                {"label": kd.label, "iso_date": kd.iso_date, "certainty": kd.certainty}
                for kd in run.summary.key_dates
            ],
            "attachments_referenced": list(run.summary.attachments_referenced),
            "deferred_doc_tags": list(run.summary.deferred_doc_tags),
            "external_links": [
                {
                    "url": el.url,
                    "referenced_as": el.referenced_as,
                    "target_summary": el.target_summary,
                }
                for el in run.summary.external_links
            ],
            "confidence": run.summary.confidence,
        }

        with envelopes_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"key": fx["key"], "id": nid, "elapsed_s": round(elapsed, 2),
                 "envelope": envelope}, ensure_ascii=False
            ) + "\n")

        # Re-validate for paranoia (already validated inside summarizer)
        _, errs = validate(envelope)
        verb_matched, verb_total, verb_missing = _check_verbatim(
            envelope, fx.get("expected_verbatim", [])
        )

        deferred_tag_present = (
            fx.get("deferred_doc_type") is None
            or len(envelope["deferred_doc_tags"]) > 0
        )

        rows.append({
            "key": fx["key"],
            "id": nid,
            "status": "ok",
            "elapsed_s": round(elapsed, 2),
            "attempts": run.attempts,
            "fallback": run.fallback,
            "used_deferred_prompt": run.used_deferred_prompt,
            "validator_fatal": is_fatal(errs),
            "validator_errors": errs,
            "verbatim_matched": f"{verb_matched}/{verb_total}",
            "verbatim_missing": verb_missing,
            "deferred_tag_present": deferred_tag_present,
            "summary_chars": len(envelope["summary"]),
            "key_figures_n": len(envelope["key_figures"]),
            "confidence": envelope["confidence"],
        })

        log.info(
            "  done in %.2fs attempts=%d fallback=%s figs=%d verbatim=%d/%d",
            elapsed, run.attempts, run.fallback,
            len(envelope["key_figures"]), verb_matched, verb_total,
        )
        log.info("  summary: %s", envelope["summary"][:200])

    # ---------------------------------------------------------------------
    # Aggregate + write report
    # ---------------------------------------------------------------------
    ok_rows = [r for r in rows if r["status"] == "ok"]
    latencies = [r["elapsed_s"] for r in ok_rows]
    if latencies:
        p50 = statistics.median(latencies)
        # statistics doesn't have a quantile in older 3.10? quantiles works.
        p95 = (
            statistics.quantiles(latencies, n=20)[-1]
            if len(latencies) >= 4 else max(latencies)
        )
    else:
        p50 = p95 = 0.0

    total_verbatim_match = sum(
        int(r["verbatim_matched"].split("/")[0]) for r in ok_rows
    )
    total_verbatim_expected = sum(
        int(r["verbatim_matched"].split("/")[1]) for r in ok_rows
    )
    fallbacks = sum(1 for r in ok_rows if r["fallback"])
    deferred_ok = all(r["deferred_tag_present"] for r in ok_rows)

    md = ["# Phase 8 — LIVE smoke results", ""]
    md.append(f"- Model: `gemma4-zenflow-moe:latest` @ http://127.0.0.1:11434")
    md.append(f"- Date: {_utc_now_naive().isoformat()}Z")
    md.append(f"- Fixtures: {len(rows)} (ok={len(ok_rows)})")
    md.append(f"- Latency p50: **{p50:.2f}s** · p95: **{p95:.2f}s** "
              f"(FR-SUMM-007 budget: <30s p95)")
    md.append(f"- FR-SUMM-002 fallbacks: **{fallbacks}** (target 0)")
    md.append(
        f"- FR-SUMM-003 verbatim coverage: **{total_verbatim_match}/{total_verbatim_expected}**"
    )
    md.append(f"- FR-SUMM-004 deferred-tag presence: **{'OK' if deferred_ok else 'FAIL'}**")
    md.append("")
    md.append("## Per-row")
    md.append("")
    md.append(
        "| key | status | latency_s | attempts | figs | verbatim | deferred_tag | "
        "summary_chars | confidence |"
    )
    md.append("|---|---|---:|---:|---:|---|---|---:|---:|")
    for r in rows:
        if r["status"] != "ok":
            md.append(
                f"| {r['key']} | {r['status']} | {r.get('elapsed_s','-')} | - | - | - | - | - | - |"
            )
            continue
        md.append(
            f"| {r['key']} | ok | {r['elapsed_s']:.2f} | {r['attempts']} | "
            f"{r['key_figures_n']} | {r['verbatim_matched']} | "
            f"{'yes' if r['deferred_tag_present'] else 'NO'} | "
            f"{r['summary_chars']} | {r['confidence']:.2f} |"
        )
    md.append("")
    if any(r.get("verbatim_missing") for r in ok_rows):
        md.append("## Missing verbatim figures (per row)")
        md.append("")
        for r in ok_rows:
            if r.get("verbatim_missing"):
                md.append(f"- **{r['key']}**: {r['verbatim_missing']}")

    out = ART_DIR / "live_summary.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    log.info("wrote %s", out)
    log.info("wrote %s", envelopes_path)

    print(
        f"\nLIVE smoke: ok={len(ok_rows)}/{len(rows)}  "
        f"p50={p50:.2f}s  p95={p95:.2f}s  "
        f"verbatim={total_verbatim_match}/{total_verbatim_expected}  "
        f"fallbacks={fallbacks}  deferred_tag_present={deferred_ok}"
    )
    print(f"  -> {out}")
    return 0 if fallbacks == 0 and len(ok_rows) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
