"""Unit tests for HttpAttachmentDownloader.

Covers:
  - no_url short-circuit
  - filename derivation (attachment_name > URL basename > nid_<id>.pdf)
  - successful fresh download
  - pre-download dedup (file already present at the same name)
  - post-download MD5 dedup (different name, same bytes already present)
  - error path (transport raises)
  - oversize cap
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from market_notification.attachments.downloader import HttpAttachmentDownloader


def _row(
    *,
    nid: int = 1,
    company_id: int = 42,
    source: str = "BSE",
    url: Optional[str] = "https://example.test/foo.pdf",
    name: Optional[str] = "foo.pdf",
) -> dict[str, Any]:
    return {
        "id": nid,
        "company_id": company_id,
        "source": source,
        "attachment_url": url,
        "attachment_name": name,
    }


def _make_dl(
    tmp_path: Path,
    *,
    row: dict[str, Any],
    bytes_back: bytes = b"%PDF-1.4 hello world",
    fail_with: Optional[Exception] = None,
    **kwargs: Any,
) -> HttpAttachmentDownloader:
    def loader(_nid: int) -> dict[str, Any]:
        return row

    def http_get(_url: str) -> bytes:
        if fail_with is not None:
            raise fail_with
        return bytes_back

    return HttpAttachmentDownloader(
        dump_root=tmp_path,
        notification_loader=loader,
        plain_http_get=http_get,
        **kwargs,
    )


def test_no_url_returns_skipped(tmp_path: Path) -> None:
    dl = _make_dl(tmp_path, row=_row(url=None))
    res = dl.download(1)
    assert res.skipped_reason == "no_url"
    assert res.local_path is None
    assert res.error is None


def test_fresh_download_writes_to_company_dir(tmp_path: Path) -> None:
    payload = b"%PDF-1.4 BSE filing"
    dl = _make_dl(tmp_path, row=_row(), bytes_back=payload)
    res = dl.download(1)
    assert res.error is None
    assert res.skipped_reason is None
    assert res.local_path is not None
    assert res.local_path.read_bytes() == payload
    assert res.local_path.parent.name == "42"  # company_id
    assert res.local_path.name == "foo.pdf"
    assert res.bytes_downloaded == len(payload)
    assert res.md5  # populated


def test_filename_falls_back_to_url_basename(tmp_path: Path) -> None:
    dl = _make_dl(
        tmp_path,
        row=_row(name=None, url="https://example.test/path/Bar%20Filing.pdf"),
    )
    res = dl.download(1)
    assert res.local_path is not None
    assert res.local_path.name == "Bar Filing.pdf"


def test_filename_falls_back_to_nid_when_no_name(tmp_path: Path) -> None:
    dl = _make_dl(tmp_path, row=_row(nid=99, name=None, url="https://x/"))
    res = dl.download(99)
    assert res.local_path is not None
    assert res.local_path.name == "nid_99.pdf"


def test_predownload_dedup_skips_network(tmp_path: Path) -> None:
    payload = b"%PDF cached"
    target = tmp_path / "42" / "foo.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    calls: list[str] = []

    def http_get(url: str) -> bytes:
        calls.append(url)
        return b"NEW BYTES"  # would override -- but we expect no call

    dl = HttpAttachmentDownloader(
        dump_root=tmp_path,
        notification_loader=lambda _nid: _row(),
        plain_http_get=http_get,
    )
    res = dl.download(1)
    assert res.skipped_reason == "already_downloaded"
    assert calls == []
    assert target.read_bytes() == payload


def test_postdownload_md5_dedup(tmp_path: Path) -> None:
    payload = b"%PDF same bytes new name"
    # Existing file with a different name but identical content.
    pre = tmp_path / "42" / "older_name.pdf"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(payload)

    dl = _make_dl(
        tmp_path,
        row=_row(name="newer_name.pdf"),
        bytes_back=payload,
    )
    res = dl.download(1)
    assert res.skipped_reason == "md5_match_existing"
    # canonical path stays the older one, no new file created
    assert res.local_path == pre
    assert not (tmp_path / "42" / "newer_name.pdf").exists()


def test_transport_failure_returns_error(tmp_path: Path) -> None:
    dl = _make_dl(tmp_path, row=_row(), fail_with=RuntimeError("connection reset"))
    res = dl.download(1)
    assert res.error is not None
    assert "connection reset" in res.error
    assert res.local_path is None


def test_oversize_payload_rejected(tmp_path: Path) -> None:
    big = b"x" * 1024
    dl = _make_dl(tmp_path, row=_row(), bytes_back=big, max_bytes=512)
    res = dl.download(1)
    assert res.error is not None
    assert "oversize" in res.error
    assert res.local_path is None


def test_empty_response_rejected(tmp_path: Path) -> None:
    dl = _make_dl(tmp_path, row=_row(), bytes_back=b"")
    res = dl.download(1)
    assert res.error == "empty_response"


def test_fetcher_registry_overrides_plain_http(tmp_path: Path) -> None:
    sentinel = b"%PDF via BSE fetcher"

    class FakeFetcher:
        def fetch_attachment(self, url: str) -> bytes:  # noqa: ARG002
            return sentinel

    dl = HttpAttachmentDownloader(
        dump_root=tmp_path,
        notification_loader=lambda _nid: _row(source="BSE"),
        fetchers={"BSE": FakeFetcher()},
        plain_http_get=lambda _u: b"PLAIN -- should not be called",
    )
    res = dl.download(1)
    assert res.local_path is not None
    assert res.local_path.read_bytes() == sentinel


def test_unsafe_filename_chars_sanitized(tmp_path: Path) -> None:
    dl = _make_dl(
        tmp_path,
        row=_row(name="evil/../../escape.pdf"),
    )
    res = dl.download(1)
    assert res.local_path is not None
    # the slashes (the actual escape vector) are scrubbed; the file lands
    # inside the company dir even though the original basename had `../`
    # path components.
    assert res.local_path.parent.name == "42"
    assert "/" not in res.local_path.name
    assert "\\" not in res.local_path.name
    # canonical resolution stays inside the dump root
    assert tmp_path in res.local_path.resolve().parents


def test_notification_not_found_returns_error(tmp_path: Path) -> None:
    dl = HttpAttachmentDownloader(
        dump_root=tmp_path,
        notification_loader=lambda _nid: None,
    )
    res = dl.download(404)
    assert res.error == "notification_not_found"


def test_does_not_double_walk_when_predownload_match(tmp_path: Path) -> None:
    """Pre-download skip path must not invoke the network at all."""
    target = tmp_path / "42" / "foo.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already there")

    calls = {"n": 0}

    def http_get(_url: str) -> bytes:
        calls["n"] += 1
        return b"shouldn't fire"

    dl = HttpAttachmentDownloader(
        dump_root=tmp_path,
        notification_loader=lambda _nid: _row(),
        plain_http_get=http_get,
    )
    dl.download(1)
    assert calls["n"] == 0


@pytest.fixture()
def dl(tmp_path: Path) -> HttpAttachmentDownloader:
    return _make_dl(tmp_path, row=_row())


def test_md5_is_stable(tmp_path: Path, dl: HttpAttachmentDownloader) -> None:
    res1 = dl.download(1)
    # second call hits the pre-download cache and recomputes md5 from disk
    res2 = dl.download(1)
    assert res1.md5 == res2.md5
