"""Phase 5 LIVE classifier verification.

Picks 20 already-ingested notifications whose `category` field is non-empty,
asks the real Gemma model to classify each, then compares the LLM output
against the expected category derived from brain's deterministic rules
(notification_classifier.RULES). Prints accuracy and writes per-row CSV.

Skip if Ollama is unreachable.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, "G:/brain")

from sqlalchemy import select  # noqa: E402

from market_notification.classifier.llm_classifier import GemmaLlmClassifier  # noqa: E402
from market_notification.classifier.taxonomy import (  # noqa: E402
    UNCATEGORIZED,
    VALID_CATEGORIES,
)
from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.db.models import Notification  # noqa: E402
from market_notification.db.session import get_session  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_5_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def expected_label(headline: str, category: str, subcategory: str, body: str) -> tuple[str, str]:
    """Use brain's deterministic rules as ground truth."""
    try:
        from exchange_util.notification_classifier import classify_notification
    except Exception:
        return ("", "")
    cat, group = classify_notification(headline, category or "", subcategory or "", body or "")
    return (cat or "", group or "")


def main(n: int = 20) -> int:
    settings = get_settings()
    classifier = GemmaLlmClassifier(
        model=settings.ollama.gemma_model,
        base_url=settings.ollama.url,
        temperature=settings.classifier.temperature,
        # Override: classifier output is a 3-key JSON object, ~50 tokens. The
        # default 1024 makes Gemma "think" past the answer; 256 is plenty.
        num_predict=256,
        request_timeout_s=settings.ollama.request_timeout_s,
        keep_alive=settings.ollama.keep_alive,
    )

    # Pull n recent rows. We don't restrict to classify_pending — we re-classify
    # whatever is fresh and flip pipeline_status forward. This script is a
    # verification tool, not an operational worker.
    with get_session() as sess:
        rows = sess.execute(
            select(Notification.id, Notification.source, Notification.headline,
                   Notification.category, Notification.subcategory, Notification.body)
            .where(Notification.headline.is_not(None))
            .where(Notification.headline != "")
            .order_by(Notification.announced_at.desc())
            .limit(n)
        ).all()

    out: list[dict] = []
    correct = 0
    n_with_label = 0
    started = time.monotonic()
    for r in rows:
        nid = r.id
        # Reset to classify_pending so the classifier runs against it.
        with get_session() as sess:
            row = sess.get(Notification, nid)
            row.pipeline_status = "classify_pending"
            sess.commit()

        result = classifier.classify(nid)
        exp_cat, exp_group = expected_label(
            r.headline or "", r.category or "", r.subcategory or "", r.body or "",
        )
        match = bool(exp_cat) and (result.category == exp_cat)
        if exp_cat:
            n_with_label += 1
            if match:
                correct += 1

        out.append({
            "id": nid,
            "source": r.source,
            "headline": (r.headline or "")[:120],
            "raw_category": r.category or "",
            "expected_category": exp_cat,
            "llm_category": result.category,
            "llm_group": result.group,
            "llm_confidence": round(result.confidence, 2),
            "match": "yes" if match else "no",
            "source_field": result.source,
        })

    elapsed = time.monotonic() - started

    csv_path = OUT_DIR / "classification_accuracy.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    accuracy = (correct / n_with_label) if n_with_label else 0.0
    summary_lines = [
        f"timestamp        : {datetime.now().isoformat()}",
        f"model            : {settings.ollama.gemma_model}",
        f"rows scored      : {len(out)}",
        f"rows w/ label    : {n_with_label}",
        f"correct          : {correct}",
        f"accuracy         : {accuracy:.1%}",
        f"elapsed (s)      : {elapsed:.1f}",
        f"per-row latency  : {(elapsed / max(1, len(out))):.1f}s",
        f"target accuracy  : >= 80%",
        f"PASS             : {'YES' if accuracy >= 0.80 else 'NO'}",
    ]
    summary = "\n".join(summary_lines)
    (OUT_DIR / "live_accuracy_summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0 if accuracy >= 0.80 else 2


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    sys.exit(main(n))
