"""Input sanitization (2026-10-09 audit): stored-XSS links, oversized fields, security headers."""

import pytest
from fastapi.testclient import TestClient

from read.ingest import check_meta
from read.review import check_checklist
from read.webapp import SECURITY_HEADERS, create_app

META = {
    "work_id": "test-source",
    "title": "T",
    "publisher": "P",
    "url": "https://example.org/a.pdf",
    "pub_date": "2020",
    "doc_type": "practice_guide",
}


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "//evil.example/x",
        'https://ok.org/"><script>',
        "ftp://example.org/a.pdf",
    ],
)
def test_source_url_must_be_http(url):
    assert any("url must be" in p for p in check_meta({**META, "url": url}))


def test_good_meta_passes_and_limits_apply():
    assert check_meta(META) == []
    assert check_meta({**META, "url": "http://example.org/x?a=1&b=2#p3"}) == []
    assert any("too long" in p for p in check_meta({**META, "title": "x" * 1001}))
    assert any("superseded_by" in p for p in check_meta({**META, "superseded_by": "../etc"}))
    assert any("work_id" in p for p in check_meta({**META, "work_id": "../../index"}))


def crit(**kw):
    c = {
        "criterion_id": "c1",
        "component": "phonics",
        "grade_band": "K-2",
        "question": "Does it?",
        "search_query": "phonics",
        "scope": "section",
    }
    return c | kw


def test_checklist_size_limits():
    assert check_checklist({"criteria": [crit()]}) == []
    assert check_checklist({"criteria": [crit(question="x" * 501)]})
    assert check_checklist({"criteria": [crit(criterion_id=f"c{i}") for i in range(41)]})
    assert check_checklist({"criteria": ["not an object"]})


def test_security_headers_on_pages_and_api():
    from read.jobs import Env

    env = Env(stores=lambda: None, temp=None, corpus=None, app_files=None, start=lambda p: None)
    c = TestClient(create_app(env))
    for path in ("/", "/sources", "/api/ping"):
        h = c.get(path).headers
        assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
        assert "connect-src 'self'" in h["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]


def test_sources_page_only_links_http_urls():
    from read.jobs import WEB

    html = (WEB / "sources.html").read_text(encoding="utf-8")
    assert "/^https?:\/\//i.test(current.url)" in html
