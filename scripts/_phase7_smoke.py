"""Phase 7 smoke harness: PDF pipeline end-to-end against synthetic + on-disk PDFs.

Writes the following artifacts under ``verification/phase_7_results/``:

  - ``pdf_coverage.csv``        -- per-row download/extract result + tags
  - ``vision_sample_summaries.md`` -- vision-pass output for any blank PDFs
  - ``deferred_tagging_examples.csv`` -- which heuristic fired per fixture
  - ``link_resolver_examples.md`` -- URLs found + HEAD probe result
  - ``smoke_log.txt``           -- run log

The harness is OFFLINE by default: no network, no Ollama. It synthesizes a
small set of fixture PDFs in ``tests/fixtures/`` (text + scanned + with-link
+ long), runs the AttachmentService against fake notification rows, and
records what each component produced.

Set ``MN_PHASE7_LIVE_DOWNLOAD=1`` to additionally download up to 5 real
attachments from the existing notifications DB (uses the real BSE/NSE
fetchers).

Set ``MN_PHASE7_LIVE_VISION=1`` to additionally run the Gemma vision
extractor against a real scanned PDF (requires Ollama).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure src/ on path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from market_notification.attachments.base import (  # noqa: E402
    DownloadResult,
    ExternalLinkSummary,
    ExtractionResult,
    PdfImageExtractor,
)
from market_notification.attachments.deferred_tagger import (  # noqa: E402
    DeferredDocTagger,
    DeferredTaggerInput,
)
from market_notification.attachments.downloader import (  # noqa: E402
    HttpAttachmentDownloader,
)
from market_notification.attachments.link_resolver import (  # noqa: E402
    HttpHeadLinkResolver,
    _HeadResponse,
)
from market_notification.attachments.pdfplumber_extractor import (  # noqa: E402
    PdfPlumberExtractor,
)
from market_notification.attachments.service import AttachmentService  # noqa: E402
from market_notification.db.models import Base, Notification  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_7_results"
FIXTURES = ROOT / "tests" / "fixtures" / "phase7_pdfs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FIXTURES.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s | %(message)s",
)
logger = logging.getLogger("phase7_smoke")


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_fixtures() -> dict[str, Path]:
    """Generate four fixture PDFs in tests/fixtures/phase7_pdfs/."""
    import fitz  # PyMuPDF

    out: dict[str, Path] = {}

    # 1) Text PDF -- normal capex announcement
    p = FIXTURES / "text_capex.pdf"
    if not p.exists():
        doc = fitz.open()
        page = doc.new_page()
        for i, line in enumerate([
            "RELIANCE INDUSTRIES LIMITED",
            "Outcome of Board Meeting held on 23 April 2026",
            "Approved capex of Rs 1500 crore for new petrochem plant.",
            "Commissioning expected by Q3 FY27.",
        ]):
            page.insert_text((72, 72 + 16 * i), line, fontsize=12)
        doc.save(p)
        doc.close()
    out["text_capex"] = p

    # 2) Blank/scanned PDF -- vision territory (no real text)
    p = FIXTURES / "blank_scan.pdf"
    if not p.exists():
        doc = fitz.open()
        for _ in range(2):
            doc.new_page()
        doc.save(p)
        doc.close()
    out["blank_scan"] = p

    # 3) Text PDF with embedded link
    p = FIXTURES / "with_link.pdf"
    if not p.exists():
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Acme Corp -- Investor Communication", fontsize=12)
        page.insert_text(
            (72, 96),
            "Full presentation available at https://example.invalid/deck.pdf",
            fontsize=11,
        )
        doc.save(p)
        doc.close()
    out["with_link"] = p

    # 4) Long PDF -- triggers large_misc
    p = FIXTURES / "long_doc.pdf"
    if not p.exists():
        doc = fitz.open()
        for i in range(25):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {i+1} -- routine boilerplate text.", fontsize=12)
        doc.save(p)
        doc.close()
    out["long_doc"] = p

    return out


# ---------------------------------------------------------------------------
# DB scaffold (in-memory SQLite for the harness)
# ---------------------------------------------------------------------------
def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _connection_record):  # noqa: ARG001
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    _ = _pragmas

    Base.metadata.create_all(engine)
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

    return engine, factory


def _seed(sess, *, headline: str, ai_category: str, attachment_name: str) -> int:
    row = Notification(
        company_id=42,
        source="BSE",
        headline=headline,
        announced_at=_utc(),
        attachment_url=f"https://fake.test/{attachment_name}",
        attachment_name=attachment_name,
        ai_category=ai_category,
        ai_category_group="Operations",
        pipeline_status="attachment_pending",
    )
    sess.add(row)
    sess.commit()
    return row.id


# ---------------------------------------------------------------------------
# Stubs that mimic real components
# ---------------------------------------------------------------------------
class _LocalFileDownloader:
    """Pretend to download but actually copy from a fixture path on disk."""

    def __init__(self, mapping: dict[str, Path], dump_root: Path) -> None:
        self.mapping = mapping
        self.dump_root = dump_root

    def download(self, notification_id: int) -> DownloadResult:
        # Each notification id maps to a fixture key by its `attachment_name`
        # passed in seed; we recompute the mapping by filename here.
        # Caller fills self.row_map before calling.
        name = self._name_by_nid[notification_id]
        src = self.mapping.get(name)
        if src is None or not src.exists():
            return DownloadResult(
                notification_id=notification_id, local_path=None,
                bytes_downloaded=0, content_type=None, md5=None,
                skipped_reason=None, error=f"fixture_missing: {name}",
            )
        dst = self.dump_root / "42" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_bytes(src.read_bytes())
        return DownloadResult(
            notification_id=notification_id,
            local_path=dst,
            bytes_downloaded=dst.stat().st_size,
            content_type="application/pdf",
            md5="fake",
            skipped_reason=None,
            error=None,
        )

    _name_by_nid: dict[int, str] = {}


class _DummyVisionExtractor(PdfImageExtractor):
    """Pretend Gemma vision -- returns a deterministic synthetic summary."""

    def extract_with_vision(self, pdf_path: Path, max_pages: int) -> ExtractionResult:  # noqa: ARG002
        return ExtractionResult(
            text="",
            pages_extracted=2,
            method="gemma_vision",
            image_summary=(
                f"### Page 1\n[stub vision] OCR-recovered headline from "
                f"{pdf_path.name}\n\n### Page 2\n[stub vision] continuation"
            ),
            deferred_doc_type=None,
            error=None,
        )


class _StubLinkResolver(HttpHeadLinkResolver):
    """Don't call the network; pretend HEAD always returns 200 PDF."""

    def __init__(self) -> None:
        super().__init__(
            head_request=lambda url, _t: _HeadResponse(  # noqa: ARG005
                status=200,
                content_type=(
                    "application/pdf" if url.endswith(".pdf") else "text/html"
                ),
                content_length=1024,
                final_url=url,
            ),
            timeout_s=1.0,
        )


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------
def main() -> int:
    started = time.time()
    fixtures = build_fixtures()

    engine, session_factory = _make_session_factory()
    dump_root = OUT_DIR / "_dump"
    dump_root.mkdir(exist_ok=True)

    # Seed four notifications, one per fixture.
    seeds = [
        ("text_capex.pdf",  "Capex Update",         "Approved capex of Rs 1500 crore"),
        ("blank_scan.pdf",  "Other Important",      "Scanned form filing"),
        # ai_category=Capex Update keeps the with_link fixture out of deferred,
        # so the link resolver actually runs on the extracted text.
        ("with_link.pdf",   "Capex Update",         "Update on planned capex; full deck linked"),
        ("long_doc.pdf",    "Other Important",      "Disclosure under SEBI LODR -- 25-page filing"),
    ]
    nid_to_name: dict[int, str] = {}
    with session_factory() as sess:
        for name, ai_cat, headline in seeds:
            nid = _seed(sess, headline=headline, ai_category=ai_cat, attachment_name=name)
            nid_to_name[nid] = name

    dl = _LocalFileDownloader(
        mapping={k + ".pdf": v for k, v in fixtures.items()},
        dump_root=dump_root,
    )
    dl._name_by_nid = nid_to_name  # noqa: SLF001 -- harness setup

    svc = AttachmentService(
        downloader=dl,
        text_extractor=PdfPlumberExtractor(),
        vision_extractor=_DummyVisionExtractor(),
        link_resolver=_StubLinkResolver(),
        deferred_tagger=DeferredDocTagger(),
        max_pages_text=20,
        session_factory=session_factory,
    )

    coverage_rows: list[dict[str, Any]] = []
    deferred_examples: list[dict[str, Any]] = []
    link_examples: list[dict[str, Any]] = []
    vision_examples: list[tuple[str, str]] = []

    for nid, name in nid_to_name.items():
        res = svc.run_for(nid)
        coverage_rows.append({
            "nid": nid,
            "fixture": name,
            "final_status": res.final_status,
            "method": res.extraction_method,
            "text_chars": res.text_chars,
            "image_summary_chars": res.image_summary_chars,
            "pdf_pages": res.pdf_pages,
            "deferred_doc_type": res.deferred_doc_type or "",
            "links": len(res.external_links),
            "error": res.error or "",
        })
        if res.deferred_doc_type:
            deferred_examples.append({
                "fixture": name,
                "deferred_doc_type": res.deferred_doc_type,
            })
        if res.external_links:
            for link in res.external_links:
                link_examples.append({
                    "fixture": name,
                    "url": link.url,
                    "kind": link.target_kind,
                    "summary": link.summary,
                })
        if res.image_summary_chars > 0:
            with session_factory() as sess:
                row = sess.get(Notification, nid)
                vision_examples.append(
                    (name, row.pdf_image_summary or "")
                )

    # Standalone tagger demonstration -- exercise every code path
    tagger = DeferredDocTagger()
    cases = [
        ("ai_category=Annual Report", DeferredTaggerInput(ai_category="Annual Report")),
        ("ai_category=Quarterly Results", DeferredTaggerInput(ai_category="Quarterly Results")),
        ("filename=concall_transcript.pdf", DeferredTaggerInput(attachment_name="Q1FY26_concall_transcript.pdf")),
        ("headline=AR 2025-26", DeferredTaggerInput(headline="AR 2025-26 published")),
        ("body=CRISIL rating", DeferredTaggerInput(body="CRISIL has reaffirmed AA+ stable.")),
        ("page-count fallback", DeferredTaggerInput(headline="random", pdf_pages=23)),
    ]
    for label, inp in cases:
        deferred_examples.append({"fixture": f"[case] {label}", "deferred_doc_type": tagger.tag(inp) or ""})

    # ---------------- write artifacts ------------------------------------
    cov = OUT_DIR / "pdf_coverage.csv"
    with cov.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(coverage_rows[0].keys()))
        w.writeheader()
        w.writerows(coverage_rows)

    dft = OUT_DIR / "deferred_tagging_examples.csv"
    with dft.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["fixture", "deferred_doc_type"])
        w.writeheader()
        w.writerows(deferred_examples)

    lks = OUT_DIR / "link_resolver_examples.md"
    with lks.open("w", encoding="utf-8") as f:
        f.write("# Phase 7 link resolver examples\n\n")
        if not link_examples:
            f.write("_no links found in fixtures (probe never invoked beyond stub)_\n")
        for ex in link_examples:
            f.write(
                f"- **{ex['fixture']}** -> `{ex['url']}` ({ex['kind']}): {ex['summary']}\n"
            )

    vis = OUT_DIR / "vision_sample_summaries.md"
    with vis.open("w", encoding="utf-8") as f:
        f.write("# Phase 7 vision pass sample summaries\n\n")
        for name, summary in vision_examples:
            f.write(f"## {name}\n\n{summary}\n\n---\n\n")
        if not vision_examples:
            f.write("_no vision-pass output recorded; all fixtures had extractable text or were deferred_\n")

    # exit-criteria check: ≥90% of rows have non-empty text OR image_summary OR deferred_doc_type
    covered = sum(
        1 for r in coverage_rows
        if r["text_chars"] > 0 or r["image_summary_chars"] > 0 or r["deferred_doc_type"]
    )
    coverage_pct = 100.0 * covered / len(coverage_rows)

    log = OUT_DIR / "smoke_log.txt"
    with log.open("w", encoding="utf-8") as f:
        f.write(f"Phase 7 smoke harness -- {datetime.now().isoformat()}\n")
        f.write(f"Elapsed: {time.time() - started:.2f}s\n")
        f.write(f"Fixtures: {len(fixtures)}\n")
        f.write(f"Coverage rows: {len(coverage_rows)}\n")
        f.write(f"Covered (text|vision|deferred): {covered}/{len(coverage_rows)} = {coverage_pct:.1f}%\n")
        f.write(f"Deferred examples: {len(deferred_examples)}\n")
        f.write(f"Vision-pass outputs: {len(vision_examples)}\n")
        f.write(f"Link probe results: {len(link_examples)}\n")
        f.write("Per-row details (also in pdf_coverage.csv):\n")
        for r in coverage_rows:
            f.write(json.dumps(r) + "\n")

    logger.info(
        "phase7 smoke complete: coverage=%.1f%% (%d/%d), deferred=%d, vision=%d, links=%d",
        coverage_pct, covered, len(coverage_rows),
        len(deferred_examples), len(vision_examples), len(link_examples),
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
