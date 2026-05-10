"""Unit tests for HttpHeadLinkResolver."""
from __future__ import annotations

from typing import Optional

import pytest

from market_notification.attachments.link_resolver import (
    DEFAULT_MAX_LINKS,
    HttpHeadLinkResolver,
    URL_REGEX,
    _HeadResponse,
    _is_safe_external_url,
)


def _resp(
    status: int = 200,
    ct: Optional[str] = "application/pdf",
    cl: Optional[int] = 1234,
    final: str = "",
) -> _HeadResponse:
    return _HeadResponse(status=status, content_type=ct, content_length=cl, final_url=final or "")


def test_url_regex_extracts_clean_urls() -> None:
    text = (
        "See https://www.example.com/foo.pdf for details. "
        "Also visit (http://x.org/y) and ftp://nope/."
    )
    urls = URL_REGEX.findall(text)
    assert "https://www.example.com/foo.pdf" in urls
    # parens stripped, dotted sentence ender stripped
    assert "http://x.org/y" in urls
    # ftp scheme not matched
    assert all("ftp" not in u for u in urls)


def test_resolves_pdf_url_with_head_metadata() -> None:
    captured: list[tuple[str, float]] = []

    def head(url: str, timeout: float) -> _HeadResponse:
        captured.append((url, timeout))
        return _resp(status=200, ct="application/pdf", cl=2048)

    resolver = HttpHeadLinkResolver(head_request=head)
    out = resolver.resolve("see https://example.com/a.pdf for full text")
    assert len(out) == 1
    item = out[0]
    assert item.url == "https://example.com/a.pdf"
    assert item.target_kind == "pdf"
    assert item.error is None
    assert "200" in item.summary
    assert "application/pdf" in item.summary
    assert captured[0][0] == "https://example.com/a.pdf"


def test_classifies_html_and_office_docs() -> None:
    def head(url: str, _t: float) -> _HeadResponse:  # noqa: ARG001
        if "html" in url:
            return _resp(ct="text/html; charset=utf-8")
        if "ppt" in url:
            return _resp(
                ct="application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
        return _resp(ct="application/octet-stream")

    resolver = HttpHeadLinkResolver(head_request=head)
    text = "links: https://x.com/page.html and https://y.com/deck.ppt and https://z.com/blob"
    out = resolver.resolve(text)
    by_url = {r.url: r for r in out}
    assert by_url["https://x.com/page.html"].target_kind == "html"
    assert by_url["https://y.com/deck.ppt"].target_kind == "other_doc"
    assert by_url["https://z.com/blob"].target_kind == "unknown"


def test_failed_head_recorded_in_error_field() -> None:
    def head(_url: str, _t: float) -> _HeadResponse:  # noqa: ARG001
        raise RuntimeError("connection refused")

    resolver = HttpHeadLinkResolver(head_request=head)
    out = resolver.resolve("see https://broken.example/x.pdf")
    assert len(out) == 1
    assert out[0].error is not None
    assert "probe_failed" in out[0].error
    assert "connection refused" in out[0].error
    assert out[0].target_kind == "unknown"


def test_max_links_caps_extraction() -> None:
    text = " ".join(f"https://x.com/p{i}" for i in range(50))
    calls = {"n": 0}

    def head(_url: str, _t: float) -> _HeadResponse:  # noqa: ARG001
        calls["n"] += 1
        return _resp()

    resolver = HttpHeadLinkResolver(head_request=head, max_links=4)
    out = resolver.resolve(text)
    assert len(out) == 4
    assert calls["n"] == 4


def test_default_max_links_constant() -> None:
    assert DEFAULT_MAX_LINKS == 8


def test_dedup_preserves_order() -> None:
    text = (
        "first https://a.com/x then https://b.com/y "
        "and again https://a.com/x and a new https://c.com/z"
    )
    seen: list[str] = []

    def head(url: str, _t: float) -> _HeadResponse:  # noqa: ARG001
        seen.append(url)
        return _resp()

    HttpHeadLinkResolver(head_request=head).resolve(text)
    assert seen == [
        "https://a.com/x",
        "https://b.com/y",
        "https://c.com/z",
    ]


def test_unsafe_url_dropped_before_network() -> None:
    calls = {"n": 0}

    def head(_url: str, _t: float) -> _HeadResponse:  # noqa: ARG001
        calls["n"] += 1
        return _resp()

    text = "internal http://192.168.1.1/admin and external https://ok.example/x"
    resolver = HttpHeadLinkResolver(head_request=head)
    out = resolver.resolve(text)
    by_url = {r.url: r for r in out}
    # the internal link is reported as dropped, no HEAD call for it
    assert by_url["http://192.168.1.1/admin"].error == "unsafe_url"
    assert by_url["http://192.168.1.1/admin"].summary.startswith("dropped:")
    # external one was probed
    assert by_url["https://ok.example/x"].error is None
    assert calls["n"] == 1


def test_empty_text_returns_empty_list() -> None:
    assert HttpHeadLinkResolver(head_request=lambda *_a: _resp()).resolve("") == []


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://example.com/x", True),
        ("http://example.com/x", True),
        ("ftp://example.com/x", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("http://127.0.0.1/x", False),
        ("http://10.0.0.5/x", False),
        ("http://172.16.0.1/x", False),
        ("http://169.254.169.254/", False),  # AWS metadata
        ("http://[::1]/", False),  # IPv6 loopback
    ],
)
def test_is_safe_external_url(url: str, safe: bool) -> None:
    assert _is_safe_external_url(url) is safe
