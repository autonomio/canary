"""Synthetic public-source/extraction contracts; no live scientific validation."""
import io
import socket
import textwrap
from threading import Event
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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


@pytest.mark.parametrize("paragraph", [
    "Measured 1.<em>23</em> mg with p&lt;<span>0.05</span>.",
    "Measured <strong>1.<em>23</em></strong> mg with p&lt;<span>0.05</span>.",
    "Measured <span>1.</span><span>23</span> mg with p&lt;0.05.",
])
def test_html_inline_markup_preserves_measurement_characters(paragraph):
    raw = f"<article><h2>Results</h2><p>{paragraph}</p></article>"
    assert sources.article_text(raw) == "Results\n\nMeasured 1.23 mg with p<0.05."


def test_html_block_and_table_boundaries_do_not_fuse_measurements():
    raw = "<article><h2>Results</h2><p><span>First</span> result.</p><p>Second result.</p><table><tr><td>1.<em>23</em></td><td>4.56</td></tr><tr><td>7.89</td><td>10.11</td></tr></table></article>"
    assert sources.article_text(raw) == "Results\n\nFirst result.\n\nSecond result.\n\n1.23 | 4.56\n\n7.89 | 10.11"


def _paper_pdf(text=None):
    """An actual PDF fixture, with extractable text or a truly blank page."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text is not None:
        font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        lines = [line for paragraph in text.splitlines() for line in textwrap.wrap(paragraph, width=85)]
        commands = ["BT /F1 10 Tf 40 750 Td 12 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append("(" + escaped + ") Tj T*")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = io.BytesIO()
    writer.write(pdf)
    return pdf.getvalue()


def _complete_landing(pdf_link=True):
    link = '<meta name="citation_pdf_url" content="/paper.pdf">' if pdf_link else '<a href="/paper.pdf">PDF</a>'
    return ('<meta name="citation_title" content="Synthetic context annotation study">'
        '<meta name="citation_doi" content="10.9999/contexts">' + link +
        '<article><h2>Methods</h2><div>Observed 1.<em>23</em> minutes of added effort. ' +
        'Researchers recorded subject and circumstances before a delayed retrieval task. ' * 8 +
        '</div><h2>Results</h2><p>' + 'The synthetic comparison reports retrieval and recording effort separately. ' * 8 +
        '</p></article>').encode()


@pytest.mark.parametrize("pdf_state", ["blank", "preview", "too_short"])
def test_unusable_metadata_linked_pdf_keeps_complete_landing_html(monkeypatch, pdf_state):
    monkeypatch.setattr(sources, "_native_pdf_text", lambda raw: None)
    if pdf_state == "blank":
        raw_pdf = _paper_pdf()
    elif pdf_state == "preview":
        raw_pdf = _paper_pdf("Synthetic context annotation study\nThis is a preview of subscription content.\n" +
            "Only a limited excerpt of the study is available in this copy. " * 14)
    else:
        raw_pdf = _paper_pdf("Synthetic context annotation study\nResults\nA brief excerpt, not a full paper.")
    landing = _complete_landing()
    requested = []
    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/landing":
            return httpx.Response(200, content=landing)
        if request.url.path == "/paper.pdf":
            return httpx.Response(200, content=raw_pdf)
        return httpx.Response(404)
    work = {"display_name": "Synthetic context annotation study",
        "primary_location": {"is_oa": True, "landing_page_url": "https://example.org/landing"}}
    result, timed_out = sources.public_work_download(work, httpx.MockTransport(handler), public_resolver)
    assert result == (landing, "text/html", "https://example.org/landing")
    assert timed_out is False
    assert requested == ["https://example.org/landing", "https://example.org/paper.pdf"]
    text = sources.article_text(result[0], work)
    assert "Observed 1.23 minutes of added effort." in text
    assert "Methods" in text and "Results" in text


def test_healthy_metadata_linked_pdf_extracts_once_and_does_not_wait_for_losing_copy(monkeypatch):
    monkeypatch.setattr(sources, "_native_pdf_text", lambda raw: None)
    raw_pdf = _paper_pdf("Synthetic context annotation study\nMethods\n" +
        "Researchers recorded subjects and circumstances before delayed retrieval. " * 9 +
        "\nResults\n" + "The comparison reports retrieval and effort separately. " * 9)
    landing = _complete_landing()
    loser_started, release_loser = Event(), Event()
    extracted = []
    extract_pdf = sources._pdf_text
    def count_extraction(raw):
        extracted.append(raw)
        return extract_pdf(raw)
    monkeypatch.setattr(sources, "_pdf_text", count_extraction)
    def handler(request):
        if request.url.path == "/landing":
            return httpx.Response(200, content=landing)
        if request.url.path == "/paper.pdf":
            assert loser_started.wait(timeout=5)
            return httpx.Response(200, content=raw_pdf)
        loser_started.set()
        assert release_loser.wait(timeout=5)
        return httpx.Response(404)
    work = {"display_name": "Synthetic context annotation study", "locations": [
        {"is_oa": True, "landing_page_url": "https://example.org/landing"},
        {"is_oa": True, "pdf_url": "https://example.org/slow.pdf"}]}
    try:
        result, timed_out = sources.public_work_download(work, httpx.MockTransport(handler), public_resolver)
        assert result == (raw_pdf, "application/pdf", "https://example.org/paper.pdf")
        assert timed_out is False and not release_loser.is_set()
        assert extracted == [raw_pdf]
    finally:
        release_loser.set()


def test_pdf_link_fallback_preserves_landing_title_and_doi_identity(monkeypatch):
    monkeypatch.setattr(sources, "_native_pdf_text", lambda raw: None)
    landing = _complete_landing(pdf_link=False)
    unrelated = _paper_pdf("A different study\nResults\n" + "Unrelated measured results and methods. " * 30)
    expected = {"title": "Synthetic context annotation study", "doi": "10.9999/contexts"}
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=landing if request.url.path == "/landing" else unrelated)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sources.public_download(client, "https://example.org/landing", public_resolver, expected)
        assert result == (landing, "text/html", "https://example.org/landing")
        assert requested == ["https://example.org/landing", "https://example.org/paper.pdf"]
        for mismatched in ({"title": "Wrong paper", "doi": expected["doi"]},
                           {"title": expected["title"], "doi": "10.9999/wrong"}):
            with pytest.raises(ValueError, match="does not match"):
                sources.public_download(client, "https://example.org/landing", public_resolver, mismatched)
    assert requested[2:] == ["https://example.org/landing", "https://example.org/landing"]


@pytest.mark.parametrize("inline", [
    "Observed 1.<em>23</em> minutes with p&lt;<span>0.05</span>.",
    "Observed <span>1.</span><strong><em>23</em></strong> minutes with p&lt;0.05.",
    "Observed 1.<span hidden>irrelevant</span><em>23</em> minutes with p&lt;0.05.",
])
def test_html_div_keeps_contiguous_inline_measurements_in_one_paragraph(inline):
    raw = "<article><h2>Results</h2><div>" + inline + "</div></article>"
    assert sources.article_text(raw) == "Results\n\nObserved 1.23 minutes with p<0.05."


def test_html_nested_mixed_blocks_keep_inline_runs_and_structural_boundaries():
    raw = ('<article><div><h2>Results</h2>Initial 1.<em>23</em> mg.'
        '<section><h3>Methods</h3><div>Nested <strong>4.<em>56</em></strong> mg.'
        '<p>Separate 7.<span>89</span> mg.</p>Trailing 10.<em>11</em> mg.</div></section>'
        'After 12.<em>34</em> mg.<table><tr><td>15.<em>67</em></td><td>18.90</td></tr></table>'
        'Final 21.<span>23</span> mg.</div></article>')
    assert sources.article_text(raw) == (
        "Results\n\nInitial 1.23 mg.\n\nMethods\n\nNested 4.56 mg.\n\nSeparate 7.89 mg."
        "\n\nTrailing 10.11 mg.\n\nAfter 12.34 mg.\n\n15.67 | 18.90\n\nFinal 21.23 mg.")


def test_html_inline_wrapper_around_real_blocks_keeps_paragraph_boundaries():
    raw = ('<article><h2>Results</h2><div><span>Before 1.<em>23</em> mg.'
        '<div>Nested 4.<strong>56</strong> mg.</div>After 7.<em>89</em> mg.</span></div></article>')
    assert sources.article_text(raw) == "Results\n\nBefore 1.23 mg.\n\nNested 4.56 mg.\n\nAfter 7.89 mg."


def test_html_table_preserves_caption_headers_rows_inline_units_and_empty_positions():
    raw = ('<article><h2>Results</h2><table><caption>Table 1. Measured yields.</caption>'
        '<thead><tr><th>Group</th><th>Yield (<span>mg</span>)</th></tr></thead>'
        '<tbody><tr><th scope="row">A</th><td>1.<em>23</em> <span>mg</span></td></tr>'
        '<tr><th scope="row">B</th><td>4.<strong>56</strong> mg</td></tr>'
        '<tr><th scope="row">C</th><td></td></tr><tr><td></td><td>7.<em>89</em> mg</td></tr>'
        '</tbody></table><p>Source caveat.</p></article>')
    assert sources.article_text(raw) == (
        "Results\n\nTable 1. Measured yields.\n\nGroup | Yield (mg)\n\nA | 1.23 mg"
        "\n\nB | 4.56 mg\n\nC | \n\n | 7.89 mg\n\nSource caveat.")


def test_html_table_spans_keep_logical_columns_without_repeating_measurements():
    raw = ('<article><h2>Results</h2><table><thead>'
        '<tr><th rowspan="2">Sample</th><th colspan="2">Yield (mg)</th></tr>'
        '<tr><th>Early</th><th>Late</th></tr></thead><tbody>'
        '<tr><th rowspan="2">A</th><td>1.<em>23</em></td><td>4.<em>56</em></td></tr>'
        '<tr><td>7.89</td><td></td></tr></tbody></table></article>')
    text = sources.article_text(raw)
    assert text == "Results\n\nSample | Yield (mg) | \n\n | Early | Late\n\nA | 1.23 | 4.56\n\n | 7.89 | "
    assert text.count("Sample") == text.count("Yield (mg)") == text.count("1.23") == text.count("4.56") == 1
    assert "rowspan" not in text and "colspan" not in text and "span from" not in text


def test_html_table_rowspan_zero_stops_at_its_row_group():
    raw = ('<article><h2>Results</h2><table><tbody>'
        '<tr><th rowspan="0">A</th><td>1.23 mg</td></tr><tr><td>4.56 mg</td></tr>'
        '</tbody><tbody><tr><th>B</th><td>7.89 mg</td></tr></tbody></table></article>')
    assert sources.article_text(raw) == "Results\n\nA | 1.23 mg\n\n | 4.56 mg\n\nB | 7.89 mg"


def test_html_nested_table_rows_remain_inside_their_outer_cell():
    raw = ('<article><h2>Results</h2><table><tr><th>Group</th><th>Records</th></tr>'
        '<tr><th>A</th><td><table><caption>Nested measurements.</caption>'
        '<tr><th>Time</th><th>Yield</th></tr><tr><td>Early</td><td>1.<em>23</em> mg</td></tr>'
        '<tr><td>Late</td><td>4.56 mg</td></tr></table></td></tr>'
        '<tr><th>B</th><td>7.89 mg</td></tr></table></article>')
    text = sources.article_text(raw)
    assert text == (
        "Results\n\nGroup | Records\n\nA | [\nNested measurements.\nTime | Yield"
        "\nEarly | 1.23 mg\nLate | 4.56 mg\n]\n\nB | 7.89 mg")
    assert text.count("Early | 1.23 mg") == text.count("Late | 4.56 mg") == 1


def test_html_table_canonical_passages_reproduce_exact_label_value_rows():
    from canary.coverage_runtime import canonical_passages
    raw = ('<article><h2>Results</h2><table><caption>Measured yields.</caption>'
        '<tr><th>Group</th><th>Yield</th></tr><tr><th>A</th><td>1.<em>23</em> mg</td></tr>'
        '<tr><th>B</th><td>4.56 mg</td></tr></table><p>Source caveat.</p></article>')
    text = sources.article_text(raw)
    passages = canonical_passages(text, "W1")
    assert [row["text"] for row in passages] == [
        "Results", "Measured yields.", "Group | Yield", "A | 1.23 mg", "B | 4.56 mg", "Source caveat."]
    assert all(row["reference_id"] == "W1" and row["text"] == text[row["start"]:row["end"]] for row in passages)
    assert "\n\n".join(row["text"] for row in passages) == text


def test_html_table_inside_blockquote_keeps_rows_and_surrounding_source_words():
    raw = ('<article><h2>Results</h2><blockquote>Source reports:'
        '<table><tr><th>Group</th><th>Yield</th></tr><tr><th>A</th><td>1.<em>23</em> mg</td></tr>'
        '<tr><th>B</th><td>4.56 mg</td></tr></table>Source caveat.</blockquote></article>')
    assert sources.article_text(raw) == (
        "Results\n\nSource reports:\n\nGroup | Yield\n\nA | 1.23 mg\n\nB | 4.56 mg\n\nSource caveat.")


@pytest.mark.parametrize("colspan", ["1000000000", "1001", "1000"])
def test_html_giant_colspan_is_bounded_before_expansion_without_duplicating_values(monkeypatch, colspan):
    expansions = []
    def bounded_range(*args):
        result = range(*args)
        # Fail before the old implementation could loop/allocate a billion slots.
        assert len(result) <= 1001, "Public column span was not bounded before expansion"
        expansions.append(len(result))
        return result
    monkeypatch.setattr(sources, "range", bounded_range, raising=False)
    raw = ('<article><h2>Results</h2><table>'
        '<tr><td colspan="' + colspan + '">1.<em>23</em> mg</td><td>4.56 mg</td></tr>'
        '<tr><td colspan="' + colspan + '">7.89 mg</td><td>10.11 mg</td></tr>'
        '</table></article>')
    text = sources.article_text(raw)
    rows = text.split("\n\n")[1:]
    assert len(rows) == 2
    assert rows[0].split(" | ") == ["1.23 mg"] + [""] * 999 + ["4.56 mg"]
    assert rows[1].split(" | ") == ["7.89 mg"] + [""] * 999 + ["10.11 mg"]
    assert max(expansions) == 1001
    assert all(text.count(value) == 1 for value in ("1.23 mg", "4.56 mg", "7.89 mg", "10.11 mg"))


@pytest.mark.parametrize("attrs", [
    'colspan="not-a-number" rowspan="not-a-number"',
    'colspan="-2" rowspan="-2"',
    'colspan="1.5" rowspan="1.5"',
    'colspan="" rowspan=""',
    'colspan rowspan',
    'colspan="0" rowspan="1"',
])
def test_html_malformed_span_attributes_keep_ordinary_cell_alignment(attrs):
    raw = ('<article><h2>Results</h2><table>'
        '<tr><th>Group</th><th>Yield</th></tr>'
        '<tr><th ' + attrs + '>A</th><td>1.<em>23</em> mg</td></tr>'
        '<tr><th>B</th><td>4.56 mg</td></tr></table></article>')
    assert sources.article_text(raw) == "Results\n\nGroup | Yield\n\nA | 1.23 mg\n\nB | 4.56 mg"


def test_html_ordinary_colspan_still_preserves_empty_slots_and_inline_numbers():
    raw = ('<article><h2>Results</h2><table>'
        '<tr><th colspan="2">Yield (<span>mg</span>)</th></tr>'
        '<tr><td>1.<em>23</em> mg</td><td>4.56 mg</td></tr>'
        '<tr><td></td><td>7.89 mg</td></tr></table></article>')
    assert sources.article_text(raw) == "Results\n\nYield (mg) | \n\n1.23 mg | 4.56 mg\n\n | 7.89 mg"


def test_html_thousand_accumulating_rowspans_fall_back_before_logical_expansion(monkeypatch):
    def no_dense_expansion(*args):
        result = range(*args)
        assert len(result) < 1000, "Accumulating table entered dense expansion instead of compact fallback"
        return result
    monkeypatch.setattr(sources, "range", no_dense_expansion, raising=False)
    values = ["R" + str(index).zfill(4) + " 1." + str(index).zfill(4) + " mg" for index in range(1000)]
    raw = '<article><h2>Results</h2><table><tbody>' + ''.join(
        '<tr><td rowspan="0" colspan="1000">' + value + '</td></tr>' for value in values) + '</tbody></table></article>'
    text = sources.article_text(raw)
    table = ElementTree.fromstring(text.split("\n\n", 1)[1])
    rows = table.findall("./tbody/tr")
    assert len(rows) == 1000
    assert [row.find("td").text for row in rows] == values
    assert all(row.find("td").attrib == {"rowspan": "0", "colspan": "1000"} for row in rows)
    assert len(text) < 100_000


def test_html_thousand_flat_rows_keep_ordinary_layout():
    values = [("R" + str(index).zfill(4), str(index) + ".23 mg") for index in range(1000)]
    raw = '<article><h2>Results</h2><table><tbody>' + ''.join(
        '<tr><th>' + label + '</th><td>' + value + '</td></tr>' for label, value in values) + '</tbody></table></article>'
    text = sources.article_text(raw)
    assert text == "Results\n\n" + "\n\n".join(label + " | " + value for label, value in values)
    assert "<table>" not in text


def test_html_multiple_tables_share_one_body_expansion_budget(monkeypatch):
    monkeypatch.setattr(sources, "MAX_HTML_TABLE_EXPANSION", 36)
    raw = ('<article><h2>Results</h2>'
        '<table><tr><th>A</th><td>1.<em>23</em> mg</td></tr></table>'
        '<table><tr><th>B</th><td>4.56 mg</td></tr><tr><th>C</th><td>7.89 mg</td></tr></table>'
        '<table><tr><td>10.11 mg</td></tr></table></article>')
    assert sources.article_text(raw) == (
        "Results\n\nA | 1.23 mg\n\nB | 4.56 mg\n\nC | 7.89 mg"
        "\n\n<table>\n<tr><td>10.11 mg</td></tr>\n</table>")


def test_html_nested_tables_share_parent_expansion_budget(monkeypatch):
    monkeypatch.setattr(sources, "MAX_HTML_TABLE_EXPANSION", 30)
    raw = ('<article><h2>Results</h2><table><tr><th>A</th><td>'
        '<table><tr><th>Early</th><td>1.<em>23</em> mg</td></tr><tr><th>Late</th><td>4.56 mg</td></tr></table>'
        '</td></tr></table></article>')
    assert sources.article_text(raw) == (
        "Results\n\nA | [\n<table>\n<tr><th>Early</th><td>1.23 mg</td></tr>"
        "\n<tr><th>Late</th><td>4.56 mg</td></tr>\n</table>\n]")


def test_html_compact_fallback_keeps_source_cells_spans_caption_and_exact_passages(monkeypatch):
    from canary.coverage_runtime import canonical_passages
    monkeypatch.setattr(sources, "MAX_HTML_TABLE_EXPANSION", 1)
    raw = ('<article><h2>Results</h2><table><caption>Measured yields.</caption><thead>'
        '<tr><th id="yield" colspan="2" scope="colgroup">Yield (<em>mg</em>)</th></tr></thead><tbody>'
        '<tr><td rowspan="2" headers="yield">1.<em>23</em> mg</td><td>p&lt;<span>0.05</span></td></tr>'
        '<tr><td></td></tr></tbody></table><p>Source caveat.</p></article>')
    text = sources.article_text(raw)
    markup = text.split("\n\n")[1]
    table = ElementTree.fromstring(markup)
    assert table.find("caption").text == "Measured yields."
    assert table.find("./thead/tr/th").text == "Yield (mg)"
    assert table.find("./thead/tr/th").attrib == {"id": "yield", "colspan": "2", "scope": "colgroup"}
    cells = table.findall("./tbody/tr/td")
    assert [cell.text or "" for cell in cells] == ["1.23 mg", "p<0.05", ""]
    assert cells[0].attrib == {"rowspan": "2", "headers": "yield"}
    assert "span from" not in text and "1.<em>23" not in text
    passages = canonical_passages(text, "W1")
    assert all(row["text"] == text[row["start"]:row["end"]] for row in passages)
    assert "\n\n".join(row["text"] for row in passages) == text


@pytest.mark.parametrize("rows", [
    "<tr><td>A<td>12<tr><td>B<td>19",
    "<tr><td>A<td>12</tr><tr><td>B<td>19</tr>",
    "<tr><td>A</td><td>12</td><tr><td>B</td><td>19</td>",
    "<tr><th>A<td>12<tr><th>B<td>19",
])
def test_html_optional_cell_and_row_endings_preserve_exact_label_value_rows(rows):
    raw = "<article><h2>Results</h2><table>" + rows + "</table></article>"
    explicit = "<article><h2>Results</h2><table><tr><td>A</td><td>12</td></tr><tr><td>B</td><td>19</td></tr></table></article>"
    assert sources.article_text(raw) == sources.article_text(explicit) == "Results\n\nA | 12\n\nB | 19"


def test_html_optional_group_endings_preserve_caption_headers_spans_and_empty_cells():
    raw = ('<article><h2>Results</h2><table><caption>Reported yields.</caption><thead>'
        '<tr><th rowspan="2">Sample<th colspan="2">Yield (mg)<tr><th>Early<th>Late'
        '<tbody><tr><th rowspan="2">A<td>1.<em>23</em><td>4.<strong>56</strong>'
        '<tr><td>7.89<td><tfoot><tr><td colspan="3">Source caveat.</table></article>')
    assert sources.article_text(raw) == (
        "Results\n\nReported yields.\n\nSample | Yield (mg) | \n\n | Early | Late"
        "\n\nA | 1.23 | 4.56\n\n | 7.89 | \n\nSource caveat. |  | ")


def test_html_optional_endings_in_nested_tables_do_not_close_outer_cells():
    raw = ('<article><h2>Results</h2><table><tr><th>Group<th>Records<th>Note'
        '<tr><th>A<td><table><tr><th>Time<th>Yield<tr><th>Early<td>1.<em>23</em> mg'
        '<tr><th>Late<td>4.56 mg</table><td>Independent note.'
        '<tr><th>B<td>7.89 mg<td>Other note.</table></article>')
    assert sources.article_text(raw) == (
        "Results\n\nGroup | Records | Note\n\nA | [\nTime | Yield\nEarly | 1.23 mg"
        "\nLate | 4.56 mg\n] | Independent note.\n\nB | 7.89 mg | Other note.")


def test_html_optional_endings_keep_compact_fallback_rows_and_original_spans(monkeypatch):
    monkeypatch.setattr(sources, "MAX_HTML_TABLE_EXPANSION", 1)
    raw = ('<article><h2>Results</h2><table><tbody>'
        '<tr><td rowspan="0" colspan="1000">A 1.<em>23</em> mg'
        '<tr><td>B 4.<strong>56</strong> mg</table></article>')
    text = sources.article_text(raw)
    table = ElementTree.fromstring(text.split("\n\n", 1)[1])
    rows = table.findall("./tbody/tr")
    assert len(rows) == 2
    assert [row.find("td").text for row in rows] == ["A 1.23 mg", "B 4.56 mg"]
    assert rows[0].find("td").attrib == {"rowspan": "0", "colspan": "1000"}


def test_html_optional_endings_canonical_passages_reproduce_measurements_and_offsets():
    from canary.coverage_runtime import canonical_passages
    raw = ('<article><h2>Results</h2><table><tr><th>Group<th>Yield'
        '<tr><td>A<td>1.<em>23</em> mg<tr><td>B<td>4.<span>56</span> mg'
        '</table><p>Source caveat.</p></article>')
    text = sources.article_text(raw)
    passages = canonical_passages(text, "W1")
    assert [row["text"] for row in passages] == [
        "Results", "Group | Yield", "A | 1.23 mg", "B | 4.56 mg", "Source caveat."]
    assert all(row["reference_id"] == "W1" and row["text"] == text[row["start"]:row["end"]] for row in passages)
    assert "\n\n".join(row["text"] for row in passages) == text
