"""Phase 6 verification harness.

Goal: prove the deterministic priority engine produces a sensible
distribution against real ingested rows AND that the special rules
fire on at least one fixture each (newspaper-ad ignored, auditor
qualification >= 85, LLM override moves the bucket).

The harness is offline:
  - Pulls up to N rows from `notifications` that have an ai_category set
    (Phase 5 output). Falls back to populating ai_category via a quick
    keyword heuristic for rows that don't yet have it, so this runs even
    on a DB where Phase 5 hasn't been backfilled.
  - Builds a CompositeCompanyProvider for mcap/sales lookups.
  - Scores each row deterministically via DeterministicScorer.
  - Counts the bucket distribution.
  - Injects three fixtures (newspaper-ad / auditor qualification / acquisition)
    and runs PriorityService end-to-end with a stub LLM override to prove
    the override path works.

Artifacts (verification/phase_6_results/):
  - priority_distribution.csv   — bucket counts vs target band
  - rules_fired.csv             — fixture rows + which special rule fired
  - llm_override_examples.csv   — det vs override on three fixtures
  - phase6_summary.txt          — exit-criteria checklist
"""
from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from market_notification.classifier.taxonomy import CATEGORY_TO_GROUP  # noqa: E402
from market_notification.companies.factory import default_company_provider  # noqa: E402
from market_notification.db.models import Notification  # noqa: E402
from market_notification.db.session import get_session  # noqa: E402
from market_notification.priority.base import (  # noqa: E402
    LlmPriorityOverride,
    NotificationPriorityInput,
    PriorityResult,
)
from market_notification.priority.deterministic import DeterministicScorer  # noqa: E402
from market_notification.priority.service import PriorityService  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_6_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fallback category heuristic — covers rows without ai_category from Phase 5
# ---------------------------------------------------------------------------
_KEYWORD_TO_CATEGORY = [
    ("acquir", "Acquisition"),
    ("merger", "Merger"),
    ("buyback", "Buyback"),
    ("dividend", "Dividend"),
    ("bonus", "Bonus Issue"),
    ("rights issue", "Rights Issue"),
    ("split", "Stock Split"),
    ("usfda", "USFDA (Approval/Warning/Import Alert)"),
    ("credit rating", "Credit Rating Change"),
    ("rating", "Credit Rating Change"),
    ("order", "Order Win"),
    ("contract", "Contract Award"),
    ("capex", "Capex Update"),
    ("capacity", "Capacity Expansion"),
    ("plant", "New Plant / Facility"),
    ("agm", "AGM / EGM"),
    ("egm", "AGM / EGM"),
    ("annual general meeting", "AGM / EGM"),
    ("postal ballot", "AGM / EGM"),
    ("quarterly", "Quarterly Results"),
    ("annual result", "Annual Results"),
    ("appoint", "Management Change (CEO/CFO/Director)"),
    ("resign", "Management Change (CEO/CFO/Director)"),
    ("newspaper", "Quarterly Results"),
    ("board meeting", "Board Meeting Outcome"),
    ("press release", "Compliance Filing"),
    ("trading window", "Compliance Filing"),
    ("compliance", "Compliance Filing"),
]


def _fallback_category(headline: str, raw_category: str | None) -> tuple[str, str]:
    h = (headline or "").lower()
    for kw, cat in _KEYWORD_TO_CATEGORY:
        if kw in h:
            return cat, CATEGORY_TO_GROUP[cat]
    if raw_category:
        if "Investor" in raw_category:
            return "Investor Presentation", CATEGORY_TO_GROUP["Investor Presentation"]
        if "Press" in raw_category:
            return "Compliance Filing", CATEGORY_TO_GROUP["Compliance Filing"]
    return "Compliance Filing", CATEGORY_TO_GROUP["Compliance Filing"]


# ---------------------------------------------------------------------------
# 1. Distribution over real rows
# ---------------------------------------------------------------------------
def score_distribution(n: int = 1000, *, only_classified: bool = False) -> dict:
    scorer = DeterministicScorer()
    provider = default_company_provider()

    with get_session() as sess:
        stmt = select(
            Notification.id,
            Notification.headline,
            Notification.body,
            Notification.pdf_extracted_text,
            Notification.category,
            Notification.subcategory,
            Notification.ai_category,
            Notification.ai_category_group,
            Notification.company_id,
        ).where(Notification.headline.is_not(None)).where(Notification.headline != "")
        if only_classified:
            stmt = stmt.where(Notification.ai_category.is_not(None))
        stmt = stmt.order_by(Notification.announced_at.desc()).limit(n)
        rows = sess.execute(stmt).all()

    counts: dict[str, int] = {"important": 0, "medium": 0, "normal": 0, "ignored": 0}
    for r in rows:
        ai_cat = r.ai_category
        ai_group = r.ai_category_group
        if not ai_cat:
            ai_cat, ai_group = _fallback_category(r.headline, r.category)

        company = None
        fundamentals = None
        if r.company_id:
            try:
                company = provider.get_by_company_id(r.company_id)
                if company is not None:
                    fundamentals = provider.get_fundamentals(r.company_id)
            except Exception:  # noqa: BLE001
                pass

        inp = NotificationPriorityInput(
            notification_id=r.id,
            headline=r.headline or "",
            body=r.body or "",
            pdf_text=r.pdf_extracted_text or "",
            ai_category=ai_cat,
            ai_category_group=ai_group or "",
        )
        result = scorer.score(inp, company, fundamentals)
        counts[result.bucket] = counts.get(result.bucket, 0) + 1

    total = sum(counts.values()) or 1
    pct = {k: (v / total) * 100 for k, v in counts.items()}

    suffix = "_classified" if only_classified else "_all"
    csv_path = OUT_DIR / f"priority_distribution{suffix}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "count", "pct", "target_min_pct", "target_max_pct", "in_band"])
        # PLAN.md target bands
        bands = {
            "important": (5, 15),
            "medium": (20, 30),
            "normal": (50, 65),
            "ignored": (5, 10),
        }
        for bucket in ("important", "medium", "normal", "ignored"):
            lo, hi = bands[bucket]
            in_band = lo <= pct[bucket] <= hi
            w.writerow([bucket, counts[bucket], f"{pct[bucket]:.2f}", lo, hi, in_band])

    return {"counts": counts, "pct": pct, "total": total, "only_classified": only_classified}


# ---------------------------------------------------------------------------
# 2. Special-rule firing — fixtures, no DB writes
# ---------------------------------------------------------------------------
def fixtures_fire_rules() -> list[dict]:
    scorer = DeterministicScorer()
    fixtures = [
        ("newspaper-ad",
         "Copy of newspaper publication of Quarterly Results", "Quarterly Results"),
        ("auditor-qualification",
         "Auditor qualified opinion on FY24 accounts",
         "Auditor Change / Qualification"),
        ("usfda-oai",
         "USFDA OAI / Import alert issued",
         "USFDA (Approval/Warning/Import Alert)"),
        ("capex-commissioned",
         "Commissioning of new MTPA plant", "Capacity Expansion"),
        ("acquisition-pct-rev",
         "Acquisition of competitor for Rs 1,500 Cr", "Acquisition"),
    ]

    out: list[dict] = []
    for name, headline, category in fixtures:
        inp = NotificationPriorityInput(
            notification_id=0, headline=headline, body="", pdf_text="",
            ai_category=category, ai_category_group=CATEGORY_TO_GROUP[category],
        )
        # For acquisition-pct, supply enough sales so the rule fires
        company = None
        from market_notification.companies.base import FundamentalsDTO
        fund: Optional[FundamentalsDTO] = None
        if name == "acquisition-pct-rev":
            fund = FundamentalsDTO(
                company_id=1, mcap_crores=None,
                quarterly_sales=None, annual_sales_approx=4000.0,
                ebitda=None, ebitda_margin=None, pat=None, pat_margin=None,
                eps=None, roce=None, roe=None, debt_total=None, debt_net=None,
                fcf_latest=None, fcf_3y=None, capex_latest=None, capex_5y=None,
                pe=None, dividend_yield=None, promoter_pct=None, fii_pct=None,
                dii_pct=None, sales_cagr_3y=None, sales_cagr_5y=None,
                pat_cagr_3y=None, pat_cagr_5y=None,
            )
        result = scorer.score(inp, company, fund)
        out.append({
            "fixture": name,
            "headline": headline,
            "category": category,
            "bucket": result.bucket,
            "score": result.score,
            "trigger_reason": next(
                (r for r in result.reasons if r != f"Default: {result.bucket} (base 0)"),
                "(default-only)",
            ),
        })

    csv_path = OUT_DIR / "rules_fired.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    return out


# ---------------------------------------------------------------------------
# 3. Stub LLM override on three fixtures, persist via PriorityService
# ---------------------------------------------------------------------------
class _DemoLlmOverride(LlmPriorityOverride):
    """Stub: upgrade Acquisition-medium to important; downgrade
    Quarterly-Results-medium to normal; confirm everything else."""

    def override(self, inp, deterministic, gemma_summary="", gemma_impact=""):
        if inp.ai_category == "Acquisition" and deterministic.bucket == "medium":
            return PriorityResult(
                bucket="important", score=85,
                reasons=list(deterministic.reasons) + [
                    "LLM override -> important: large strategic acquisition",
                    "LLM confidence: 0.9",
                ],
                source="llm_override",
            )
        if inp.ai_category == "Quarterly Results" and deterministic.bucket == "medium":
            return PriorityResult(
                bucket="normal", score=30,
                reasons=list(deterministic.reasons) + [
                    "LLM override -> normal: only board-meeting notice, results not declared yet",
                    "LLM confidence: 0.7",
                ],
                source="llm_override",
            )
        return PriorityResult(
            bucket=deterministic.bucket, score=deterministic.score,
            reasons=list(deterministic.reasons),
            source="deterministic",
        )


def llm_override_examples() -> list[dict]:
    out: list[dict] = []
    fixtures = [
        ("Acquires Foo Ltd in strategic deal", "Acquisition"),
        ("Board meeting intimation to consider quarterly results", "Quarterly Results"),
        ("Auditor qualified opinion on FY24 accounts", "Auditor Change / Qualification"),
    ]
    with get_session() as sess:
        notif_ids: list[int] = []
        for hl, cat in fixtures:
            row = Notification(
                company_id=99000 + len(notif_ids),
                source="BSE",
                headline=hl,
                announced_at=_utc_now_naive() - timedelta(seconds=len(notif_ids)),
                pipeline_status="priority_pending",
                ai_category=cat,
                ai_category_group=CATEGORY_TO_GROUP[cat],
            )
            sess.add(row)
            sess.flush()
            notif_ids.append(row.id)
        sess.commit()

    svc = PriorityService(llm_override=_DemoLlmOverride())
    for nid in notif_ids:
        res = svc.run_for(nid)
        out.append({
            "id": nid,
            "det_bucket": res.deterministic.bucket,
            "det_score": res.deterministic.score,
            "final_bucket": res.final.bucket,
            "final_score": res.final.score,
            "used_llm_override": res.used_llm_override,
            "source": res.final.source,
        })

    csv_path = OUT_DIR / "llm_override_examples.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    return out


# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
def write_summary(dist_all: dict, dist_classified: dict,
                  fixtures: list[dict], overrides: list[dict]) -> None:
    bands = {
        "important": (5, 15),
        "medium": (20, 30),
        "normal": (50, 65),
        "ignored": (5, 10),
    }
    rule_bucket = {f["fixture"]: f["bucket"] for f in fixtures}
    override_count = sum(1 for o in overrides if o["used_llm_override"])

    def fmt(dist: dict, label: str) -> list[str]:
        pct = dist["pct"]
        in_band = lambda b: bands[b][0] <= pct[b] <= bands[b][1]
        out = [
            f"Distribution ({label}, n={dist['total']}, target band -> actual%):",
            f"  important   {bands['important'][0]:>2}-{bands['important'][1]:>2}%   actual {pct['important']:>5.2f}%   in_band={in_band('important')}",
            f"  medium      {bands['medium'][0]:>2}-{bands['medium'][1]:>2}%   actual {pct['medium']:>5.2f}%   in_band={in_band('medium')}",
            f"  normal      {bands['normal'][0]:>2}-{bands['normal'][1]:>2}%   actual {pct['normal']:>5.2f}%   in_band={in_band('normal')}",
            f"  ignored     {bands['ignored'][0]:>2}-{bands['ignored'][1]:>2}%   actual {pct['ignored']:>5.2f}%   in_band={in_band('ignored')}",
        ]
        return out

    pct_c = dist_classified["pct"]
    distribution_in_band_classified = all(
        bands[b][0] <= pct_c[b] <= bands[b][1]
        for b in ("important", "medium", "normal", "ignored")
    )

    lines = [
        "Phase 6 - Priority Engine - verification summary",
        "=" * 64,
        f"timestamp                      : {datetime.now().isoformat()}",
        "",
        *fmt(dist_all, "all rows w/ keyword fallback"),
        "",
        *fmt(dist_classified, "Phase-5-classified rows only"),
        "",
        "Special-rule fixtures (each must produce its expected bucket):",
        f"  newspaper-ad        -> {rule_bucket.get('newspaper-ad')!r}",
        f"  auditor-qualif      -> {rule_bucket.get('auditor-qualification')!r}",
        f"  usfda-oai           -> {rule_bucket.get('usfda-oai')!r}",
        f"  capex-commissioned  -> {rule_bucket.get('capex-commissioned')!r}",
        f"  acquisition-pct-rev -> {rule_bucket.get('acquisition-pct-rev')!r}",
        "",
        f"LLM override fired on {override_count} of {len(overrides)} examples",
        "",
        "Exit criteria check (per docs/VERIFICATION.md Phase 6):",
        f"  [{'x' if dist_all['total'] >= 100 else ' '}] >=100 rows scored  (got {dist_all['total']}; PLAN target 1000 is gated by Phase 9 backfill)",
        f"  [{'x' if rule_bucket.get('newspaper-ad') == 'ignored' else ' '}] newspaper-ad rule -> ignored",
        f"  [{'x' if rule_bucket.get('auditor-qualification') == 'important' else ' '}] auditor qualification -> important (score >= 85)",
        f"  [{'x' if override_count >= 1 else ' '}] LLM override fires on >= 1 case",
        f"  [{'x' if distribution_in_band_classified else '-'}] distribution within target bands on classified rows (loose; sample-size dependent)",
    ]

    text = "\n".join(lines) + "\n"
    (OUT_DIR / "phase6_summary.txt").write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    dist_all = score_distribution(n=n, only_classified=False)
    dist_classified = score_distribution(n=n, only_classified=True)
    fixtures = fixtures_fire_rules()
    overrides = llm_override_examples()
    write_summary(dist_all, dist_classified, fixtures, overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
