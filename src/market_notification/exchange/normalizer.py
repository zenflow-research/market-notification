"""Pure normalization helpers: raw BSE/NSE API records -> RawNotification.

No I/O lives here. No HTTP, no DB. The fetcher modules call into these
functions; that keeps date-format quirks and field-name aliasing testable
without live network calls.

Date-format constants are copied verbatim from brain's
`notification_poller.py` (lines 55-65). Any format we observe in the wild
that isn't here MUST be added with a unit test alongside.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from .base import RawNotification

# ---------------------------------------------------------------------------
# Constants -- API-side endpoint particulars
# ---------------------------------------------------------------------------

# BSE attachments are returned as bare filenames; we synthesize the URL.
BSE_ATTACH_BASE_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive"
BSE_ATTACH_BASE_HIS = "https://www.bseindia.com/xml-data/corpfiling/AttachHis"

# Date formats observed in API responses. Order matters -- try most-common first.
NSE_DATE_FMTS: tuple[str, ...] = (
    "%d-%b-%Y %H:%M:%S",   # "12-Feb-2026 23:39:23"
    "%Y-%m-%d %H:%M:%S",   # "2026-02-12 23:39:23" (sort_date)
)

BSE_DATE_FMTS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S.%f",  # "2026-02-18T13:04:18.127" (with ms)
    "%Y-%m-%dT%H:%M:%S",     # "2026-02-18T10:30:00"
    "%d/%m/%Y %H:%M:%S",     # "18/02/2026 10:30:00"
    "%d-%m-%Y %H:%M:%S",     # "18-02-2026 10:30:00"
)


# ---------------------------------------------------------------------------
# Small typed coercions
# ---------------------------------------------------------------------------

def safe_str(val: Any) -> Optional[str]:
    """Convert to stripped str. Returns None for empty / NaN / placeholder."""
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in ("nan", "none", "-", ""):
        return None
    return s


def safe_int(val: Any, default: int = 0) -> int:
    """Convert to int; return default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def safe_bool(val: Any) -> bool:
    """Coerce truthy/falsy values (incl. "true"/"yes"/"1" strings) to bool."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("true", "1", "yes", "y", "t")


def parse_dt(raw: Any, formats: tuple[str, ...]) -> Optional[datetime]:
    """Try each format in order. Returns naive datetime or None.

    Returned datetime is naive (no tz) -- IST is the implicit clock for both
    BSE and NSE. Tz attachment happens at the storage layer per project
    convention (see CONTEXT.md "Style and conventions").
    """
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# NSE normalization
# ---------------------------------------------------------------------------

def normalize_nse(rec: dict[str, Any]) -> Optional[RawNotification]:
    """Map one NSE record to RawNotification, or None if unusable.

    Handles two schema variants:
      A) `getCorporateInfo` (latest-N endpoint) -- has `subject`, `details`,
         `actualDate`, `attachment`, `seqId`, `companyName`, `fileSize`.
      B) `corporate-announcements` (date-range endpoint) -- has `desc`,
         `attchmntText`, `sort_date`/`an_dt`, `attchmntFile`, `seq_id`,
         `sm_name`, `sm_isin`, `smIndustry`, `hasXbrl`, `exchdisstime`.
    """
    if "subject" in rec:  # variant A: getCorporateInfo
        symbol = safe_str(rec.get("symbol"))
        category = safe_str(rec.get("subject"))
        headline = safe_str(rec.get("details"))
        company_name = safe_str(rec.get("companyName"))
        attachment_url = safe_str(rec.get("attachment"))
        announced_raw = rec.get("actualDate") or rec.get("broadcastDate")
        seq_id = safe_str(rec.get("seqId"))
        file_size = safe_str(rec.get("fileSize"))
        isin = None
        industry = None
        has_xbrl = False
        exch_dissem_raw = None
    else:  # variant B: corporate-announcements
        symbol = safe_str(rec.get("symbol"))
        category = safe_str(rec.get("desc"))
        headline = safe_str(rec.get("attchmntText"))
        company_name = safe_str(rec.get("sm_name"))
        attachment_url = safe_str(rec.get("attchmntFile"))
        announced_raw = rec.get("sort_date") or rec.get("an_dt")
        seq_id = safe_str(rec.get("seq_id"))
        file_size = safe_str(rec.get("fileSize"))
        isin = safe_str(rec.get("sm_isin"))
        industry = safe_str(rec.get("smIndustry"))
        has_xbrl = safe_bool(rec.get("hasXbrl"))
        exch_dissem_raw = safe_str(rec.get("exchdisstime"))

    if not symbol or not headline:
        return None

    announced_at = parse_dt(announced_raw, NSE_DATE_FMTS)
    if announced_at is None:
        return None

    return RawNotification(
        source="NSE",
        seq_id=seq_id,
        headline=headline,
        category=category,
        subcategory=None,
        body=None,
        announced_at=announced_at,
        exchange_disseminated_at=parse_dt(exch_dissem_raw, NSE_DATE_FMTS),
        attachment_url=attachment_url,
        attachment_name=None,
        attachment_size=file_size,
        is_critical=False,
        has_xbrl=has_xbrl,
        symbol=symbol,
        company_name_raw=company_name,
        isin=isin,
        industry_raw=industry,
        raw_json=json.dumps(rec, default=str),
    )


# ---------------------------------------------------------------------------
# BSE normalization
# ---------------------------------------------------------------------------

def normalize_bse(rec: dict[str, Any]) -> Optional[RawNotification]:
    """Map one BSE `AnnSubCategoryGetData` row to RawNotification, or None.

    The BSE API returns a bare attachment filename in `ATTACHMENTNAME`; we
    synthesize the live URL here. Callers that hit 404 should retry against
    `BSE_ATTACH_BASE_HIS` (older filings get archived).
    """
    scrip_cd = safe_str(rec.get("SCRIP_CD"))
    headline = safe_str(rec.get("NEWSSUB"))
    if not scrip_cd or not headline:
        return None

    dt_tm = safe_str(rec.get("DT_TM"))
    announced_at = parse_dt(dt_tm, BSE_DATE_FMTS)
    if announced_at is None:
        return None

    attachment_name = safe_str(rec.get("ATTACHMENTNAME"))
    attachment_url = (
        f"{BSE_ATTACH_BASE_LIVE}/{attachment_name}" if attachment_name else None
    )

    return RawNotification(
        source="BSE",
        seq_id=safe_str(rec.get("NEWS_SUBMISSION_DT")) or safe_str(rec.get("NEWSID")),
        headline=headline,
        category=safe_str(rec.get("CATEGORYNAME")),
        subcategory=safe_str(rec.get("SUBCATNAME")),
        body=safe_str(rec.get("MORE")),
        announced_at=announced_at,
        exchange_disseminated_at=parse_dt(safe_str(rec.get("DissemDT")), BSE_DATE_FMTS),
        attachment_url=attachment_url,
        attachment_name=attachment_name,
        attachment_size=safe_str(rec.get("PDFFLAG")),
        is_critical=safe_int(rec.get("CRITICALNEWS")) == 1,
        has_xbrl=safe_bool(rec.get("XBRL")),
        symbol=scrip_cd,
        company_name_raw=safe_str(rec.get("SLONGNAME")),
        isin=None,
        industry_raw=None,
        raw_json=json.dumps(rec, default=str),
    )
