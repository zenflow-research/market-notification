"""NSE corporate-announcements fetcher.

Cleaned port of brain's `NSEFetcher.py`. We keep:
  - Session warm-up (the lightweight NextApi probe).
  - Optional Playwright cookie bootstrap, used only as a fallback when the
    warmup hits a 403/no-cookie response. Playwright is *not* a hard
    dependency -- if not installed we raise a clear RuntimeError instead.
  - `fetch_latest_announcements` (latest-N) -> ABC `fetch_latest`.
  - `fetch_corporate_announcements` (date-range) -> ABC `fetch_for_date`.
  - Attachment download via the warmed session.

Dropped (lives in brain only): indices history, security archives,
historical trade data, corp-info dynamic/static, live analysis snapshots.
"""
from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import ExchangeFetcher, RawNotification
from .normalizer import normalize_nse

logger = logging.getLogger(__name__)


_BASE = "https://www.nseindia.com"

# Cookie names Akamai sets when a session is "in"
_AKAMAI_COOKIE_NAMES = frozenset({"ak_bmsc", "bm_sz", "_abck"})


class NSEFetcher(ExchangeFetcher):
    """NSE notifications + attachment downloader."""

    source: ClassVar[str] = "NSE"

    def __init__(
        self,
        max_calls: int = 25,
        timeout: float = 10.0,
        warmup_timeout: tuple[float, float] = (8.0, 30.0),
        warmup_timeout_after_bootstrap: tuple[float, float] = (5.0, 20.0),
        max_attempts: int = 4,
        playwright_headless: bool = False,
    ) -> None:
        self.max_calls = int(max_calls)
        self.timeout = float(timeout)
        self.warmup_timeout = warmup_timeout
        self.warmup_timeout_after_bootstrap = warmup_timeout_after_bootstrap
        self.max_attempts = int(max_attempts)
        self.playwright_headless = bool(playwright_headless)

        self.call_count = 0
        self.session: Optional[requests.Session] = None

        self._default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/143.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": f"{_BASE}/",
        }

        self.refresh_session()

    # ------------------------------------------------------------------
    # Session / warmup
    # ------------------------------------------------------------------
    def _mount_retries(self, s: requests.Session) -> None:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)

    def refresh_session(self) -> None:
        """Build a fresh session and warm it up via a small NextApi probe.

        If the probe returns 403 with no Akamai cookies (or times out), we
        try the Playwright bootstrap once. Playwright is optional -- if
        unavailable we raise a clear RuntimeError so callers can decide.
        """
        s = requests.Session()
        self._mount_retries(s)
        s.headers.update(self._default_headers)
        self.session = s
        self.call_count = 0

        probe_url = f"{_BASE}/api/NextApi/apiClient/GetQuoteApi"
        probe_params = {
            "functionName": "getHistoricalTradeData",
            "symbol": "SBIN",
            "series": "EQ",
            "fromDate": "01-01-2024",
            "toDate": "02-01-2024",
        }
        probe_headers = dict(self._default_headers)
        probe_headers["Referer"] = f"{_BASE}/get-quote/equity/SBIN/"

        def do_probe(t: tuple[float, float]) -> Optional[requests.Response]:
            try:
                return s.get(
                    probe_url,
                    params=probe_params,
                    headers=probe_headers,
                    timeout=t,
                    allow_redirects=True,
                )
            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
            ) as e:
                logger.warning("NSE warmup probe network error: %s", e)
                return None

        r = do_probe(self.warmup_timeout)
        need_bootstrap = (
            r is None
            or (
                r.status_code == 403
                and not r.headers.get("set-cookie")
                and not self._has_akamai_cookies(s)
            )
        )

        if need_bootstrap:
            logger.warning("NSE warmup failed; attempting Playwright bootstrap")
            self._bootstrap_cookies_with_playwright(
                f"{_BASE}/reports-indices-historical-index-data"
            )
            r2 = do_probe(self.warmup_timeout_after_bootstrap)
            if r2 is None or r2.status_code >= 400:
                status = r2.status_code if r2 is not None else "no-response"
                raise RuntimeError(
                    f"NSE warmup failed even after Playwright bootstrap: "
                    f"status={status}"
                )
        else:
            assert r is not None
            if r.status_code >= 400:
                raise RuntimeError(
                    f"NSE warmup failed: status={r.status_code}"
                )

        logger.info("NSE session warmed (cookies=%d)", len(s.cookies))

    def _maybe_refresh(self) -> None:
        if self.call_count >= self.max_calls:
            self.refresh_session()

    @staticmethod
    def _has_akamai_cookies(s: requests.Session) -> bool:
        return bool({c.name for c in s.cookies} & _AKAMAI_COOKIE_NAMES)

    def _bootstrap_cookies_with_playwright(self, target_url: str) -> None:
        """Last-resort cookie injection. No-op-raises if Playwright missing."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "Playwright not installed. NSE warmup probe failed and we "
                "cannot bootstrap cookies. Install with `pip install "
                "playwright && playwright install chromium`."
            ) from e

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.playwright_headless)
            context = browser.new_context()
            page = context.new_page()
            page.goto(target_url, timeout=30000)
            page.wait_for_timeout(2000)
            cookies = context.cookies()
            browser.close()

        if not cookies:
            raise RuntimeError("Playwright bootstrap returned 0 cookies.")
        assert self.session is not None
        for c in cookies:
            # Playwright cookies always carry name/value at runtime
            self.session.cookies.set(
                c["name"],  # type: ignore[typeddict-item]
                c["value"],  # type: ignore[typeddict-item]
                domain=c.get("domain"), path=c.get("path", "/"),
            )
        logger.info("Injected %d Playwright cookies into NSE session", len(cookies))

    # ------------------------------------------------------------------
    # Core request with retry / refresh on 401/403/429
    # ------------------------------------------------------------------
    def _request(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        max_attempts: Optional[int] = None,
    ) -> requests.Response:
        if self.session is None:
            raise RuntimeError("NSE session is None")
        self._maybe_refresh()

        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        attempts_limit = int(max_attempts or self.max_attempts)

        last_status: Optional[int] = None
        for attempt in range(1, attempts_limit + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=merged,
                    timeout=timeout or self.timeout,
                )
                last_status = resp.status_code

                if resp.status_code == 404:
                    raise RuntimeError(f"HTTP 404 for {resp.url}")

                if resp.status_code in (401, 403):
                    logger.warning(
                        "NSE %s -> refresh_session and retry", resp.status_code
                    )
                    self.refresh_session()
                    time.sleep(min(0.5 * attempt, 2.0))
                    continue

                if resp.status_code == 429:
                    backoff = min(2 ** attempt, 8)
                    logger.warning("NSE 429 -> sleep %.1fs", backoff)
                    time.sleep(backoff)
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    backoff = min(0.5 * (2 ** (attempt - 1)), 4)
                    logger.warning(
                        "NSE %s -> sleep %.1fs", resp.status_code, backoff
                    )
                    time.sleep(backoff)
                    continue

                resp.raise_for_status()
                self.call_count += 1
                return resp

            except requests.RequestException as e:
                if attempt >= attempts_limit:
                    raise RuntimeError(
                        f"NSE request failed after {attempts_limit} attempts: "
                        f"{url} params={params}: {e}"
                    ) from e
                backoff = min(0.5 * (2 ** (attempt - 1)), 4)
                logger.warning("NSE RequestException -> sleep %.1fs", backoff)
                time.sleep(backoff)

        raise RuntimeError(
            f"NSE request exhausted retries: url={url} last_status={last_status}"
        )

    def _get_json(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        referer: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        merged = dict(self._default_headers)
        if referer:
            merged["referer"] = referer
        merged.setdefault("accept", "*/*")
        merged.setdefault("accept-language", "en-US,en;q=0.9")
        merged.setdefault("sec-fetch-site", "same-origin")
        merged.setdefault("sec-fetch-mode", "cors")
        merged.setdefault("sec-fetch-dest", "empty")
        merged.setdefault("priority", "u=1, i")

        resp = self._request(
            _BASE + path, params=params, headers=merged, max_attempts=max_attempts
        )
        try:
            return resp.json()
        except ValueError as e:
            body_head = (resp.text or "")[:1000]
            raise RuntimeError(
                f"NSE JSON parse failed for {path}: {e}. body_head={body_head!r}"
            ) from e

    @staticmethod
    def _yyyymmdd_to_ddmmyyyy(s: str) -> str:
        """Convert our internal YYYYMMDD to NSE's DD-MM-YYYY format."""
        if len(s) != 8 or not s.isdigit():
            raise ValueError(f"Expected YYYYMMDD, got: {s!r}")
        return f"{s[6:8]}-{s[4:6]}-{s[0:4]}"

    # ------------------------------------------------------------------
    # ABC implementation
    # ------------------------------------------------------------------
    def fetch_latest(self, n: int = 50) -> list[RawNotification]:
        """Latest N corporate announcements across all companies."""
        raw = self._get_json(
            "/api/NextApi/apiClient",
            params={
                "functionName": "getCorporateInfo",
                "type": "null",
                "noOfRecords": str(n),
                "flag": "CAN",
            },
        )
        records = self._extract_records(raw)
        out: list[RawNotification] = []
        for rec in records:
            norm = normalize_nse(rec)
            if norm is not None:
                out.append(norm)
        return out

    def fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]:
        """All filings for a specific calendar date (IST)."""
        ddmm = self._yyyymmdd_to_ddmmyyyy(date_yyyymmdd)
        raw = self._get_json(
            "/api/corporate-announcements",
            params={"index": "equities", "from_date": ddmm, "to_date": ddmm},
            referer=f"{_BASE}/companies-listing/corporate-filings-announcements",
            max_attempts=5,
        )
        records = self._extract_records(raw)
        out: list[RawNotification] = []
        for rec in records:
            norm = normalize_nse(rec)
            if norm is not None:
                out.append(norm)
        return out

    @staticmethod
    def _extract_records(raw: Any) -> list[dict]:
        """NSE responses come as either a list, {"data": [...]}, or nested."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("data", "rows", "Records"):
                v = raw.get(key)
                if isinstance(v, list):
                    return v
        return []

    def fetch_attachment(self, url: str) -> bytes:
        """Download a single attachment using the warmed NSE session.

        NSE attachment URLs typically point to nsearchives.nseindia.com which
        accepts the same cookies. We do not synthesize URLs for NSE -- the
        API gives us the full path in `attchmntFile`/`attachment`.
        """
        if self.session is None:
            self.refresh_session()
        self._maybe_refresh()
        headers = {
            "Referer": f"{_BASE}/",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        }
        try:
            r = self.session.get(  # type: ignore[union-attr]
                url, headers=headers, timeout=30, allow_redirects=True
            )
            self.call_count += 1
            if r.status_code == 200 and r.content:
                return r.content
            raise RuntimeError(
                f"NSE attachment download failed: status={r.status_code} url={url}"
            )
        except requests.RequestException as e:
            raise RuntimeError(f"NSE attachment download error: {e}") from e
