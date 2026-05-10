"""BSE corporate-announcements fetcher.

Cleaned port of brain's `BSE_fetcher.py`. We keep ONLY the notification
listing API and the PDF-download path with the AttachLive/AttachHis
fallback. All non-notification methods (Bhavcopy, shareholding, mcap,
sector header, Sensex) are dropped -- they belong to brain's price/
fundamentals stack, not this system.

Conforms to `ExchangeFetcher` ABC.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime
from typing import ClassVar, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import ExchangeFetcher, RawNotification
from .normalizer import normalize_bse

logger = logging.getLogger(__name__)


# Endpoint constants
_BASE_URL_NOTIFICATIONS = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)
_BSE_HOMEPAGE = "https://www.bseindia.com"
_PAGE_SIZE = 50  # BSE API returns 50 records per page


class BSEFetcher(ExchangeFetcher):
    """BSE notifications + attachment downloader."""

    source: ClassVar[str] = "BSE"

    MAX_CALLS_BEFORE_REFRESH = 25
    DEFAULT_TIMEOUT = 10
    DEFAULT_RETRY_DELAY = 5  # seconds between download retries

    def __init__(self) -> None:
        self.session: Optional[requests.Session] = None
        self.call_count = 0
        self.refresh_session()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------
    def refresh_session(self) -> None:
        """Create a new requests.Session with retry adapter and warm cookies."""
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        s = requests.Session()
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        try:
            r = s.get(_BSE_HOMEPAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            r.raise_for_status()
            logger.info("BSE session refreshed")
        except requests.RequestException as e:
            logger.error("BSE session warm-up failed: %s", e)
            # Keep the session anyway -- some calls may still work.
        self.session = s
        self.call_count = 0

    def _maybe_refresh(self) -> None:
        if self.call_count >= self.MAX_CALLS_BEFORE_REFRESH:
            self.refresh_session()

    @staticmethod
    def _common_headers() -> dict[str, str]:
        return {
            "authority": "api.bseindia.com",
            "origin": _BSE_HOMEPAGE,
            "referer": f"{_BSE_HOMEPAGE}/",
            "sec-ch-ua": (
                '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
            ),
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                "Safari/537.36"
            ),
        }

    # ------------------------------------------------------------------
    # ABC implementation
    # ------------------------------------------------------------------
    def fetch_latest(self, n: int = 50) -> list[RawNotification]:
        """Return up to N most recent filings -- BSE has no 'latest N' endpoint,
        so we fetch today's filings and slice. They come back newest-first."""
        today = datetime.now().strftime("%Y%m%d")
        rows = self.fetch_for_date(today)
        return rows[:n]

    def fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]:
        """Page through BSE's `AnnSubCategoryGetData` for a single date (IST).

        BSE expects YYYYMMDD for both `strPrevDate` and `strToDate`. We page
        until we've collected `ROWCNT` rows.
        """
        raw_rows = self._fetch_raw_for_date(date_yyyymmdd)
        out: list[RawNotification] = []
        for r in raw_rows:
            norm = normalize_bse(r)
            if norm is not None:
                out.append(norm)
        return out

    def _fetch_raw_for_date(self, date_yyyymmdd: str) -> list[dict]:
        """Same as fetch_for_date but returns raw API dicts (used for artifacts)."""
        if self.session is None:
            self.refresh_session()
        self._maybe_refresh()

        headers = {
            "referer": f"{_BSE_HOMEPAGE}/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 "
                "Safari/537.36"
            ),
        }

        # First page: also tells us total rows.
        first = self._get_page(date_yyyymmdd, page=1, headers=headers)
        if not first:
            return []

        all_rows: list[dict] = list(first.get("Table", []))
        try:
            row_cnt = int(first["Table1"][0]["ROWCNT"])
        except (KeyError, IndexError, ValueError, TypeError):
            return all_rows

        total_pages = math.ceil(row_cnt / _PAGE_SIZE)
        for pg in range(2, total_pages + 1):
            self._maybe_refresh()
            page_data = self._get_page(date_yyyymmdd, page=pg, headers=headers)
            if not page_data:
                break
            all_rows.extend(page_data.get("Table", []))
        return all_rows

    def _get_page(
        self, date_yyyymmdd: str, page: int, headers: dict[str, str]
    ) -> Optional[dict]:
        """Fetch one page from the notifications endpoint. Returns None on error."""
        querystring = {
            "pageno": str(page),
            "strCat": "-1",
            "strPrevDate": date_yyyymmdd,
            "strScrip": "",
            "strSearch": "P",
            "strToDate": date_yyyymmdd,
            "strType": "C",
            "subcategory": "",
        }
        try:
            r = self.session.get(  # type: ignore[union-attr]
                _BASE_URL_NOTIFICATIONS,
                headers=headers,
                params=querystring,
                timeout=self.DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
            self.call_count += 1
            return r.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(
                "BSE notifications fetch failed (date=%s page=%d): %s",
                date_yyyymmdd, page, e,
            )
            return None

    def fetch_attachment(self, url: str) -> bytes:
        """Download a single PDF. Tries the given URL first; on failure falls
        back to AttachLive/AttachHis if the URL embeds a GUID."""
        # 1) try the original URL
        try:
            return self._download_bytes(url)
        except Exception as e1:
            guid = self._extract_guid_from_bse_url(url)
            if not guid:
                raise

            for candidate in (
                f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{guid}.pdf",
                f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{guid}.pdf",
            ):
                if candidate == url:
                    continue
                try:
                    return self._download_bytes(candidate)
                except Exception:  # noqa: BLE001 -- we want to try all candidates
                    continue
            raise e1

    # ------------------------------------------------------------------
    # Internal: download with anti-bot retry
    # ------------------------------------------------------------------
    def _download_bytes(
        self, url: str, timeout: int = 20, max_retries: int = 3
    ) -> bytes:
        if self.session is None:
            self.refresh_session()

        headers = self._common_headers().copy()
        headers.update({
            "referer": f"{_BSE_HOMEPAGE}/",
            "accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
        })

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            if attempt > 1 or self.call_count > self.MAX_CALLS_BEFORE_REFRESH:
                self.refresh_session()
            try:
                r = self.session.get(  # type: ignore[union-attr]
                    url, headers=headers, timeout=timeout, allow_redirects=True
                )
                self.call_count += 1
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status_code == 200 and "pdf" in ctype:
                    return r.content
                last_err = RuntimeError(
                    f"Non-PDF response. status={r.status_code} "
                    f"content-type={ctype} url={url}"
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(self.DEFAULT_RETRY_DELAY)

        raise last_err if last_err else RuntimeError(f"Failed to download {url}")

    @staticmethod
    def _extract_guid_from_bse_url(url: str) -> Optional[str]:
        """Pull GUID from one of:
          - .../AnnPdfOpen.aspx?Pname=<guid>.pdf
          - .../AttachLive/<guid>.pdf
          - .../AttachHis/<guid>.pdf
        """
        try:
            u = urlparse(url)
            if "AnnPdfOpen.aspx" in u.path:
                pname = (parse_qs(u.query).get("Pname") or [None])[0]
                if pname:
                    return pname.replace(".pdf", "").strip()
            m = re.search(
                r"/(AttachLive|AttachHis)/([0-9a-fA-F-]{36})\.pdf", u.path
            )
            if m:
                return m.group(2)
        except Exception:  # noqa: BLE001 -- best-effort parse
            return None
        return None
