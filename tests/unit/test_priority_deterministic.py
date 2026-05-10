"""Unit tests: DeterministicScorer — exercises every special rule.

Each test fixes one notification + (optional) company/fundamentals and
asserts the bucket + a substring of one expected reason. Together they
hit:

  - newspaper-ad result -> ignored                     (FR-PRIORITY-003)
  - auditor qualification -> score >= 85               (PLAN exit)
  - capex commissioning -> +25 + bucket upgrade
  - capex absolute >= 5000Cr
  - capex pct of mcap >= 5%
  - order win pct of sales >= 20%
  - acquisition pct of sales >= 30%
  - OFS mcap > 500Cr -> important
  - USFDA OAI -> >= 85
  - USFDA VAI -> normal
  - SEBI ban -> >= 85
  - rating upgrade vs downgrade
  - special dividend
  - WOS merger vs third-party merger
  - QIP procedural allotment -> medium
  - tax demand pct of mcap
"""
from __future__ import annotations

from typing import Optional

from market_notification.companies.base import CompanyDTO, FundamentalsDTO
from market_notification.priority.base import NotificationPriorityInput
from market_notification.priority.deterministic import DeterministicScorer


def _company(mcap: float | None) -> CompanyDTO:
    return CompanyDTO(
        company_id=1, short_id="X", company_name="X", company_fullname="X",
        bse_code=None, nse_code=None, isin=None, sector=None,
        industry=None, basic_industry=None, macro=None,
        mcap_crores=mcap,
    )


def _fund(*, mcap: Optional[float] = None,
          quarterly_sales: Optional[float] = None,
          annual_sales: Optional[float] = None) -> FundamentalsDTO:
    return FundamentalsDTO(
        company_id=1, mcap_crores=mcap,
        quarterly_sales=quarterly_sales,
        annual_sales_approx=annual_sales,
        ebitda=None, ebitda_margin=None, pat=None, pat_margin=None,
        eps=None, roce=None, roe=None, debt_total=None, debt_net=None,
        fcf_latest=None, fcf_3y=None, capex_latest=None, capex_5y=None,
        pe=None, dividend_yield=None, promoter_pct=None, fii_pct=None,
        dii_pct=None, sales_cagr_3y=None, sales_cagr_5y=None,
        pat_cagr_3y=None, pat_cagr_5y=None,
    )


def _input(headline: str, ai_category: str, group: str = "") -> NotificationPriorityInput:
    return NotificationPriorityInput(
        notification_id=1, headline=headline, body="", pdf_text="",
        ai_category=ai_category, ai_category_group=group,
    )


SCORER = DeterministicScorer()


# ---------------------------------------------------------------------------
# Default-only paths
# ---------------------------------------------------------------------------
def test_compliance_filing_default_normal() -> None:
    res = SCORER.score(_input("LODR filing", "Compliance Filing"), None)
    assert res.bucket == "normal"
    assert res.score == 30
    assert res.source == "deterministic"


def test_acquisition_with_no_amount_stays_medium() -> None:
    res = SCORER.score(_input("Acquires Foo Ltd", "Acquisition"), None)
    assert res.bucket == "medium"


# ---------------------------------------------------------------------------
# Ignored / floor rules
# ---------------------------------------------------------------------------
def test_newspaper_ad_quarterly_results_is_ignored() -> None:
    res = SCORER.score(
        _input("Copy of newspaper publication of Quarterly Results",
               "Quarterly Results"),
        None,
    )
    assert res.bucket == "ignored"
    assert res.score == 0
    assert any("Newspaper" in r for r in res.reasons)


def test_auditor_qualification_floor_85() -> None:
    res = SCORER.score(
        _input("Auditor qualified opinion on FY24 accounts",
               "Auditor Change / Qualification"),
        None,
    )
    assert res.score >= 85
    assert res.bucket == "important"
    assert any("qualification" in r.lower() or "disclaimer" in r.lower() for r in res.reasons)


def test_usfda_oai_floor_85() -> None:
    res = SCORER.score(
        _input("USFDA OAI / import alert issued", "USFDA (Approval/Warning/Import Alert)"),
        None,
    )
    assert res.score >= 85
    assert res.bucket == "important"


def test_usfda_vai_normal() -> None:
    res = SCORER.score(
        _input("USFDA VAI letter (Voluntary Action Indicated)",
               "USFDA (Approval/Warning/Import Alert)"),
        None,
    )
    assert res.bucket == "normal"
    assert res.score == 30


def test_sebi_ban_floor_85() -> None:
    res = SCORER.score(_input("SEBI debarment order", "SEBI Order"), None)
    assert res.score >= 85
    assert res.bucket == "important"


# ---------------------------------------------------------------------------
# Capex stage + amount thresholds
# ---------------------------------------------------------------------------
def test_capex_commissioned_upgrades_bucket() -> None:
    # Base medium (50) + commissioning (+25) = 75 -> important
    res = SCORER.score(
        _input("Commissioning of new MTPA plant",
               "Capacity Expansion"),
        None,
    )
    assert res.bucket == "important"
    assert any("Commissioned" in r for r in res.reasons)


def test_capex_absolute_5000cr() -> None:
    res = SCORER.score(
        _input("Capex of Rs 5,000 Cr approved", "Capex Update"),
        None,
    )
    # 50 (medium) + 5 (board approved) + 20 (>=5000Cr abs) = 75 -> important
    assert res.bucket == "important"
    assert any("Capex" in r and "5000Cr" in r for r in res.reasons)


def test_capex_pct_of_mcap_5() -> None:
    res = SCORER.score(
        _input("Rs 200 Cr capex announced", "Capex Update"),
        _company(mcap=2000),  # 10% of mcap
    )
    assert any("% of mcap" in r for r in res.reasons)
    # 50 base + 20 (pct) + 10 (>=1000? no, 200) = 70 -> important
    assert res.score >= 70


# ---------------------------------------------------------------------------
# Order win
# ---------------------------------------------------------------------------
def test_order_win_pct_of_sales() -> None:
    res = SCORER.score(
        _input("Order received for Rs 500 Cr from XYZ", "Order Win"),
        _company(mcap=None),
        _fund(annual_sales=2000),
    )
    # 50 + 40 (pct >=20) = 90 -> important
    assert res.bucket == "important"
    assert any("annual rev" in r for r in res.reasons)


def test_order_win_significant_keyword_alone() -> None:
    res = SCORER.score(
        _input("Major order win from defence ministry", "Order Win"),
        None,
    )
    # 50 + 15 = 65 -> medium
    assert res.bucket == "medium"
    assert any("significant" in r.lower() or "material" in r.lower() or "major" in r.lower()
               for r in res.reasons)


# ---------------------------------------------------------------------------
# Other
# ---------------------------------------------------------------------------
def test_acquisition_pct_of_sales_30() -> None:
    res = SCORER.score(
        _input("Acquisition of competitor for Rs 1500 Cr", "Acquisition"),
        None,
        _fund(annual_sales=4000),  # 37.5%
    )
    # 50 + 25 = 75 -> important
    assert res.bucket == "important"


def test_ofs_large_mcap_important() -> None:
    res = SCORER.score(
        _input("OFS launch", "OFS (Offer for Sale)"),
        _company(mcap=2000),
    )
    # 50 + 30 = 80 -> important
    assert res.bucket == "important"


def test_credit_rating_upgrade() -> None:
    res = SCORER.score(
        _input("CRISIL upgrades long-term rating", "Credit Rating Change"),
        None,
    )
    # 30 + 40 = 70 -> important
    assert res.bucket == "important"


def test_special_dividend_medium() -> None:
    res = SCORER.score(
        _input("Special interim dividend declared", "Dividend"),
        None,
    )
    # 30 + 20 = 50 -> medium
    assert res.bucket == "medium"


def test_wos_merger_stays_medium() -> None:
    # Merger with WOS phrasing -> NO third-party bonus -> base 50 medium
    res = SCORER.score(
        _input("Merger of wholly-owned subsidiary into the company", "Merger"),
        None,
    )
    assert res.bucket == "medium"


def test_third_party_merger_upgraded() -> None:
    res = SCORER.score(
        _input("Merger with peer pharma company", "Merger"),
        None,
    )
    # 50 + 20 = 70 -> important
    assert res.bucket == "important"


def test_qip_procedural_allotment_medium() -> None:
    # Equity dilution default = important; procedural allotment overrides to medium
    res = SCORER.score(
        _input("Allotment of equity shares pursuant to QIP",
               "Equity Dilution (QIP/FPO/Preferential)"),
        None,
    )
    assert res.bucket == "medium"
    assert res.score == 50


def test_tax_demand_pct_of_mcap() -> None:
    res = SCORER.score(
        _input("Income tax demand of Rs 50 Cr", "Tax / GST Order"),
        _company(mcap=1000),  # 5% of mcap
    )
    # 30 + 40 = 70 -> important
    assert res.bucket == "important"


def test_demerger_internal_transfer_normal() -> None:
    # Demerger default = important, but internal transfer downgrades to normal
    res = SCORER.score(
        _input("Transfer of 100% wholly-owned subsidiary to parent",
               "Demerger / Spinoff"),
        None,
    )
    assert res.bucket == "normal"


def test_buyback_procedural_closure_normal() -> None:
    res = SCORER.score(
        _input("Post-buyback public announcement and extinguishment", "Buyback"),
        None,
    )
    assert res.bucket == "normal"


# ---------------------------------------------------------------------------
# Rounding / bounds
# ---------------------------------------------------------------------------
def test_score_clamped_to_0_100() -> None:
    # Stack as many bonuses as we can to verify the clamp at 100.
    res = SCORER.score(
        _input(
            "Major commissioned capex of Rs 6000 Cr approved by board",
            "Capacity Expansion",
        ),
        _company(mcap=10000),
        _fund(annual_sales=10000),
    )
    assert 0 <= res.score <= 100
    assert res.bucket == "important"
