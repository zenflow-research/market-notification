"""Embedded-link resolver (FR-ATTACH-005).

Finds URLs in the PDF text and probes each one with a HEAD request to
classify the target. The summarizer (Phase 8) decides whether to follow up
based on ``target_kind``; this module deliberately stops at classification
to keep blast radius small.

Design choices:
  * HEAD-only (no body fetch): Phase 7 only labels what's pointed to.
  * Per-host rate cap + a hard total cap: a single PDF in the wild has
    been seen with 60+ outbound links. We probe at most ``max_links``.
  * SSRF-safe URL filtering: drop ``file://``, ``javascript:`` and
    private-RFC1918 destinations *before* any network call. Internal
    addresses are pointless for corporate filings and risky to probe.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Callable, Optional
from urllib.parse import urlparse

from .base import ExternalLinkSummary, LinkResolver

logger = logging.getLogger(__name__)


# Catches http/https URLs in the body of a PDF. The trailing punctuation
# class is excluded so common sentence enders don't bleed into the URL.
URL_REGEX = re.compile(
    r"https?://[^\s)<>\]\"'`]+[^\s)<>\]\"'`.,;:!?]",
    re.IGNORECASE,
)

DEFAULT_MAX_LINKS = 8
DEFAULT_TIMEOUT_S = 10


class HttpHeadLinkResolver(LinkResolver):
    """Default LinkResolver: regex extract + HEAD probe."""

    def __init__(
        self,
        *,
        head_request: Optional[Callable[[str, float], "_HeadResponse"]] = None,
        max_links: int = DEFAULT_MAX_LINKS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._head = head_request or _default_head
        self.max_links = max_links
        self.timeout_s = timeout_s

    def resolve(self, pdf_text: str) -> list[ExternalLinkSummary]:
        if not pdf_text:
            return []

        urls = _dedup_preserve_order(URL_REGEX.findall(pdf_text))
        results: list[ExternalLinkSummary] = []

        for url in urls[: self.max_links]:
            if not _is_safe_external_url(url):
                results.append(
                    ExternalLinkSummary(
                        url=url,
                        target_kind="unknown",
                        summary="dropped: unsafe target",
                        error="unsafe_url",
                    )
                )
                continue

            try:
                head = self._head(url, self.timeout_s)
            except Exception as exc:  # noqa: BLE001
                logger.info("link probe failed url=%s err=%s", url, exc)
                results.append(
                    ExternalLinkSummary(
                        url=url,
                        target_kind="unknown",
                        summary="",
                        error=f"probe_failed: {exc}",
                    )
                )
                continue

            kind = _infer_kind(head)
            summary = (
                f"HEAD {head.status} {head.content_type or 'unknown'}"
                + (f" {head.content_length}B" if head.content_length else "")
            )
            results.append(
                ExternalLinkSummary(
                    url=url,
                    target_kind=kind,
                    summary=summary,
                    error=None,
                )
            )

        return results


# ---------------------------------------------------------------------------
# HEAD response shape and default impl
# ---------------------------------------------------------------------------
class _HeadResponse:
    __slots__ = ("status", "content_type", "content_length", "final_url")

    def __init__(
        self,
        status: int,
        content_type: Optional[str],
        content_length: Optional[int],
        final_url: str,
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.content_length = content_length
        self.final_url = final_url


def _default_head(url: str, timeout_s: float) -> _HeadResponse:
    """HEAD probe; falls back to a tiny GET if the server rejects HEAD."""
    import requests  # local import; tests mock this out via injection

    try:
        r = requests.head(url, timeout=timeout_s, allow_redirects=True)
        if r.status_code in (405, 501):
            r = requests.get(
                url, timeout=timeout_s, allow_redirects=True, stream=True
            )
            r.close()
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc

    cl_raw = r.headers.get("Content-Length")
    cl: Optional[int]
    try:
        cl = int(cl_raw) if cl_raw is not None else None
    except (TypeError, ValueError):
        cl = None
    return _HeadResponse(
        status=r.status_code,
        content_type=(r.headers.get("Content-Type") or "").split(";")[0].strip() or None,
        content_length=cl,
        final_url=str(r.url),
    )


def _infer_kind(head: _HeadResponse) -> str:
    ct = (head.content_type or "").lower()
    if "pdf" in ct:
        return "pdf"
    # Office MIME types must be checked BEFORE the html/xml fallback because
    # ``application/vnd.openxmlformats-officedocument.presentationml....``
    # contains the substring "xml" and would otherwise mis-classify as html.
    if any(k in ct for k in (
        "msword",
        "officedocument",
        "powerpoint",
        "presentation",
        "spreadsheet",
        "excel",
    )):
        return "other_doc"
    if any(k in ct for k in ("html", "xhtml", "xml")):
        return "html"
    return "unknown"


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        s = s.rstrip(".,;:!?)\"'")
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _is_safe_external_url(url: str) -> bool:
    """Drop unsafe schemes + internal IPs *before* any network call."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # Try to detect raw private IPs (RFC1918 / loopback / link-local).
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        # hostname (not IP) -- best-effort lookup; if it resolves to private
        # space, drop it. We swallow DNS failures (we'll let HEAD fail later).
        try:
            for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
                if fam in (socket.AF_INET, socket.AF_INET6):
                    addr = sockaddr[0]
                    try:
                        if ipaddress.ip_address(addr).is_private:
                            return False
                    except ValueError:
                        continue
        except socket.gaierror:
            return True  # let the HEAD attempt surface the failure
    return True


__all__ = [
    "HttpHeadLinkResolver",
    "URL_REGEX",
    "DEFAULT_MAX_LINKS",
    "DEFAULT_TIMEOUT_S",
]
