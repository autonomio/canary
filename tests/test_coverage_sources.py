"""Synthetic public-source/extraction contracts; no live scientific validation."""
import socket
from types import SimpleNamespace

import httpx
import pytest

from canary import coverage_sources as sources


def public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def test_valueless_landing_attributes_do_not_break_identified_pdf_links():
    raw = b'<meta name="citation_title" content="Synthetic paper"><meta name="citation_doi" content="10.9999/synthetic"><a href>empty</a><link type><a href="../paper.pdf">PDF</a>'
    assert sources.repository_pdf_urls(raw, {"title": "Synthetic paper", "doi": "10.9999/synthetic"}) == ["../paper.pdf"]
    assert sources.repository_pdf_urls(raw, {"title": "Different paper", "doi": "10.9999/other"}) == []


def test_redirect_destinations_are_checked_before_second_request():
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private.pdf"})
    def resolver(host, port, **kwargs):
        address = "127.0.0.1" if host == "127.0.0.1" else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="public addresses"):
            sources.public_download(client, "https://example.org/paper.pdf", resolver)
    assert requested == ["https://example.org/paper.pdf"]
    with pytest.raises(ValueError, match="disabled"):
        sources.public_url("https://content.openalex.org/paid-paper.pdf", public_resolver)


def test_html_abstract_preview_and_access_challenge_are_not_article_bodies():
    expected = {"title": "Synthetic paper"}
    abstract = b'<meta name="citation_title" content="Synthetic paper"><article><h2>Abstract</h2><p>Only an abstract.</p></article>'
    blocked = b'<meta name="citation_title" content="Synthetic paper"><article><h2>Methods</h2><p>Sign in to read the article.</p></article>'
    complete = b'<meta name="citation_title" content="Synthetic paper"><article><h2>Methods</h2><p>A measured synthetic comparison.</p><h2>Results</h2><p>Reported result.</p></article>'
    for raw in (abstract, blocked):
        with pytest.raises(ValueError):
            sources.article_text(raw, expected)
    assert "A measured synthetic comparison." in sources.article_text(complete, expected)


def test_native_pdf_extraction_keeps_table_layout_and_blank_pages(monkeypatch):
    monkeypatch.setattr(sources.shutil, "which", lambda command: "/synthetic/pdftotext")
    def run(command, **kwargs):
        assert "-layout" in command and kwargs["input"] == b"%PDF synthetic"
        return SimpleNamespace(stdout=b"Group    Outcome\nA        12\n\f\fB        19\n\f")
    monkeypatch.setattr(sources.subprocess, "run", run)
    text, pages, blank = sources._pdf_text(b"%PDF synthetic")
    assert pages == 3 and blank == (2,)
    assert "Group    Outcome" in text and "A        12" in text
    assert "[PDF page 2]" in text and "[PDF page 3]" in text


def test_xml_requires_full_body_and_keeps_abstract_and_references():
    raw = b'<article><front><abstract><p>Synthetic abstract.</p></abstract></front><body><sec><title>Results</title><p>Synthetic result.</p><table><tr><td>A</td><td>12</td></tr></table></sec></body><back><ref-list><ref>Source citation.</ref></ref-list></back></article>'
    text = sources._xml_text(raw)
    assert all(value in text for value in ("Synthetic abstract.", "Synthetic result.", "Source citation."))
    with pytest.raises(sources.AccessFailure):
        sources._xml_text(b"<article><abstract>Only abstract.</abstract></article>")
