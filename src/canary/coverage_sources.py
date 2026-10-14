"""Public scholarly source resolution and layout-preserving extraction.

Required deterministic helpers promoted from Canary's working source modules.
No provider, ledger, worker-service or experiment dependencies; no paid content.
"""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import io
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree
import xml.etree.ElementTree as ET

import httpx
from pypdf import PdfReader

PUBLIC_DOWNLOAD_TIMEOUT_SECONDS = 10
MAX_HTML_TABLE_EXPANSION = 100_000
PUBLIC_SSL_CONTEXT = httpx.create_ssl_context(trust_env=False)
_PUBLIC_DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="coverage-public-copy")
_PUBLIC_METADATA_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="coverage-public-metadata")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "button", "noscript"}
BLOCKS = {"p", "li", "pre", "blockquote", "figcaption", "table", "math", "h1", "h2", "h3", "h4", "h5", "h6"}
PARAGRAPH_BLOCK_STARTS = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr",
    "main", "menu", "nav", "ol", "p", "pre", "search", "section", "table", "ul"}
BODY_MARKERS = {"articlebody", "articlecontent", "articletext", "arttext", "articlesections",
                "htmlbody", "fulltext", "fullarticle", "mainarticle"}
SCIENCE = re.compile(r"\b(introduction|background|method(?:s|ology)?|materials|results|discussion|conclusions?|"
                     r"experimental|theory|analysis|case study|literature review|findings)\b", re.I)
PREVIEW_MARKER = re.compile(r"article[- ]preview|section snippets|access through your organization|purchase this article|preview\s+of\s+subscription\s+content", re.I)
ACCESS_PROMPT = re.compile(r"\b(access denied|verify you are human|checking your browser|"
                           r"sign in to (?:read|access)|subscribe to (?:read|access)|"
                           r"purchase (?:this|the) article|preview of subscription content|enable javascript to continue)\b", re.I)


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


class _ArticleParser(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.metadata = {}
        self.feed(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)

    def handle_starttag(self, tag, attrs):
        if tag in PARAGRAPH_BLOCK_STARTS:
            for index in range(len(self.stack) - 1, 0, -1):
                current = self.stack[index].tag
                if current == "p":
                    del self.stack[index:]
                    break
                if current in {"button", "table", "td", "th", "caption", "template", "html",
                               "applet", "object", "marquee", "svg", "math"}:
                    break  # Do not close an outer paragraph across nested content.
        # HTML permits omitted cell/row/group endings. Limit implicit closure
        # to the nearest table so an inner table cannot close an outer cell.
        if tag in {"td", "th", "tr", "thead", "tbody", "tfoot"}:
            table = next((index for index in range(len(self.stack) - 1, 0, -1)
                          if self.stack[index].tag == "table"), None)
            if table is not None:
                if tag in {"thead", "tbody", "tfoot"}:
                    del self.stack[table + 1:]
                else:
                    endings = {"td", "th"} if tag in {"td", "th"} else {"tr"}
                    for index in range(len(self.stack) - 1, table, -1):
                        if self.stack[index].tag in endings:
                            del self.stack[index:]
                            break
        values = {key: value or "" for key, value in attrs}
        node = _Node(tag, values)
        self.stack[-1].children.append(node)
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name and values.get("content"):
                self.metadata.setdefault(name, []).append(values["content"])
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node):
    if _skip(node):
        return
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _skip(node):
    return (node.tag in SKIP or "hidden" in node.attrs or node.attrs.get("aria-hidden") == "true"
            or node.attrs.get("role") in ("navigation", "complementary"))


def _plain(node):
    def content(current):
        if _skip(current):
            return ""
        text = "".join(child if isinstance(child, str) else content(child)
                       for child in current.children)
        # Inline markup contributes no new characters; blocks/cells need a
        # separator so adjacent paragraphs and measurements cannot fuse.
        boundaries = BLOCKS | {"div", "section", "br", "hr", "td", "th", "tr", "caption", "dt", "dd"}
        return " " + text + " " if current.tag in boundaries else text

    return " ".join(content(node).split())


def _compact_table(table):
    """Retain source table markup/cells when logical blank padding would amplify it."""
    tags = {"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td"}
    def content(node):
        if _skip(node):
            return ""
        text = "".join(escape(re.sub(r"\s+", " ", child), quote=False) if isinstance(child, str) else content(child)
                       for child in node.children)
        if node.tag not in tags:
            boundaries = BLOCKS | {"div", "section", "br", "hr"}
            return " " + text + " " if node.tag in boundaries else text
        attrs = "".join(' ' + name + '="' + escape(value or "", quote=True) + '"'
                        for name, value in node.attrs.items() if name in {"colspan", "rowspan", "scope", "headers", "id"})
        if node.tag in {"table", "thead", "tbody", "tfoot"}:
            return "<" + node.tag + attrs + ">\n" + text.strip() + "\n</" + node.tag + ">\n"
        suffix = "\n" if node.tag in {"tr", "caption"} else ""
        return "<" + node.tag + attrs + ">" + text.strip() + "</" + node.tag + ">" + suffix
    return "\n".join(line.strip() for line in content(table).splitlines() if line.strip())


def _table_cell_text(node, table_budget=None):
    def content(current):
        if _skip(current):
            return ""
        if current.tag == "table":
            # Only delimiters are added; nested rows stay inside this cell.
            return "\n[\n" + "\n".join(text for _, text in _table_blocks(current, table_budget)) + "\n]\n"
        text = "".join(child if isinstance(child, str) else content(child) for child in current.children)
        boundaries = BLOCKS | {"div", "section", "br", "hr", "td", "th", "caption", "dt", "dd"}
        return " " + text + " " if current.tag in boundaries else text
    return "\n".join(" ".join(line.split()) for line in content(node).splitlines()).strip()


def _table_blocks(table, table_budget=None):
    """Keep the literal caption and logical row/cell positions, without copying spans."""
    rows, captions = [], []
    def collect(node, group):
        if _skip(node) or (node is not table and node.tag == "table"):
            return
        if node.tag == "caption":
            captions.append(_plain(node))
        elif node.tag == "tr":
            rows.append((node, group))
        else:
            if node.tag in {"thead", "tbody", "tfoot"}:
                group = id(node)
            for child in node.children:
                if isinstance(child, _Node):
                    collect(child, group)
    collect(table, id(table))
    groups_remaining = {}
    for _, group in rows:
        groups_remaining[group] = groups_remaining.get(group, 0) + 1
    def span(cell, name):
        try:
            value = int(cell.attrs.get(name, "1"))
            return value if value >= 0 else 1
        except (TypeError, ValueError):
            return 1
    # Count only literal source cells. No logical padding/copies/search happens
    # until a conservative bound covers grid retention and final rectangular output.
    row_cells, group_total, group_max, spanning_groups = [], {}, {}, set()
    for row, group in rows:
        cells = [cell for cell in row.children if isinstance(cell, _Node) and cell.tag in {"td", "th"} and not _skip(cell)]
        row_cells.append((cells, group))
        width = sum(min(1000, max(1, span(cell, "colspan"))) for cell in cells)
        group_total[group] = group_total.get(group, 0) + width
        group_max[group] = max(group_max.get(group, 0), width)
        if groups_remaining[group] > 1 and any(span(cell, "rowspan") != 1 for cell in cells):
            spanning_groups.add(group)
    width_bound = max((group_total[group] if group in spanning_groups else group_max[group] for group in group_total), default=0)
    expansion = 6 * len(rows) * width_bound
    if table_budget is None:
        table_budget = [MAX_HTML_TABLE_EXPANSION]
    if expansion > table_budget[0]:
        yield "table", _compact_table(table)
        return
    table_budget[0] -= expansion
    for caption in captions:
        if caption:
            yield "caption", caption
    occupied, previous_group, grid = {}, None, []
    for cells, group in row_cells:
        if group != previous_group:
            occupied = {}
        values = {column: "" for column in occupied}
        next_occupied = {column: count - 1 for column, count in occupied.items() if count > 1}
        column = 0
        for cell in cells:
            # Match HTML's column-span limit before expanding public markup.
            colspan = min(1000, max(1, span(cell, "colspan")))
            while True:
                conflict = next((column + offset for offset in range(colspan) if column + offset in values), None)
                if conflict is None:
                    break
                column = conflict + 1  # Skip checked gaps instead of rescanning them one start at a time.
            rowspan = span(cell, "rowspan") or groups_remaining[group]
            rowspan = min(rowspan, groups_remaining[group])
            values[column] = _table_cell_text(cell, table_budget)
            for offset in range(colspan):
                values.setdefault(column + offset, "")
                if rowspan > 1:
                    next_occupied[column + offset] = rowspan - 1
            column += colspan
        grid.append(values)
        occupied, previous_group = next_occupied, group
        groups_remaining[group] -= 1
    width = max((max(row, default=-1) + 1 for row in grid), default=0)
    for row in grid:
        if row:
            yield "tr", " | ".join(row.get(column, "") for column in range(width))


def _blocks(node, table_budget=None):
    if table_budget is None:
        table_budget = [MAX_HTML_TABLE_EXPANSION]
    if _skip(node):
        return
    if node.tag == "table":
        yield from _table_blocks(node, table_budget)
        return
    if node.tag in BLOCKS and not any(descendant.tag == "table" for descendant in _walk(node)):
        text = _plain(node)
        if text:
            yield node.tag, text
        return
    structural = BLOCKS | {"div", "section", "article", "main", "ul", "ol", "dl", "dt", "dd", "figure", "details", "summary"}
    inline = []
    def flush():
        text = _plain(_Node("inline", children=inline))
        inline.clear()
        if text:
            yield "p", text
    for child in node.children:
        if isinstance(child, _Node) and _skip(child):
            continue
        if isinstance(child, _Node) and (child.tag in structural or
                any(descendant.tag in structural for descendant in _walk(child))):
            yield from flush()
            yield from _blocks(child, table_budget)
        else:
            inline.append(child)
    yield from flush()


def _normalize(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _doi(value):
    match = re.search(r"10\.[0-9]{4,9}/[^\s<>\"]+", unquote(value), re.I)
    return match[0].lower().rstrip(".") if match else ""


def _identity(parser, expected):
    expected = expected or {}
    doi = _doi(expected.get("doi") or "")
    actual_dois = [_doi(value) for name in ("citation_doi", "prism.doi", "dc.identifier")
                   for value in parser.metadata.get(name, []) if _doi(value)]
    if doi and actual_dois and doi not in actual_dois:
        raise ValueError("HTML DOI does not match the requested paper")
    title = expected.get("title") or expected.get("display_name") or ""
    titles = [value for name in ("citation_title", "prism.title", "dc.title", "dcterms.title")
              for value in parser.metadata.get(name, [])]
    if not actual_dois and not titles:
        titles = [_plain(node) for node in _walk(parser.root) if node.tag == "h1"]
    if title and titles and _normalize(title) not in {_normalize(value) for value in titles}:
        raise ValueError("HTML title does not match the requested paper")


def citation_pdf_urls(raw, expected=None):
    """Return scholarly PDF metadata links after available identity checks."""
    parser = _ArticleParser(raw)
    _identity(parser, expected)
    return list(dict.fromkeys(parser.metadata.get("citation_pdf_url", [])))


def article_text(raw, expected=None):
    """Return complete static article paragraphs/headings, or raise ValueError.

    expected optionally supplies doi/title (or an OpenAlex work object).
    Abstract-only landing pages and access challenges never count as full text.
    """
    parser = _ArticleParser(raw)
    _identity(parser, expected)
    candidates = []
    for node in _walk(parser.root):
        markers = [_normalize(value) for name in ("id", "class", "itemprop")
                   for value in (node.attrs.get(name) or "").split()]
        if node.tag == "article" or any(any(marker.endswith(body) for body in BODY_MARKERS) for marker in markers):
            candidates.append(node)
    for node in candidates:
        blocks = list(_blocks(node))
        section = False
        substantive = False
        for tag, text in blocks:
            if tag in {"h2", "h3", "h4", "h5", "h6"}:
                section = bool(SCIENCE.search(text))
            elif section and text and not ACCESS_PROMPT.search(text):
                substantive = True
        text = "\n\n".join(value for _, value in blocks)
        if substantive and text and not ACCESS_PROMPT.search(text):
            return text
    raise ValueError("HTML has no accessible full article sections beyond the abstract")


def public_url(url, resolver=socket.getaddrinfo):
    """Accept public HTTP(S) only, including every redirect destination."""
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("Invalid content URL")
    if any(key and key in unquote(url) for key in
           (os.environ.get("OPENALEX_API_KEY"), os.environ.get("DEEPSEEK_API_KEY"),
            os.environ.get("MUSE_API_KEY"))):
        raise ValueError("A source credential cannot appear in a public URL")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
        raise ValueError("Invalid content URL")
    if parsed.hostname in ("api.openalex.org", "content.openalex.org"):
        raise ValueError("Remote OpenAlex full-paper downloads are disabled")
    addresses = resolver(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                         type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Content URL must resolve to public addresses")
    return url


def content_kind(raw):
    if raw[:1024].lstrip().startswith(b"%PDF-") and b"%%EOF" in raw[-4096:]:
        return "application/pdf"
    if raw.lstrip().startswith(b"<"):
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError:
            return None
        if root.tag.rsplit("}", 1)[-1].lower() == "html":
            return None
        tags = {element.tag.rsplit("}", 1)[-1].lower() for element in root.iter()}
        body = any(element.tag.rsplit("}", 1)[-1].lower() == "body" and
                   " ".join(element.itertext()).strip() for element in root.iter())
        tei_text = any(element.tag.rsplit("}", 1)[-1].lower() == "text" and
                       " ".join(element.itertext()).strip() for element in root.iter())
        if ({"article", "body"} <= tags and body) or ({"tei", "text"} <= tags and tei_text):
            return "application/xml"
    return None


class _PaperLinks(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.metadata, self.links = {}, []
        self.feed(raw.decode("utf-8", errors="replace"))

    def handle_starttag(self, tag, attrs):
        values = {key: value or "" for key, value in attrs}
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            self.metadata.setdefault(name, []).append(values.get("content", ""))
        href = values.get("href", "")
        if tag in ("a", "link") and (re.search(r"\.pdf(?:[?#]|$)", unquote(href), re.I)
                or values.get("type", "").casefold() == "application/pdf"):
            self.links.append(href)


def repository_pdf_urls(raw, expected):
    """Discover real PDF anchors only on an identified paper's landing page."""
    parser = _PaperLinks(raw)
    expected = expected or {}
    doi = normalized_doi(expected.get("doi"))
    actual_dois = {normalized_doi(match[0]) for key in ("citation_doi", "prism.doi", "dc.identifier", "eprints.id_number")
                  for value in parser.metadata.get(key, [])
                  for match in re.finditer(r"10\.[0-9]{4,9}/[^\s<>\"]+", unquote(value), re.I)}
    normalize = lambda value: re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())
    title = normalize(expected.get("title") or expected.get("display_name") or "")
    titles = {normalize(value) for key in ("citation_title", "dc.title", "dcterms.title", "eprints.title", "prism.title", "og:title")
              for value in parser.metadata.get(key, [])}
    if (doi and actual_dois and doi not in actual_dois) or (title and titles and title not in titles):
        return []
    if not ((doi and doi in actual_dois) or (title and title in titles)):
        return []
    return list(dict.fromkeys(parser.links))


def pdf_matches_work(raw, expected):
    """Check newly discovered PDFs against their actual first-page identity."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:2])
        normalize = lambda value: re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())
        title = normalize(expected.get("title") or expected.get("display_name") or "")
        if not title or title not in normalize(text):
            return False
        doi = normalized_doi(expected.get("doi"))
        actual_dois = {normalized_doi(match[0].rstrip("."))
                      for match in re.finditer(r"10\.[0-9]{4,9}/[^\s<>\"]+", text, re.I)}
        return not (doi and actual_dois and doi not in actual_dois)
    except Exception:
        return False


def _validate_paper_copy(raw, kind, expected):
    """Validate before selecting a copy, including PDFs found on HTML landings."""
    if kind == "application/pdf":
        text = _pdf_text(raw)[0]
    elif kind in ("application/xml", "text/xml", "application/tei+xml"):
        text = _xml_text(raw)
    elif kind == "text/html":
        text = article_text(raw, expected)
    else:
        raise ValueError("Unsupported paper body")
    if PREVIEW_MARKER.search(text):
        raise ValueError("Paper copy is a subscription preview")
    if len(text.strip()) < 500:
        raise ValueError("Too little source text for paper investigation")


def public_download(client, url, resolver, expected=None, discover_pdf=True):
    for _ in range(6):
        public_url(url, resolver)
        # Explicitly empty credentials. Redirects are checked before following;
        # they never change the provider client's redirect/retry behavior.
        with client.stream("GET", url, headers={"User-Agent": "Canary/0.1 paper retrieval"},
                           timeout=PUBLIC_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Missing content redirect")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise ValueError("Content unavailable")
            raw = response.read()
            kind = content_kind(raw)
            if kind is None:
                linked_timeout = None
                # Metadata-listed OA landing pages can expose a scholarly PDF
                # link or the full article itself; an abstract is insufficient.
                if discover_pdf:
                    explicit = citation_pdf_urls(raw, expected)
                    discovered = repository_pdf_urls(raw, expected)
                    for link in dict.fromkeys([*explicit, *discovered]):
                        try:
                            result = public_download(client, urljoin(url, link), resolver, expected, False)
                            if link not in explicit and (result[1] != "application/pdf" or not pdf_matches_work(result[0], expected)):
                                continue
                            return result
                        except httpx.TimeoutException as exc:
                            linked_timeout = exc
                            continue
                        except (httpx.HTTPError, ValueError, OSError, AccessFailure):
                            continue
                try:
                    _validate_paper_copy(raw, "text/html", expected)
                except ValueError:
                    if linked_timeout is not None:
                        raise linked_timeout
                    raise
                kind = "text/html"
            else:
                _validate_paper_copy(raw, kind, expected)
            return raw, kind, url
    raise ValueError("Too many content redirects")


def pmc_identifier(value, explicit=False):
    """Recover a PMCID only from its external-ID field or a PMC repository identity."""
    if not isinstance(value, str):
        return None
    value = unquote(value).strip()
    match = re.fullmatch(r"(?:pmh:)?oai:pubmedcentral\.nih\.gov:(?:PMC)?([1-9][0-9]*)", value, re.I)
    if match:
        return "PMC" + match[1]
    if explicit:
        match = re.fullmatch(r"(?:PMC)?([1-9][0-9]*)", value, re.I)
        if match:
            return "PMC" + match[1]
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return None
    host = parsed.hostname
    pattern = None
    if host in ("www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"):
        pattern = r"/pmc/articles/(?:PMC)?([1-9][0-9]*)(?:/.*)?"
    elif host == "pmc.ncbi.nlm.nih.gov":
        pattern = r"/articles/(?:PMC)?([1-9][0-9]*)(?:/.*)?"
    elif host in ("europepmc.org", "www.europepmc.org"):
        pattern = r"/(?:articles/|article/PMC/)(PMC[1-9][0-9]*)(?:/.*)?"
    match = re.fullmatch(pattern, parsed.path, re.I) if pattern else None
    if match:
        return "PMC" + match[1].upper().removeprefix("PMC")
    return None


def raw_pdf_candidates(work):
    """Legacy location hints authorize only literal HTTP(S) PDF candidates.

    These are weaker than a pdf_url field, so public_work_download also verifies
    the returned PDF's paper identity before accepting one.
    """
    locations = [work.get("best_oa_location"), work.get("primary_location"), *(work.get("locations") or [])]
    urls = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        value = location.get("raw_source_name")
        if not isinstance(value, str) or value != value.strip() or any(c.isspace() for c in value):
            continue
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
                and parsed.path.casefold().endswith(".pdf")):
            urls.append(value)
    return list(dict.fromkeys(urls))


def public_candidates(work):
    locations = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    primary = work.get("primary_location")
    remaining = ([primary] if isinstance(primary, dict) else []) + [
        location for location in (work.get("locations") or []) if isinstance(location, dict)]
    locations.extend(location for location in remaining if location.get("is_oa") is True)
    others = [location for location in remaining if location.get("is_oa") is not True]
    urls = [location["pdf_url"] for location in locations
            if isinstance(location.get("pdf_url"), str) and location["pdf_url"]]
    access = work.get("open_access")
    if isinstance(access, dict) and isinstance(access.get("oa_url"), str) and access["oa_url"]:
        urls.append(access["oa_url"])
    # Location identities also authorize canonical repository copies when the
    # external pmcid field is missing or a legacy landing URL is obsolete.
    ids = work.get("ids")
    pmcids = [pmc_identifier(ids.get("pmcid"), explicit=True)] if isinstance(ids, dict) else []
    for location in locations + others:
        pmcids.extend(pmc_identifier(location.get(key)) for key in ("id", "pdf_url", "landing_page_url"))
    canonical = []
    for pmcid in dict.fromkeys(identifier for identifier in pmcids if identifier):
        canonical.extend(["https://europepmc.org/api/getPdf?pmcid=" + pmcid,
                          "https://europepmc.org/articles/" + pmcid + "?pdf=render",
                          "https://www.ebi.ac.uk/europepmc/webservices/rest/" + pmcid + "/fullTextXML",
                          "https://pmc.ncbi.nlm.nih.gov/articles/" + pmcid + "/",
                          "https://europepmc.org/articles/" + pmcid])
    urls = canonical + urls
    for location in locations:
        landing = location.get("landing_page_url", "")
        if isinstance(landing, str) and landing:
            urls.append(landing)
    # OA metadata is a preference, not proof of current access. Try remaining
    # work-authorized locations too; actual downloaded content decides admission.
    for location in others:
        for key in ("pdf_url", "landing_page_url"):
            url = location.get(key)
            if isinstance(url, str) and url:
                urls.append(url)
    # Authoritative arXiv IDs and abstract URLs identify the same free PDF.
    arxiv_ids = [ids.get("arxiv", "")] if isinstance(ids, dict) else []
    for location in locations + others:
        value = location.get("id", "")
        if isinstance(value, str) and value.casefold().startswith("pmh:oai:arxiv.org:"):
            arxiv_ids.append(value.split(":", 3)[-1])
    for url in list(urls):
        parsed = urlsplit(url)
        if parsed.hostname in ("arxiv.org", "www.arxiv.org") and parsed.path.startswith("/abs/"):
            arxiv_ids.append(parsed.path[5:])
    for identifier in arxiv_ids:
        if isinstance(identifier, str) and re.fullmatch(
                r"(?:[0-9]{4}\.[0-9]{4,5}|[a-zA-Z.-]+/[0-9]{7})(?:v[0-9]+)?", identifier):
            urls.insert(0, "https://arxiv.org/pdf/" + identifier)
    return list(dict.fromkeys([*urls, *raw_pdf_candidates(work)]))


def normalized_doi(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", unquote(value.strip()), flags=re.I)
    return value.casefold() if re.fullmatch(r"10\.[0-9]{4,9}/[^\s]+", value) else ""


def free_metadata_candidates(work, client, resolver, provider):
    """One free exact-DOI lookup; never broaden the research question."""
    doi = normalized_doi(work.get("doi"))
    if not doi:
        return []

    try:
        if provider == "crossref":
            url = "https://api.crossref.org/works/" + quote(doi, safe="")
            params = None
        else:
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            literal = doi.replace("\\", "\\\\").replace('"', '\\"')
            params = {"query": 'DOI:"' + literal + '"', "format": "json", "resultType": "core"}
        public_url(url, resolver)
        response = client.get(url, params=params, headers={"User-Agent": "Canary/0.1 paper retrieval"},
                              timeout=10, follow_redirects=False)
        if response.status_code != 200:
            return []
        data = response.json()
        if provider == "crossref":
            record = data.get("message", {})
            if not isinstance(record, dict) or normalized_doi(record.get("DOI")) != doi:
                return []
            return [link["URL"] for link in record.get("link", []) if isinstance(link, dict)
                    and isinstance(link.get("URL"), str) and link.get("content-type") in
                    (None, "unspecified", "application/pdf", "application/xml", "text/xml", "text/html")]
        urls = []
        for record in data.get("resultList", {}).get("result", []):
            if not isinstance(record, dict) or normalized_doi(record.get("doi")) != doi:
                continue
            if pmc_identifier(record.get("pmcid"), explicit=True):
                urls.extend(public_candidates({"ids": {"pmcid": record["pmcid"]}}))
            for link in (record.get("fullTextUrlList") or {}).get("fullTextUrl", []):
                if (isinstance(link, dict) and isinstance(link.get("url"), str)
                        and str(link.get("availability", "")).casefold() in ("free", "open access")
                        and str(link.get("documentStyle", "")).casefold() in ("pdf", "xml", "html")):
                    urls.append(link["url"])
        return urls
    except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError, AttributeError):
        return []


class _BorrowedPublicTransport(httpx.BaseTransport):
    """An injected server transport outlives each candidate's private client."""
    def __init__(self, transport):
        self.transport = transport

    def handle_request(self, request):
        return self.transport.handle_request(request)


def public_work_download(work, transport, resolver):
    """Race three free copies plus DOI metadata; only the caller saves a winner."""
    def client():
        return httpx.Client(trust_env=False, follow_redirects=False, verify=PUBLIC_SSL_CONTEXT,
                            transport=_BorrowedPublicTransport(transport) if transport else None)

    raw_hints = set(raw_pdf_candidates(work))

    def download(url):
        with client() as public:
            try:
                result = public_download(public, url, resolver, work)
                if url in raw_hints and (result[1] != "application/pdf" or not pdf_matches_work(result[0], work)):
                    return None, False
                # public_download validates each linked/direct copy before
                # returning; do not extract a healthy winner a second time here.
                return result, False
            except httpx.TimeoutException:
                return None, True
            except (httpx.HTTPError, ValueError, OSError, AccessFailure):
                return None, False

    def metadata(provider):
        with client() as public:
            return free_metadata_candidates(work, public, resolver, provider)

    pending, tried, urls = {}, set(), deque(public_candidates(work))
    timed_out = False

    def fill():
        while urls and sum(kind == "paper" for kind in pending.values()) < 3:
            url = urls.popleft()
            if url not in tried:
                tried.add(url)
                pending[_PUBLIC_DOWNLOAD_POOL.submit(download, url)] = "paper"

    try:
        fill()
        if normalized_doi(work.get("doi")):
            for provider in ("crossref", "europepmc"):
                pending[_PUBLIC_METADATA_POOL.submit(metadata, provider)] = "metadata"
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                kind = pending.pop(future)
                if kind == "metadata":
                    urls.extend(future.result())
                else:
                    result, timeout = future.result()
                    timed_out |= timeout
                    if result is not None:
                        return result, timed_out
            fill()
        return None, timed_out
    finally:
        # Do not wait for slow losing requests. Each running task owns and closes
        # its client; it cannot mutate the cache or the chosen response.
        for future in pending:
            future.cancel()


class AccessFailure(RuntimeError):
    pass


def _readable_unicode(text: str) -> str:
    """Keep unknown PDF/metadata glyphs visible; never guess missing characters."""
    return re.sub(r"[\ud800-\udfff]", "\ufffd", text)


def _native_pdf_text(raw: bytes) -> tuple[str, int, tuple[int, ...]] | None:
    executable = shutil.which("pdftotext")
    if executable is None:
        return None
    try:
        output = subprocess.run([executable, "-layout", "-enc", "UTF-8", "-eol", "unix", "-", "-"],
                                input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None  # Retain the existing reader when the native tool cannot read this file.
    pages = _readable_unicode(output.decode("utf-8", errors="replace")).split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()  # A trailing separator is not an extra PDF page; blank interior pages remain.
    blank = tuple(number for number, text in enumerate(pages, 1) if not text.strip())
    if len(blank) == len(pages):
        raise AccessFailure("PDF has no extractable text; OCR is required")
    # Preserve table columns and source line layout, including all original pages.
    text = "\n\n".join(f"[PDF page {number}]\n\n{body.rstrip()}" for number, body in enumerate(pages, 1))
    return text, len(pages), blank


def _pdf_text(raw: bytes) -> tuple[str, int, tuple[int, ...]]:
    if not raw.lstrip().startswith(b"%PDF"):
        raise AccessFailure("download was not a PDF")
    native = _native_pdf_text(raw)
    if native is not None:
        return native
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages, blank = [], []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            # Join typographic line wraps while keeping paragraphs/page boundaries.
            text = re.sub(r"(\w)-\n(?=\w)", r"\1", text)
            text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
            text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:
                blank.append(number)
            pages.append(f"[PDF page {number}]\n\n{text}")
        if not any((p.split("\n\n", 1)[-1].strip() for p in pages)):
            raise AccessFailure("PDF has no extractable text; OCR is required")
        return _readable_unicode("\n\n".join(pages)), len(pages), tuple(blank)
    except AccessFailure:
        raise
    except Exception:
        raise AccessFailure("PDF text could not be extracted completely") from None


def _xml_text(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
        tag = lambda element: element.tag.rsplit("}", 1)[-1].lower()
        article = next((element for element in root.iter() if tag(element) in ("article", "tei")), root)
        namespace = article.tag.rsplit("}", 1)[0] + "}" if article.tag.startswith("{") else ""
        # JATS owns its body directly; TEI owns it through its document-level text.
        # A descendant SVG text element is a figure label, never the paper container.
        container = article.find(f"{namespace}text") if tag(article) == "tei" else article
        body = container.find(f"{namespace}body") if container is not None else None
        if body is None and tag(article) == "tei":
            body = container
        if body is None or not "".join(body.itertext()).strip():
            raise AccessFailure("XML has no readable article body")
        blocks = {"p", "title", "head", "sec", "div", "table", "tr", "fig", "ref",
                  "ref-list", "biblstruct", "list", "list-item", "disp-formula", "formula"}
        def section_text(section):
            pieces = []
            def walk(element):
                if tag(element) in ("tr", "row"):
                    cells = [child for child in element if tag(child) in ("td", "th", "cell")]
                    if cells:
                        pieces.append("\n\n")
                        pieces.append(" | ".join(" ".join(section_text(cell).split()) for cell in cells))
                        pieces.append("\n\n")
                        return
                if tag(element) in blocks:
                    pieces.append("\n\n")
                if element.text:
                    pieces.append(element.text)
                for child in element:
                    walk(child)
                    if child.tail:
                        pieces.append(child.tail)
                if tag(element) in blocks:
                    pieces.append("\n\n")
            walk(section)
            return "\n\n".join(" ".join(p.split()) for p in "".join(pieces).split("\n\n") if p.strip())
        included = {id(element) for element in body.iter()}
        sections = [element for element in article.iter() if tag(element) == "abstract" and id(element) not in included]
        sections += [body]
        sections += [element for element in article.iter() if tag(element) == "back" and id(element) not in included]
        return "\n\n".join(section_text(section) for section in sections)
    except ET.ParseError:
        raise AccessFailure("article XML could not be parsed") from None

