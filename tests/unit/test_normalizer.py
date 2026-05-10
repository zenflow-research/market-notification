"""Unit tests for `market_notification.exchange.normalizer`.

Pure-function tests -- no network, no DB. Covers:
  - Date format coverage for both BSE (4 formats) and NSE (2 formats).
  - Both NSE schema variants (getCorporateInfo + corporate-announcements).
  - BSE attachment URL synthesis.
  - Coercion helpers.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from market_notification.exchange.normalizer import (
    BSE_ATTACH_BASE_LIVE,
    BSE_DATE_FMTS,
    NSE_DATE_FMTS,
    normalize_bse,
    normalize_nse,
    parse_dt,
    safe_bool,
    safe_int,
    safe_str,
)


# ---------------------------------------------------------------------------
# Coercions
# ---------------------------------------------------------------------------

class TestSafeStr:
    def test_none(self):
        assert safe_str(None) is None

    @pytest.mark.parametrize("v", ["", " ", "nan", "None", "-", "NaN"])
    def test_placeholders_become_none(self, v):
        assert safe_str(v) is None

    def test_strips(self):
        assert safe_str("  hello  ") == "hello"

    def test_int_to_str(self):
        assert safe_str(42) == "42"


class TestSafeInt:
    def test_int(self):
        assert safe_int(7) == 7

    def test_str_int(self):
        assert safe_int("12") == 12

    def test_garbage_returns_default(self):
        assert safe_int("nope", default=-1) == -1
        assert safe_int(None) == 0


class TestSafeBool:
    @pytest.mark.parametrize("v", [True, 1, "1", "true", "TRUE", "yes", "Y"])
    def test_truthy(self, v):
        assert safe_bool(v) is True

    @pytest.mark.parametrize("v", [None, False, 0, "0", "no", "false", ""])
    def test_falsy(self, v):
        assert safe_bool(v) is False


# ---------------------------------------------------------------------------
# parse_dt -- date format coverage
# ---------------------------------------------------------------------------

class TestParseDtNSE:
    def test_dd_mon_yyyy(self):
        result = parse_dt("12-Feb-2026 23:39:23", NSE_DATE_FMTS)
        assert result == datetime(2026, 2, 12, 23, 39, 23)

    def test_iso_no_tz(self):
        result = parse_dt("2026-02-12 23:39:23", NSE_DATE_FMTS)
        assert result == datetime(2026, 2, 12, 23, 39, 23)

    def test_unknown_returns_none(self):
        assert parse_dt("12/Feb/2026", NSE_DATE_FMTS) is None

    def test_empty(self):
        assert parse_dt("", NSE_DATE_FMTS) is None
        assert parse_dt(None, NSE_DATE_FMTS) is None


class TestParseDtBSE:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-02-18T13:04:18.127", datetime(2026, 2, 18, 13, 4, 18, 127000)),
            ("2026-02-18T10:30:00", datetime(2026, 2, 18, 10, 30, 0)),
            ("18/02/2026 10:30:00", datetime(2026, 2, 18, 10, 30, 0)),
            ("18-02-2026 10:30:00", datetime(2026, 2, 18, 10, 30, 0)),
        ],
    )
    def test_all_formats(self, raw, expected):
        assert parse_dt(raw, BSE_DATE_FMTS) == expected


# ---------------------------------------------------------------------------
# normalize_nse
# ---------------------------------------------------------------------------

class TestNormalizeNSEVariantA:
    """getCorporateInfo schema (latest-N endpoint)."""

    def test_full_record(self):
        rec = {
            "symbol": "RELIANCE",
            "subject": "Press Release",
            "details": "Reliance announces buyback",
            "companyName": "Reliance Industries Ltd",
            "attachment": "https://nsearchives.nseindia.com/corporate/RIL_press.pdf",
            "actualDate": "12-Feb-2026 14:30:00",
            "seqId": "12345",
            "fileSize": "245 KB",
        }
        n = normalize_nse(rec)
        assert n is not None
        assert n.source == "NSE"
        assert n.symbol == "RELIANCE"
        assert n.headline == "Reliance announces buyback"
        assert n.category == "Press Release"
        assert n.company_name_raw == "Reliance Industries Ltd"
        assert n.announced_at == datetime(2026, 2, 12, 14, 30, 0)
        assert n.attachment_url is not None
        assert n.attachment_url.endswith("RIL_press.pdf")
        assert n.attachment_size == "245 KB"
        assert n.seq_id == "12345"
        assert n.is_critical is False
        assert n.has_xbrl is False

    def test_missing_symbol_returns_none(self):
        assert normalize_nse({"subject": "x", "details": "y"}) is None

    def test_missing_headline_returns_none(self):
        assert normalize_nse({"subject": "x", "symbol": "RELIANCE"}) is None

    def test_unparseable_date_returns_none(self):
        rec = {
            "symbol": "RELIANCE",
            "subject": "x",
            "details": "y",
            "actualDate": "garbage",
        }
        assert normalize_nse(rec) is None

    def test_broadcast_date_fallback(self):
        rec = {
            "symbol": "RELIANCE",
            "subject": "x",
            "details": "y",
            "actualDate": None,
            "broadcastDate": "12-Feb-2026 14:30:00",
        }
        n = normalize_nse(rec)
        assert n is not None
        assert n.announced_at == datetime(2026, 2, 12, 14, 30, 0)


class TestNormalizeNSEVariantB:
    """corporate-announcements schema (date-range endpoint)."""

    def test_full_record(self):
        rec = {
            "symbol": "TCS",
            "desc": "Allotment",
            "attchmntText": "TCS allotment of equity shares",
            "sm_name": "Tata Consultancy Services",
            "attchmntFile": "https://nsearchives.nseindia.com/x.pdf",
            "sort_date": "2026-02-12 14:30:00",
            "seq_id": "999",
            "sm_isin": "INE467B01029",
            "smIndustry": "IT - Software",
            "hasXbrl": "true",
            "exchdisstime": "12-Feb-2026 14:31:00",
        }
        n = normalize_nse(rec)
        assert n is not None
        assert n.symbol == "TCS"
        assert n.isin == "INE467B01029"
        assert n.industry_raw == "IT - Software"
        assert n.has_xbrl is True
        assert n.announced_at == datetime(2026, 2, 12, 14, 30, 0)
        assert n.exchange_disseminated_at == datetime(2026, 2, 12, 14, 31, 0)

    def test_an_dt_fallback(self):
        rec = {
            "symbol": "TCS",
            "desc": "x",
            "attchmntText": "y",
            "sort_date": None,
            "an_dt": "12-Feb-2026 09:00:00",
        }
        n = normalize_nse(rec)
        assert n is not None
        assert n.announced_at == datetime(2026, 2, 12, 9, 0, 0)


# ---------------------------------------------------------------------------
# normalize_bse
# ---------------------------------------------------------------------------

class TestNormalizeBSE:
    def test_full_record(self):
        rec = {
            "SCRIP_CD": "500325",
            "NEWSSUB": "Outcome of Board Meeting",
            "CATEGORYNAME": "Board Meeting",
            "SUBCATNAME": "Outcome",
            "MORE": "The Board approved...",
            "ATTACHMENTNAME": "abc-def-1234.pdf",
            "SLONGNAME": "RELIANCE INDUSTRIES LTD.",
            "DT_TM": "2026-02-18T13:04:18.127",
            "CRITICALNEWS": 1,
        }
        n = normalize_bse(rec)
        assert n is not None
        assert n.source == "BSE"
        assert n.symbol == "500325"
        assert n.headline == "Outcome of Board Meeting"
        assert n.category == "Board Meeting"
        assert n.subcategory == "Outcome"
        assert n.body is not None
        assert n.body.startswith("The Board")
        assert n.company_name_raw == "RELIANCE INDUSTRIES LTD."
        assert n.is_critical is True
        assert n.attachment_name == "abc-def-1234.pdf"
        assert n.attachment_url == f"{BSE_ATTACH_BASE_LIVE}/abc-def-1234.pdf"
        assert n.announced_at.year == 2026 and n.announced_at.month == 2

    def test_no_attachment(self):
        rec = {
            "SCRIP_CD": "500325",
            "NEWSSUB": "Update",
            "DT_TM": "2026-02-18T10:30:00",
        }
        n = normalize_bse(rec)
        assert n is not None
        assert n.attachment_url is None
        assert n.attachment_name is None
        assert n.is_critical is False

    def test_missing_scrip_returns_none(self):
        assert normalize_bse({"NEWSSUB": "x", "DT_TM": "2026-02-18T10:30:00"}) is None

    def test_missing_headline_returns_none(self):
        assert normalize_bse({"SCRIP_CD": "500325", "DT_TM": "2026-02-18T10:30:00"}) is None

    def test_unparseable_date_returns_none(self):
        rec = {"SCRIP_CD": "500325", "NEWSSUB": "x", "DT_TM": "yesterday"}
        assert normalize_bse(rec) is None

    def test_raw_json_preserved(self):
        rec = {
            "SCRIP_CD": "500325",
            "NEWSSUB": "x",
            "DT_TM": "2026-02-18T10:30:00",
            "EXTRA_FIELD": [1, 2, 3],
        }
        n = normalize_bse(rec)
        assert n is not None
        assert "EXTRA_FIELD" in n.raw_json
