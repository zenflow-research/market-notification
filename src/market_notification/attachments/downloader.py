"""HTTP downloader for notification attachments.

Routes downloads through the per-source ``ExchangeFetcher.fetch_attachment``
when one is registered (so BSE/NSE-specific session warmup, retry logic, and
referer headers are reused). Falls back to a plain ``requests.get`` when no
fetcher is registered for the source -- used by the unit tests.

Files land at ``{pdf_dump_root}/{company_id}/{filename}``. MD5 dedup avoids
re-downloading: if a file with the same final byte-hash already exists in the
target directory, the download is skipped and the existing path is returned.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import unquote, urlparse

from .base import AttachmentDownloader, DownloadResult

logger = logging.getLogger(__name__)


class _BytesFetcher(Protocol):
    def fetch_attachment(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class _Notif:
    """Just the fields the downloader needs from a notification row."""

    notification_id: int
    source: str
    company_id: int
    attachment_url: Optional[str]
    attachment_name: Optional[str]


class HttpAttachmentDownloader(AttachmentDownloader):
    """Real HTTP downloader.

    Args:
        dump_root: filesystem root for per-company directories.
        fetchers: optional per-source ``ExchangeFetcher`` registry. Pass
            ``{'BSE': BseFetcher(), 'NSE': NseFetcher()}`` in production so
            the downloader uses the same warmed sessions as the poller.
        notification_loader: callable returning a row-dict from a
            notification id. The downloader pulls attachment_url, source,
            company_id from it. Defaults to a closure built around the
            standard ``SqlaNotificationRepo`` if you call ``.from_settings``.
        plain_http_get: injectable for tests. When ``fetchers`` doesn't
            cover a source, this is used instead. Defaults to a tiny
            ``requests.get`` wrapper.
        max_bytes: hard cap on download size; oversize hits are skipped
            with ``error='oversize'``. Defaults to 100MiB.
    """

    DEFAULT_MAX_BYTES = 100 * 1024 * 1024

    def __init__(
        self,
        *,
        dump_root: Path,
        fetchers: Optional[Mapping[str, _BytesFetcher]] = None,
        notification_loader: Callable[[int], Optional[Mapping[str, Any]]],
        plain_http_get: Optional[Callable[[str], bytes]] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.dump_root = Path(dump_root)
        self.fetchers = dict(fetchers) if fetchers else {}
        self._loader = notification_loader
        self._plain_http_get = plain_http_get or _default_http_get
        self.max_bytes = max_bytes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def download(self, notification_id: int) -> DownloadResult:
        row = self._loader(notification_id)
        if row is None:
            return _err(notification_id, "notification_not_found")

        notif = _Notif(
            notification_id=notification_id,
            source=str(row.get("source") or "").upper(),
            company_id=int(row.get("company_id") or 0),
            attachment_url=row.get("attachment_url"),
            attachment_name=row.get("attachment_name"),
        )

        if not notif.attachment_url:
            return DownloadResult(
                notification_id=notification_id,
                local_path=None,
                bytes_downloaded=0,
                content_type=None,
                md5=None,
                skipped_reason="no_url",
                error=None,
            )

        target_dir = self.dump_root / str(notif.company_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = self._derive_filename(notif)
        target_path = target_dir / filename

        # Pre-download dedup: if the exact filename is already present and
        # non-empty, treat it as the canonical copy and skip the network call.
        if target_path.exists() and target_path.stat().st_size > 0:
            md5 = _file_md5(target_path)
            return DownloadResult(
                notification_id=notification_id,
                local_path=target_path,
                bytes_downloaded=target_path.stat().st_size,
                content_type=_guess_ctype(target_path.suffix),
                md5=md5,
                skipped_reason="already_downloaded",
                error=None,
            )

        try:
            data = self._fetch_bytes(notif)
        except Exception as exc:  # noqa: BLE001 -- consolidated error path
            logger.warning(
                "attachment download failed nid=%d url=%s err=%s",
                notification_id, notif.attachment_url, exc,
            )
            return _err(notification_id, f"download_failed: {exc}")

        if len(data) == 0:
            return _err(notification_id, "empty_response")
        if len(data) > self.max_bytes:
            return _err(notification_id, f"oversize: {len(data)} > {self.max_bytes}")

        md5 = hashlib.md5(data).hexdigest()  # noqa: S324 -- non-crypto dedup

        # Post-download dedup: scan the company dir for any existing file
        # with the same byte-hash. If we find one, we keep the existing path
        # and skip writing a duplicate. The caller still gets the canonical
        # local_path.
        existing = _find_by_md5(target_dir, md5)
        if existing is not None and existing != target_path:
            return DownloadResult(
                notification_id=notification_id,
                local_path=existing,
                bytes_downloaded=existing.stat().st_size,
                content_type=_guess_ctype(existing.suffix),
                md5=md5,
                skipped_reason="md5_match_existing",
                error=None,
            )

        target_path.write_bytes(data)
        logger.info(
            "attachment saved nid=%d cid=%d path=%s bytes=%d md5=%s",
            notification_id, notif.company_id, target_path, len(data), md5,
        )
        return DownloadResult(
            notification_id=notification_id,
            local_path=target_path,
            bytes_downloaded=len(data),
            content_type=_guess_ctype(target_path.suffix),
            md5=md5,
            skipped_reason=None,
            error=None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _fetch_bytes(self, notif: _Notif) -> bytes:
        url = notif.attachment_url or ""
        fetcher = self.fetchers.get(notif.source)
        if fetcher is not None:
            return fetcher.fetch_attachment(url)
        return self._plain_http_get(url)

    @staticmethod
    def _derive_filename(notif: _Notif) -> str:
        # Prefer the exchange-supplied filename so brain's per-CID directories
        # remain navigable. Fall back to the URL path basename, then to a
        # stable synthesized name keyed on notification id.
        name = (notif.attachment_name or "").strip()
        if name:
            return _safe_name(name)
        try:
            path_basename = Path(unquote(urlparse(notif.attachment_url or "").path)).name
        except Exception:  # noqa: BLE001
            path_basename = ""
        if path_basename:
            return _safe_name(path_basename)
        return f"nid_{notif.notification_id}.pdf"


def _default_http_get(url: str) -> bytes:
    """Plain HTTP GET. Lazy-imports requests so unit tests can mock without it."""
    import requests  # local import keeps tests light

    resp = requests.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _err(notification_id: int, error: str) -> DownloadResult:
    return DownloadResult(
        notification_id=notification_id,
        local_path=None,
        bytes_downloaded=0,
        content_type=None,
        md5=None,
        skipped_reason=None,
        error=error,
    )


def _file_md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_by_md5(directory: Path, md5: str) -> Optional[Path]:
    if not directory.exists():
        return None
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        if entry.stat().st_size == 0:
            continue
        try:
            if _file_md5(entry) == md5:
                return entry
        except OSError:
            continue
    return None


def _safe_name(name: str) -> str:
    """Strip path traversal characters; keep filesystem-safe basename."""
    bad = '<>:"/\\|?*\0'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    return cleaned or "attachment.bin"


def _guess_ctype(suffix: str) -> str:
    s = suffix.lower().lstrip(".")
    return {
        "pdf": "application/pdf",
        "html": "text/html",
        "htm": "text/html",
        "xml": "application/xml",
        "zip": "application/zip",
    }.get(s, "application/octet-stream")


__all__ = ["HttpAttachmentDownloader"]
