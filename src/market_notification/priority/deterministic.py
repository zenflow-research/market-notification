"""Deterministic priority engine.

Composes ``rubric.default_for`` (the base score) with a bank of per-category
adjusters from `thresholds.py`. Each adjuster is a small function that takes
a ``_ScoreCtx`` and may add/subtract from the score plus push a human
explanation onto ``reasons``.

This faithfully reproduces brain's `determine_priority` behavior — the same
input yields the same numeric score and the same final bucket — but split
into per-category functions so each rule is independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..companies.base import CompanyDTO, FundamentalsDTO
from . import thresholds as t
from .base import (
    DeterministicPriority,
    NotificationPriorityInput,
    PriorityResult,
)
from .rubric import bucket_for_score, default_for


# ---------------------------------------------------------------------------
# Internal scoring context — mutable across adjusters
# ---------------------------------------------------------------------------
@dataclass
class _ScoreCtx:
    headline: str
    ai_category: str
    score: int
    reasons: list[str] = field(default_factory=list)
    amount_cr: Optional[float] = None
    mcap_cr: Optional[float] = None
    annual_sales: Optional[float] = None

    def add(self, delta: int, reason: str) -> None:
        self.score += delta
        self.reasons.append(reason)

    def set_score(self, value: int, reason: str) -> None:
        self.score = value
        self.reasons.append(reason)

    def at_least(self, floor: int, reason: str) -> None:
        if self.score < floor:
            self.score = floor
            self.reasons.append(reason)


# ---------------------------------------------------------------------------
# Per-category adjusters — keep each one short and focused
# ---------------------------------------------------------------------------
def _adj_capex_stage(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {"Capacity Expansion", "Capex Update", "New Plant / Facility"}:
        return
    if t.is_commissioned(ctx.headline):
        ctx.add(25, "Stage: Commissioned/Operational (+25)")
    elif t.is_board_approved(ctx.headline):
        ctx.add(5, "Stage: Board Approved (+5)")
    elif t.is_proposed_or_exploring(ctx.headline):
        ctx.add(-10, "Stage: Proposed/Exploring (-10)")


def _adj_capex_pct_of_mcap(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {
        "Capacity Expansion", "Capex Update",
        "Debt Raise (NCD/Bond/ECB)", "Corporate Guarantee",
    }:
        return
    pct = t.pct_of(ctx.amount_cr, ctx.mcap_cr)
    if pct is not None and pct >= 5:
        assert ctx.amount_cr is not None  # for type-checkers
        ctx.add(20, f"Amount Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of mcap (>=5%)")


def _adj_capex_absolute(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {"Capacity Expansion", "Capex Update"}:
        return
    if ctx.amount_cr is None:
        return
    if ctx.amount_cr >= 5000:
        ctx.add(20, f"Capex Rs {ctx.amount_cr:.0f}Cr >= 5000Cr (large absolute capex)")
    elif ctx.amount_cr >= 1000:
        ctx.add(10, f"Capex Rs {ctx.amount_cr:.0f}Cr >= 1000Cr (significant capex)")


def _adj_order_win_pct_of_sales(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {"Order Win", "Contract Award"}:
        return
    pct = t.pct_of(ctx.amount_cr, ctx.annual_sales)
    if pct is not None and pct >= 20:
        assert ctx.amount_cr is not None
        ctx.add(40, f"Order Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of annual rev (>=20%)")


def _adj_order_win_keyword(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {"Order Win", "Contract Award"}:
        return
    if t.has_significant_keyword(ctx.headline):
        ctx.add(15, "Company flagged order as significant/material/major (+15)")


def _adj_acquisition_pct_of_sales(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Acquisition":
        return
    pct = t.pct_of(ctx.amount_cr, ctx.annual_sales)
    if pct is not None and pct >= 30:
        assert ctx.amount_cr is not None
        ctx.add(25, f"Acquisition Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of FY rev (>=30%)")


def _adj_ofs_mcap(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "OFS (Offer for Sale)":
        return
    if ctx.mcap_cr and ctx.mcap_cr > 500:
        ctx.add(30, f"OFS company mcap Rs {ctx.mcap_cr:.0f}Cr > 500Cr")


def _adj_management_change_keyword(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Management Change (CEO/CFO/Director)":
        return
    if t.mentions_ceo_cfo_md(ctx.headline):
        ctx.add(15, "CEO/MD/CFO change detected (+15, stays medium)")


def _adj_auditor_qualification(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Auditor Change / Qualification":
        return
    if t.is_auditor_qualification(ctx.headline):
        ctx.at_least(85, "Auditor qualification/disclaimer (red flag)")


def _adj_credit_rating(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Credit Rating Change":
        return
    if t.is_rating_upgrade(ctx.headline):
        ctx.add(40, "Rating UPGRADE")
    elif t.is_rating_downgrade(ctx.headline):
        ctx.add(40, "Rating DOWNGRADE (negative)")


def _adj_dividend(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Dividend":
        return
    if t.is_special_dividend(ctx.headline):
        ctx.add(20, "Special/one-time dividend")
    elif t.has_declared_amount(ctx.headline):
        ctx.add(10, "Dividend declared with amount")


def _adj_quarterly_results_special(ctx: _ScoreCtx) -> None:
    """Newspaper-ad → ignored (FR-PRIORITY-003 fixture); board-meeting notice → medium."""
    if ctx.ai_category != "Quarterly Results":
        return
    if t.is_newspaper_publication(ctx.headline):
        ctx.set_score(0, "Newspaper advertisement -> IGNORED")
    elif t.is_board_notice_for_results(ctx.headline):
        ctx.set_score(50, "Board meeting notice to consider results -> medium")
    elif t.is_clarification(ctx.headline):
        ctx.set_score(30, "Clarification on results -> normal")


def _adj_demerger(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Demerger / Spinoff":
        return
    if t.is_internal_subsidiary_transfer(ctx.headline):
        ctx.set_score(30, "Internal subsidiary transfer, not genuine demerger")


def _adj_usfda(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "USFDA (Approval/Warning/Import Alert)":
        return
    if t.is_usfda_vai(ctx.headline):
        ctx.set_score(30, "VAI (clean bill) -> normal")
    elif t.is_usfda_oai_or_warning(ctx.headline):
        ctx.at_least(85, "OAI/Warning/Import Alert (negative)")


def _adj_tax_legal_pct(ctx: _ScoreCtx) -> None:
    if ctx.ai_category not in {"Tax / GST Order", "Legal / Litigation"}:
        return
    pct = t.pct_of(ctx.amount_cr, ctx.mcap_cr)
    if pct is not None and pct >= 1:
        assert ctx.amount_cr is not None
        ctx.add(40, f"Demand Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of mcap (>=1%)")


def _adj_sebi(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "SEBI Order":
        return
    if t.is_sebi_ban(ctx.headline):
        ctx.at_least(85, "SEBI ban/debarment")
    pct = t.pct_of(ctx.amount_cr, ctx.mcap_cr)
    if pct is not None and pct >= 5:
        assert ctx.amount_cr is not None
        ctx.add(30, f"SEBI penalty Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of mcap (>=5%)")


def _adj_merger_third_party(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Merger":
        return
    if not t.is_wos_merger(ctx.headline):
        ctx.add(20, "Third-party merger (not WOS absorption)")


def _adj_buyback_procedural(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Buyback":
        return
    if t.is_buyback_procedural(ctx.headline):
        ctx.set_score(30, "Buyback procedural closure -> normal")


def _adj_qip_procedural(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Equity Dilution (QIP/FPO/Preferential)":
        return
    if t.is_qip_procedural(ctx.headline):
        ctx.set_score(50, "Procedural QIP allotment/listing -> medium")


def _adj_warrant_promoter(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Warrant Conversion":
        return
    if t.is_promoter_action(ctx.headline):
        ctx.add(20, "Promoter warrant conversion (confidence signal)")


def _adj_divestiture(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Divestiture / Sale":
        return
    pct = t.pct_of(ctx.amount_cr, ctx.mcap_cr)
    if pct is not None and pct >= 5:
        assert ctx.amount_cr is not None
        ctx.add(20, f"Sale proceeds Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of mcap (>=5%)")


def _adj_rpt(ctx: _ScoreCtx) -> None:
    if ctx.ai_category != "Related Party Transaction":
        return
    pct = t.pct_of(ctx.amount_cr, ctx.mcap_cr)
    if pct is not None and pct >= 1:
        assert ctx.amount_cr is not None
        ctx.add(25, f"RPT Rs {ctx.amount_cr:.0f}Cr = {pct:.1f}% of mcap (>=1%)")


# Order matters: tax_legal/sebi pct adjusters run after sebi-ban so a banned-
# party SEBI penalty still picks up both the floor and the additional pct.
_ADJUSTERS = (
    _adj_capex_stage,
    _adj_capex_pct_of_mcap,
    _adj_capex_absolute,
    _adj_order_win_pct_of_sales,
    _adj_order_win_keyword,
    _adj_acquisition_pct_of_sales,
    _adj_ofs_mcap,
    _adj_management_change_keyword,
    _adj_auditor_qualification,
    _adj_credit_rating,
    _adj_dividend,
    _adj_quarterly_results_special,
    _adj_demerger,
    _adj_usfda,
    _adj_tax_legal_pct,
    _adj_sebi,
    _adj_merger_third_party,
    _adj_buyback_procedural,
    _adj_qip_procedural,
    _adj_warrant_promoter,
    _adj_divestiture,
    _adj_rpt,
)


# ---------------------------------------------------------------------------
# Public engine
# ---------------------------------------------------------------------------
class DeterministicScorer(DeterministicPriority):
    """Concrete deterministic priority — pure function of inputs.

    The constructor takes no dependencies; a single instance is safe to share
    across threads. The score is composed by:
      1. Look up category default (`rubric.default_for`).
      2. Extract amount from headline (and PDF text if available).
      3. Run each adjuster in declared order.
      4. Map final score → bucket via `rubric.bucket_for_score`.
    """

    def score(
        self,
        inp: NotificationPriorityInput,
        company: CompanyDTO | None,
        fundamentals: FundamentalsDTO | None = None,
    ) -> PriorityResult:
        bucket, base = default_for(inp.ai_category)
        ctx = _ScoreCtx(
            headline=inp.headline or "",
            ai_category=inp.ai_category,
            score=base,
            reasons=[f"Default: {bucket} (base {base})"],
            amount_cr=t.extract_amount_cr(inp.headline)
                or t.extract_amount_cr(inp.body)
                or t.extract_amount_cr(inp.pdf_text),
            mcap_cr=_pick_mcap(company, fundamentals),
            annual_sales=_pick_annual_sales(fundamentals),
        )

        for fn in _ADJUSTERS:
            fn(ctx)

        final_bucket = bucket_for_score(ctx.score)
        return PriorityResult(
            bucket=final_bucket,
            score=max(0, min(100, ctx.score)),
            reasons=list(ctx.reasons),
            source="deterministic",
            extracted_amount_cr=ctx.amount_cr,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pick_mcap(company: CompanyDTO | None, fundamentals: FundamentalsDTO | None) -> Optional[float]:
    if fundamentals is not None and fundamentals.mcap_crores is not None:
        return float(fundamentals.mcap_crores)
    if company is not None and company.mcap_crores is not None:
        return float(company.mcap_crores)
    return None


def _pick_annual_sales(fundamentals: FundamentalsDTO | None) -> Optional[float]:
    if fundamentals is None:
        return None
    if fundamentals.annual_sales_approx is not None:
        return float(fundamentals.annual_sales_approx)
    if fundamentals.quarterly_sales is not None:
        return float(fundamentals.quarterly_sales) * 4
    return None


__all__ = ["DeterministicScorer"]
