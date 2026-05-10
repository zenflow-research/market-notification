"""Phase 1 verification — CSV identity provider, composite chain, fallback semantics.

Live CSV-load tests are gated by `live_internet=False` since the CSV is
local-disk; they're run as integration tests when the file exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from market_notification.companies.base import FundamentalsDTO
from market_notification.companies.composite import CompositeCompanyProvider
from market_notification.companies.csv_source import CsvCompanyProvider


SAMPLE_CSV = """tags,geography,sector_original,Company Name,mcap,URL,BSE Code,NSE Code,DataCompanyID,DataWarehouseID,ISIN,Macro,Sector,Industry,BasicIndustry,CompanyFullName,Short_id,Concall,credit_report,PPT
,,"{""isin"": ""INE144J01027"", ""macro"": ""Commodities"", ""sector"": ""Metals & Mining"", ""industry"": ""Minerals & Mining"", ""basic_industry"": ""Industrial Minerals"", ""company_fullname"": ""20 Microns Ltd"", ""short_id"": ""20MICRONS""}",20 Microns,702.56,https://www.screener.in/company/20MICRONS/consolidated/,533022.0,20MICRONS,11,6594401,INE144J01027,Commodities,Metals & Mining,Minerals & Mining,metal_ore,20 Microns Ltd,20MICRONS,True,True,True
,,"{""isin"": ""INE466L01038"", ""macro"": ""Financial Services"", ""sector"": ""Financial Services"", ""industry"": ""Capital Markets"", ""basic_industry"": ""Stockbroking & Allied"", ""company_fullname"": ""360 ONE WAM LTD"", ""short_id"": ""360ONE""}",360 ONE,48774.27,https://www.screener.in/company/360ONE/consolidated/,542772.0,360ONE,1274695,37277222,INE466L01038,Financial Services,Financial Services,Capital Markets,Other Financial Services,360 ONE WAM LTD,360ONE,True,True,True
"""


@pytest.fixture()
def sample_csv(tmp_path: Path) -> Path:
    p = tmp_path / "company_sector_mapping_master.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    return p


@pytest.fixture()
def csv_provider(sample_csv: Path) -> CsvCompanyProvider:
    return CsvCompanyProvider(csv_path=sample_csv)


def test_csv_loads_two_companies(csv_provider: CsvCompanyProvider):
    assert csv_provider.total_count == 2


def test_lookup_by_bse_code_with_decimal(csv_provider: CsvCompanyProvider):
    """BSE codes in CSV have ".0" suffix; provider must normalize."""
    company = csv_provider.get_by_bse_code("533022")
    assert company is not None
    assert company.company_id == 11
    assert company.company_name == "20 Microns"
    assert company.bse_code == "533022"


def test_lookup_by_bse_code_with_dot_zero_input(csv_provider: CsvCompanyProvider):
    company = csv_provider.get_by_bse_code("533022.0")
    assert company is not None
    assert company.company_id == 11


def test_lookup_by_nse_symbol(csv_provider: CsvCompanyProvider):
    company = csv_provider.get_by_nse_symbol("20MICRONS")
    assert company is not None
    assert company.company_id == 11
    assert company.nse_code == "20MICRONS"
    assert company.sector == "Metals & Mining"


def test_lookup_by_nse_symbol_case_insensitive(csv_provider: CsvCompanyProvider):
    company = csv_provider.get_by_nse_symbol("360one")
    assert company is not None
    assert company.company_id == 1274695


def test_lookup_by_isin(csv_provider: CsvCompanyProvider):
    company = csv_provider.get_by_isin("INE144J01027")
    assert company is not None
    assert company.company_id == 11


def test_lookup_by_company_id(csv_provider: CsvCompanyProvider):
    company = csv_provider.get_by_company_id(1274695)
    assert company is not None
    assert company.company_name == "360 ONE"
    assert company.mcap_crores == pytest.approx(48774.27)
    assert company.macro == "Financial Services"


def test_lookup_miss_returns_none(csv_provider: CsvCompanyProvider):
    assert csv_provider.get_by_bse_code("999999") is None
    assert csv_provider.get_by_nse_symbol("DOESNOTEXIST") is None
    assert csv_provider.get_by_isin("INE000000000") is None
    assert csv_provider.get_by_company_id(99999999) is None


def test_fundamentals_returns_static_mcap(csv_provider: CsvCompanyProvider):
    f = csv_provider.get_fundamentals(11)
    assert f is not None
    assert isinstance(f, FundamentalsDTO)
    assert f.mcap_crores == pytest.approx(702.56)
    # Other fields are None (sales/EBITDA/etc. not in CSV)
    assert f.quarterly_sales is None
    assert f.pat is None


def test_fundamentals_unknown_returns_none(csv_provider: CsvCompanyProvider):
    assert csv_provider.get_fundamentals(99999999) is None


def test_csv_does_not_provide_prices(csv_provider: CsvCompanyProvider):
    """CSV provider deliberately returns None; prices come from Screener_original."""
    assert csv_provider.get_price_series(11) is None


def test_composite_chains_lookups(csv_provider: CsvCompanyProvider):
    """Composite asks providers in order; first non-None wins."""

    class _NullProvider(CsvCompanyProvider.__bases__[0]):  # type: ignore[misc]
        def get_by_bse_code(self, code):  # noqa: ARG002
            return None

        def get_by_nse_symbol(self, symbol):  # noqa: ARG002
            return None

        def get_by_isin(self, isin):  # noqa: ARG002
            return None

        def get_by_company_id(self, company_id):  # noqa: ARG002
            return None

        def get_fundamentals(self, company_id):  # noqa: ARG002
            return None

        def get_price_series(self, company_id, days=90):  # noqa: ARG002
            return None

    composite = CompositeCompanyProvider([_NullProvider(), csv_provider])
    company = composite.get_by_bse_code("533022")
    assert company is not None
    assert company.company_id == 11


def test_composite_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        CompositeCompanyProvider([])


def test_composite_returns_none_when_all_miss(csv_provider: CsvCompanyProvider):
    composite = CompositeCompanyProvider([csv_provider])
    assert composite.get_by_bse_code("999999") is None
