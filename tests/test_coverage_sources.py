"""Synthetic public-source/extraction contracts; no live scientific validation."""
import io
import socket
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfWriter

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


@pytest.mark.parametrize("document", ["jats", "namespaced-jats", "tei"])
def test_xml_figure_text_before_document_body_does_not_truncate_paper(document):
    figure = '<svg:svg><svg:text>Illustration label.</svg:text></svg:svg>'
    abstract = '<abstract><p>Synthetic abstract.</p></abstract>'
    body = '<body><p>Measured synthetic result.</p><table><tr><th>Group</th><th>Outcome</th></tr><tr><td>A</td><td>12</td></tr></table></body>'
    back = '<back><ref-list><ref>Source citation.</ref></ref-list></back>'
    if document == "tei":
        body = body.replace("tr>", "row>").replace("th>", "cell>").replace("td>", "cell>")
        raw = f'<TEI xmlns="http://www.tei-c.org/ns/1.0" xmlns:svg="http://www.w3.org/2000/svg"><teiHeader>{figure}{abstract}</teiHeader><text>{body}{back}</text></TEI>'
    else:
        namespace = ' xmlns="urn:synthetic-jats"' if document == "namespaced-jats" else ""
        raw = f'<article{namespace} xmlns:svg="http://www.w3.org/2000/svg"><front>{figure}{abstract}</front>{body}{back}</article>'
    text = sources._xml_text(raw.encode())
    assert all(value in text for value in ("Synthetic abstract.", "Measured synthetic result.", "Group | Outcome", "A | 12", "Source citation."))
    assert "Illustration label." not in text


def test_subscription_preview_is_not_a_full_html_paper():
    raw = b'<meta name="citation_title" content="Synthetic paper"><article><h2>Methods</h2><p>Results are described briefly.</p><p>This is a preview of subscription content.</p></article>'
    with pytest.raises(ValueError):
        sources.article_text(raw, {"title": "Synthetic paper"})


def test_raw_source_pdf_hint_must_match_the_selected_paper(monkeypatch):
    url = "https://example.org/author/paper.pdf"
    work = {"display_name": "Synthetic paper", "primary_location": {"raw_source_name": url},
            "locations": [{"raw_source_name": "Journal with https://example.org/other.pdf"},
                          {"raw_source_name": "file:///private/paper.pdf"}]}
    assert sources.public_candidates(work) == [url]
    seen = []
    def download(client, requested, resolver, expected):
        seen.append((requested, expected))
        sources.public_url(requested, resolver)
        return b"%PDF synthetic", "application/pdf", requested
    monkeypatch.setattr(sources, "public_download", download)
    monkeypatch.setattr(sources, "_pdf_text", lambda raw: ("Synthetic readable paper. " * 30, 1, ()))
    monkeypatch.setattr(sources, "pdf_matches_work", lambda raw, expected: False)
    assert sources.public_work_download(work, None, public_resolver)[0] is None
    monkeypatch.setattr(sources, "pdf_matches_work", lambda raw, expected: True)
    assert sources.public_work_download(work, None, public_resolver)[0][2] == url
    assert seen == [(url, work), (url, work)]


def test_unreadable_fast_pdf_does_not_discard_readable_copy_or_wait_for_loser(monkeypatch):
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    raw_pdf = pdf.getvalue()
    raw_xml = b"<article><body><sec><title>Results</title><p>" + b"Readable synthetic result. " * 30 + b"</p></sec></body></article>"
    unreadable_rejected, loser_started, release_loser = Event(), Event(), Event()
    monkeypatch.setattr(sources, "_native_pdf_text", lambda raw: None)
    extract_pdf = sources._pdf_text

    def reject_blank_pdf(raw):
        try:
            return extract_pdf(raw)
        finally:
            unreadable_rejected.set()

    monkeypatch.setattr(sources, "_pdf_text", reject_blank_pdf)

    def handler(request):
        if request.url.path == "/unreadable.pdf":
            return httpx.Response(200, content=raw_pdf)
        if request.url.path == "/readable.xml":
            assert unreadable_rejected.wait(timeout=5)
            assert loser_started.wait(timeout=5)
            return httpx.Response(200, content=raw_xml)
        loser_started.set()
        assert release_loser.wait(timeout=5)
        return httpx.Response(404)

    work = {"locations": [{"is_oa": True, "pdf_url": "https://example.org/" + name}
                          for name in ("unreadable.pdf", "readable.xml", "slow.pdf")]}
    try:
        result, timed_out = sources.public_work_download(work, httpx.MockTransport(handler), public_resolver)
        assert result == (raw_xml, "application/xml", "https://example.org/readable.xml")
        assert not timed_out and not release_loser.is_set()
    finally:
        release_loser.set()


@pytest.mark.parametrize("raw", [
    b"<article><body><table><tr><th>Group</th><th>Outcome</th></tr><tr><td>A</td><td>12</td></tr><tr><td>B</td><td>19</td></tr></table></body></article>",
    b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><table><row><cell>Group</cell><cell>Outcome</cell></row><row><cell>A</cell><cell>12</cell></row><row><cell>B</cell><cell>19</cell></row></table></body></text></TEI>',
])
def test_xml_preserves_table_cell_and_row_boundaries(raw):
    text = sources._xml_text(raw)
    assert text == "Group | Outcome\n\nA | 12\n\nB | 19"
    assert "A12" not in text and "B19" not in text


@pytest.mark.parametrize("kind", ["pdf", "xml"])
def test_readable_preview_cannot_win_race_over_complete_copy(monkeypatch, kind):
    preview_text = "This is a preview of subscription content. " + "Synthetic section excerpt. " * 30
    preview_xml = b"<article><body><sec><title>Results</title><p>" + preview_text.encode() + b"</p></sec></body></article>"
    raw_preview = b"%PDF-1.7 synthetic preview\n%%EOF" if kind == "pdf" else preview_xml
    complete_xml = b"<article><body><sec><title>Results</title><p>" + b"Complete synthetic result. " * 30 + b"</p></sec></body></article>"
    preview_extracted, loser_started, release_loser = Event(), Event(), Event()
    extract_xml = sources._xml_text

    def xml_text(raw):
        text = extract_xml(raw)
        if raw == preview_xml:
            preview_extracted.set()
        return text

    def pdf_text(raw):
        assert raw == raw_preview
        preview_extracted.set()
        return preview_text, 1, ()

    monkeypatch.setattr(sources, "_xml_text", xml_text)
    monkeypatch.setattr(sources, "_pdf_text", pdf_text)

    def handler(request):
        if request.url.path.startswith("/preview."):
            return httpx.Response(200, content=raw_preview)
        if request.url.path == "/complete.xml":
            assert preview_extracted.wait(timeout=5)
            assert loser_started.wait(timeout=5)
            return httpx.Response(200, content=complete_xml)
        loser_started.set()
        assert release_loser.wait(timeout=5)
        return httpx.Response(404)

    work = {"locations": [{"is_oa": True, "pdf_url": "https://example.org/" + name}
                          for name in ("preview." + kind, "complete.xml", "slow.pdf")]}
    try:
        result, timed_out = sources.public_work_download(work, httpx.MockTransport(handler), public_resolver)
        assert result == (complete_xml, "application/xml", "https://example.org/complete.xml")
        assert not timed_out and preview_extracted.is_set() and not release_loser.is_set()
    finally:
        release_loser.set()
