"""Acquire and parse one fresh source artifact without touching the corpus.

This helper is for bounded replacement preparation. It deliberately keeps the
raw downloaded bytes, derived source text, and provenance together in a private
candidate directory; callers decide separately whether a parsed source is
complete enough to prepare for publication.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ast
import gzip
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import tempfile
import tarfile
import io
import time
import zlib
from urllib.parse import urlsplit


class FreshSourceError(ValueError):
    """Fresh source failed identity, transport, or parse validation."""


def _http_failure(message, response):
    error = FreshSourceError(message)
    error.status_code = getattr(response, 'status_code', None)
    error.headers = getattr(response, 'headers', {})
    return error


class FreshSourceDependencyError(FreshSourceError):
    """A shared local dependency or configured store is unavailable."""


_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5})(?:v([1-9]\d*))?$")
_ACL_ID = re.compile(r"^acl:([A-Za-z0-9][A-Za-z0-9._/-]*)$")
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_USER_AGENT = "DTOX-Research source recovery (contact: dtox-research)"


@dataclass(frozen=True)
class FreshArtifact:
    canonical_id: str
    source_url: str
    request_url: str
    retrieval_url: str
    original_kind: str
    edition: str
    retrieved_at: str
    raw_sha256: str
    raw_size_bytes: int
    raw_bytes: bytes


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _url_parts(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ValueError
        parsed.port
        return parsed
    except (TypeError, ValueError) as exc:
        raise FreshSourceError("source URL must be a credential-free HTTPS URL") from exc


def expected_source(canonical_id, original_kind, edition):
    """Derive and validate the exact public URL for a canonical article ID."""
    acl = _ACL_ID.fullmatch(canonical_id or "")
    if acl:
        if original_kind != "PDF" or edition != canonical_id:
            raise FreshSourceError("ACL PDF edition must equal its canonical proceedings ID")
        ident = acl.group(1)
        if ".." in ident.split("/"):
            raise FreshSourceError("invalid ACL canonical ID")
        return f"https://aclanthology.org/{ident}.pdf"

    arxiv = _ARXIV_ID.fullmatch(canonical_id or "")
    if not arxiv:
        raise FreshSourceError("unsupported canonical paper ID")
    bare, id_version = arxiv.groups()
    if not re.fullmatch(r"v[1-9]\d*", edition or ""):
        raise FreshSourceError("arXiv edition must be an explicit version such as v1")
    version = edition[1:]
    if id_version and id_version != version:
        raise FreshSourceError("canonical ID version and edition disagree")
    suffix = f"{bare}v{version}"
    if original_kind == "HTML":
        return f"https://arxiv.org/html/{suffix}"
    if original_kind == "rawTexarchive":
        return f"https://arxiv.org/e-print/{suffix}"
    if original_kind == "PDF":
        return f"https://arxiv.org/pdf/{suffix}"
    raise FreshSourceError("unsupported arXiv source kind")


def _response_bytes(response, limit):
    if getattr(response, "status_code", None) != 200:
        raise _http_failure(f"source request returned HTTP {getattr(response, 'status_code', None)}", response)
    chunks = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    parts = iterator(1024 * 1024) if callable(iterator) else (response.content,)
    for part in parts:
        if not part:
            continue
        total += len(part)
        if total > limit:
            raise FreshSourceError("source exceeds byte limit")
        chunks.append(part)
    return b"".join(chunks)


def fetch_fresh_artifact(canonical_id, *, original_kind, edition, session=None,
                         timeout=90, max_bytes=_MAX_SOURCE_BYTES, retrieved_at=None,
                         source_url=None):
    """GET an exact source URL derived from ID/kind/edition and seal its bytes."""
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    try:
        expected = expected_source(canonical_id, original_kind, edition)
    except FreshSourceError:
        if original_kind != "PDF" or not isinstance(source_url, str):
            raise
        _url_parts(source_url)
        expected = source_url
    if source_url is not None:
        _url_parts(source_url)
        if source_url != expected:
            raise FreshSourceError("provided source URL conflicts with canonical ID/edition route")
    source_url = expected
    parsed_source = _url_parts(source_url)
    if session is None:
        import requests
        session = requests.Session()
    response = session.get(source_url, timeout=timeout, headers={"User-Agent": _USER_AGENT},
                           allow_redirects=True, stream=True)
    try:
        retrieval_url = getattr(response, "url", source_url)
        parsed_final = _url_parts(retrieval_url)
        if parsed_final.hostname != parsed_source.hostname:
            raise FreshSourceError("source redirected to a different host")
        raw = _response_bytes(response, max_bytes)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    if original_kind == "PDF" and not raw.startswith(b"%PDF-"):
        raise FreshSourceError("response is not a PDF")
    if original_kind == "HTML":
        try:
            decoded = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FreshSourceError("HTML source is not valid UTF-8") from exc
        store_header = raw.lstrip(b"\xef\xbb\xbf \r\n\t").lower()
        if not store_header.startswith((b"<!doctype html", b"<html")):
            raise FreshSourceError("response is not raw HTML accepted by ORIGINAL store")
    if original_kind == "rawTexarchive" and not (raw.startswith(b"\x1f\x8b") or raw.startswith(b"%PDF-")):
        raise FreshSourceError("response is not a supported arXiv source archive")

    raw_sha256 = hashlib.sha256(raw).hexdigest()
    artifact_edition = edition if _ARXIV_ID.fullmatch(canonical_id or "") else "sha256:" + raw_sha256
    return FreshArtifact(
        canonical_id=canonical_id, source_url=source_url, request_url=source_url,
        retrieval_url=retrieval_url, original_kind=original_kind, edition=artifact_edition,
        retrieved_at=retrieved_at or _utc_now(), raw_sha256=raw_sha256,
        raw_size_bytes=len(raw), raw_bytes=raw,
    )


def resolve_arxiv_version(canonical_id, *, session=None, timeout=30):
    """Resolve the current arXiv edition from its public Atom API, with title evidence."""
    started = time.perf_counter()
    match = _ARXIV_ID.fullmatch(canonical_id or "")
    if not match:
        raise FreshSourceError("arXiv canonical ID required for version resolution")
    bare, explicit = match.groups()
    if explicit:
        return {"fresh_version": "v" + explicit, "title": None,
                "resolver_url": None, "resolved_at": _utc_now(),
                "resolution": "explicit_version_in_canonical_id", "resolver_seconds": 0.0}
    if session is None:
        import requests
        session = requests.Session()
    resolver_url = "https://export.arxiv.org/api/query?id_list=" + bare
    try:
        response = session.get(resolver_url, timeout=timeout,
                               headers={"User-Agent": _USER_AGENT}, allow_redirects=True)
        try:
            if getattr(response, "status_code", None) != 200:
                raise _http_failure("arXiv version resolver returned non-200", response)
            final = _url_parts(getattr(response, "url", resolver_url))
            if final.hostname != "export.arxiv.org":
                raise FreshSourceError("arXiv version resolver redirected off host")
            body = _response_bytes(response, 2 * 1024 * 1024)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        # The export API can be unavailable while the publisher's abstract page
        # remains live. Restrict fallback to transport failures and its explicit
        # non-200 response; identity/parse failures below remain hard errors.
        import requests
        if not isinstance(exc, requests.RequestException) and not (
                isinstance(exc, FreshSourceError) and "non-200" in str(exc)):
            raise
        return _resolve_arxiv_abs_version(
            bare, session=session, timeout=timeout, started=started,
            fallback_reason="export_api_unavailable")
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FreshSourceError("arXiv version resolver returned invalid Atom XML") from exc
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    matches = []
    for entry in entries:
        raw_id = (entry.findtext("{http://www.w3.org/2005/Atom}id") or "").strip()
        found = re.search(r"/abs/" + re.escape(bare) + r"v([1-9]\d*)$", raw_id)
        if found:
            title = " ".join((entry.findtext("{http://www.w3.org/2005/Atom}title") or "").split())
            matches.append((int(found.group(1)), title))
    if len(matches) != 1 or not matches[0][1]:
        raise FreshSourceError("arXiv resolver did not return one exact ID with title")
    version, title = matches[0]
    return {"fresh_version": "v" + str(version), "title": title,
            "resolver_url": resolver_url, "resolved_at": _utc_now(),
            "resolution": "public_atom_exact_id",
            "resolver_seconds": round(time.perf_counter() - started, 6)}


def _resolve_arxiv_abs_version(bare, *, session, timeout, started, fallback_reason):
    """Read exact latest edition/title from the publisher abstract page."""
    from html.parser import HTMLParser

    url = "https://arxiv.org/abs/" + bare
    response = session.get(url, timeout=timeout,
                           headers={"User-Agent": _USER_AGENT}, allow_redirects=True)
    try:
        if getattr(response, "status_code", None) != 200:
            raise _http_failure("arXiv abstract-page version resolver returned non-200", response)
        final = _url_parts(getattr(response, "url", url))
        if final.hostname != "arxiv.org":
            raise FreshSourceError("arXiv abstract-page resolver redirected off host")
        body = _response_bytes(response, 2 * 1024 * 1024)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    try:
        page = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FreshSourceError("arXiv abstract page is not valid UTF-8") from exc

    class _Metadata(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.title = None
            self.dateline = []
            self.breadcrumb = []
            self.history = []
            self._in_dateline = False
            self._in_breadcrumb = False
            self._in_history = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag.lower() == "meta" and attrs.get("name", "").lower() == "citation_title":
                self.title = attrs.get("content", "").strip() or self.title
            if tag.lower() == "div" and "dateline" in attrs.get("class", "").split():
                self._in_dateline = True
            if tag.lower() == "div" and {"header-breadcrumbs", "header-breadcrumbs-mobile"}.intersection(attrs.get("class", "").split()):
                self._in_breadcrumb = True
            if tag.lower() == "div" and "submission-history" in attrs.get("class", "").split():
                self._in_history = True

        def handle_endtag(self, tag):
            if tag.lower() == "div" and self._in_dateline:
                self._in_dateline = False
            if tag.lower() == "div" and self._in_breadcrumb:
                self._in_breadcrumb = False
            if tag.lower() == "div" and self._in_history:
                self._in_history = False

        def handle_data(self, data):
            if self._in_dateline:
                self.dateline.append(data)
            if self._in_breadcrumb:
                self.breadcrumb.append(data)
            if self._in_history:
                self.history.append(data)

    parsed = _Metadata()
    parsed.feed(page)
    # Bind identity to the visible page breadcrumb, not related-paper links or
    # arbitrary occurrences elsewhere in the HTML.
    breadcrumb = " ".join(" ".join(parsed.breadcrumb).split())
    id_witness = re.search(r"\barXiv:" + re.escape(bare) + r"(?:v[1-9]\d*)?\b", breadcrumb)
    data = " ".join(" ".join(parsed.dateline).split())
    latest = re.search(r"this version,\s*v([1-9]\d*)", data, flags=re.I)
    if not latest and not re.search(r"\blast revised\b", data, flags=re.I):
        latest = re.search(r"Submitted on[^\]]*\(\s*v([1-9]\d*)\s*\)\s*\]", data, flags=re.I)
    history_versions = [int(v) for v in re.findall(r"\[v([1-9]\d*)\]", " ".join(parsed.history))]
    version = int(latest.group(1)) if latest else None
    if history_versions:
        history_latest = max(history_versions)
        if version is not None and version != history_latest:
            raise FreshSourceError("arXiv dateline and submission history versions disagree")
        if version is None and re.search(r"\bSubmitted on\b", data, flags=re.I):
            version = history_latest
    if not id_witness or version is None or not parsed.title:
        raise FreshSourceError("arXiv abstract page lacks exact ID, version-history, or title evidence")
    return {"fresh_version": "v" + str(version), "title": " ".join(parsed.title.split()),
            "resolver_url": url, "resolved_at": _utc_now(),
            "resolution": "publisher_abs_exact_id_version_history",
            "fallback_reason": fallback_reason,
            "resolver_seconds": round(time.perf_counter() - started, 6)}


def _repo_root():
    configured = os.environ.get("DTOX_RESEARCH_ROOT")
    if configured and Path(configured).is_dir():
        return Path(configured).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pipeline" / "extractor.py").is_file():
            return parent
    deployed = Path("/opt/dtox-corpus-quality-20261013")
    if (deployed / "pipeline" / "extractor.py").is_file():
        return deployed
    raise FreshSourceDependencyError("research source checkout is unavailable")


def _pipeline_modules():
    import sys
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from pipeline import extractor, structured_html
    except (ImportError, OSError) as exc:
        raise FreshSourceDependencyError("existing extraction adapters are unavailable") from exc
    return extractor, structured_html


def _parse_pdf(artifact, timeout, native_build=None):
    extractor, _ = _pipeline_modules()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as stream:
        path = Path(stream.name)
        stream.write(artifact.raw_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        text, conversion = extractor.convert_pdf_isolated(
            str(path), timeout=timeout, native_build=native_build,
            normalize_unicode=True, return_diagnostics=True)
    except extractor.PDFConversionError as exc:
        if exc.reason == "conversion_timeout":
            raise FreshSourceError("PDF conversion timed out for this source") from exc
        raise
    finally:
        path.unlink(missing_ok=True)
    normalization = conversion.get("unicode_normalization") or {}
    unresolved = normalization.get("unresolved", [])
    diagnostics = {"parser": "pymupdf4llm-isolated", "parse_status": "ok",
                  "page_count": conversion.get("page_count"),
                  "page_text_chars": conversion.get("page_text_chars"),
                  "markdown_chars": conversion.get("markdown_chars"),
                  "markdown_replacement_characters": conversion.get("markdown_replacement_characters"),
                  "unicode_normalization": normalization,
                  "unsupported_structures": [],
                  "unresolved_unicode_mappings": unresolved}
    if conversion.get("page_count", 0) <= 0 or conversion.get("page_text_chars", 0) <= 0:
        raise FreshSourceError("PDF conversion has no page-level text coverage")
    if unresolved:
        diagnostics["parse_status"] = "warning"
    return text, diagnostics


def _html_search_text(ir, expected_title=None):
    """Render one exact `<article>` body, retaining equations and source table tags."""
    from copy import deepcopy
    _, structured_html = _pipeline_modules()
    candidate = deepcopy(ir)
    raw = candidate.get("_raw_text", "")
    openings = list(re.finditer(r"<article\b[^>]*>", raw, re.I))
    closings = list(re.finditer(r"</article\s*>", raw, re.I))
    if len(openings) != 1 or len(closings) != 1 or closings[0].start() <= openings[0].end():
        raise FreshSourceError("HTML does not contain one bounded article element")
    article_start, article_end = openings[0].end(), closings[0].start()
    article_blocks = [block for block in candidate.get("blocks", [])
                      if block.get("source_span", {}).get("start", -1) >= article_start
                      and block.get("source_span", {}).get("end", len(raw) + 1) <= article_end]
    if not article_blocks:
        raise FreshSourceError("HTML article boundary contains no structured text blocks")
    headings = [block for block in article_blocks if block.get("kind") == "heading"]
    title_matches = [block for block in headings if expected_title and
                     _normalize_title(block.get("text", "")) == _normalize_title(expected_title)]
    if expected_title and len(title_matches) != 1:
        raise FreshSourceError("HTML article heading does not uniquely match expected title")
    if not headings:
        raise FreshSourceError("HTML article has no source headings")

    math_alttext, warnings, used_math, used_tables, used_equation_layouts = [], [], set(), set(), set()
    math_records = {item["id"]: item for item in candidate.get("math", [])
                    if item.get("context", {}).get("block_id") in {b["id"] for b in article_blocks}}
    article_table_records = [item for item in candidate.get("tables", [])
                             if item.get("context", {}).get("block_id") in {b["id"] for b in article_blocks}]
    def is_equation_layout(record):
        match = re.search(r"\bclass\s*=\s*['\"]([^'\"]*)", record.get("raw_html", ""), re.I)
        classes = set(match.group(1).split()) if match else set()
        return {"ltx_equation", "ltx_eqn_table"}.issubset(classes)
    equation_layout_records = {item["id"]: item for item in article_table_records if is_equation_layout(item)}
    table_records = {item["id"]: item for item in article_table_records if not is_equation_layout(item)}
    for record in math_records.values():
        annotations = record.get("tex_annotations") or []
        values = list(dict.fromkeys(a.get("value", "").strip() for a in annotations
                                    if a.get("value", "").strip()))
        if len(values) == 1:
            record["_render_value"] = values[0]
        elif not values and isinstance(record.get("alttext"), str) and record["alttext"].strip():
            record["_render_value"] = record["alttext"].strip()
            math_alttext.append(record["id"])
        else:
            raise FreshSourceError("HTML article math lacks unique explicit TeX or source alttext")

    def render_content(items, *, table_cell=False):
        parts = []
        for item in items:
            kind = item.get("kind")
            if kind == "text":
                parts.append(html.escape(item.get("text", ""), quote=False) if table_cell else item.get("text", ""))
            elif kind == "math":
                identity = item["id"]
                record = math_records.get(identity)
                if record is None:
                    raise FreshSourceError("HTML article references math outside its exact article source")
                value = record["_render_value"]
                used_math.add(identity)
                display = bool(re.search(r"\bdisplay\s*=\s*['\"]block['\"]",
                                         record.get("mathml", ""), re.I))
                if display:
                    parts.append("\n\n$$\n" + value + "\n$$\n\n")
                elif value.startswith(("$", r"\(")):
                    parts.append(value)
                else:
                    parts.append("$" + value + "$")
            elif kind == "table":
                parts.append(render_table(item["id"]))
            elif kind == "element":
                tag = item.get("tag")
                if tag in ("img", "svg", "canvas", "object", "iframe"):
                    warnings.append({"code": "figure_asset_not_textual", "alttext": item.get("alttext")})
                    continue
                if tag == "figure":
                    text = render_content(item.get("children", []), table_cell=table_cell).strip()
                    if not text:
                        raise FreshSourceError("HTML article figure has neither caption nor textual description")
                    parts.append(text)
                elif tag == "br":
                    parts.append("\n")
                else:
                    body = render_content(item.get("children", []), table_cell=table_cell)
                    if tag in ("sub", "sup"):
                        parts.append(("_{" if tag == "sub" else "^{") + body + "}")
                    elif tag == "figcaption":
                        parts.append("\n" + body + "\n")
                    else:
                        parts.append(body)
            else:
                raise FreshSourceError("HTML article has an unknown structured content node")
        return "".join(parts)

    def render_table(identity):
        if identity in equation_layout_records:
            record = equation_layout_records[identity]
            grid, cells = record.get("grid"), record.get("cells", [])
            if not grid or not cells or any(cell.get("row_span") != 1 or cell.get("column_span") != 1 for cell in cells):
                raise FreshSourceError("HTML equation layout grid or span structure cannot be preserved")
            by_id = {cell["id"]: cell for cell in cells}
            rendered_rows = []
            for row in grid:
                formulas, labels = [], []
                for cell_id in row:
                    if cell_id not in by_id:
                        raise FreshSourceError("HTML equation layout contains an unbound source cell")
                    cell = by_id[cell_id]
                    content = render_content(cell.get("content", [])).strip()
                    if not content:
                        continue
                    contains_math = any(item.get("kind") == "math" for item in cell.get("content", []))
                    if contains_math:
                        formula = re.fullmatch(r"\$\$\s*\n(.*?)\n\$\$", content, re.S)
                        if not formula:
                            raise FreshSourceError("equation-layout math was not rendered as display TeX")
                        formulas.append(formula.group(1))
                    else:
                        labels.append(content)
                if not formulas:
                    raise FreshSourceError("equation-layout row has no explicit source math")
                rendered = "$$\n" + " & ".join(formulas) + "\n$$"
                if labels:
                    rendered += "\n" + " ".join(labels)
                rendered_rows.append(rendered)
            used_equation_layouts.add(identity)
            return "\n\n".join(rendered_rows)
        record = table_records.get(identity)
        if record is None:
            raise FreshSourceError("HTML article references a table outside its exact article source")
        grid = record.get("grid")
        cells = record.get("cells", [])
        if not grid or not cells:
            raise FreshSourceError("HTML table source grid is unavailable")
        if record.get("unsupported"):
            fatal = [issue for issue in record["unsupported"]
                     if issue not in {"no_explicit_th_headers", "unresolved_cell_headers"}]
            if fatal:
                raise FreshSourceError("HTML table has an unsupported source grid")
            warnings.append({"code": "table_header_semantics_unresolved", "table_id": identity})
        by_id = {cell["id"]: cell for cell in cells}
        if len(by_id) != len(cells):
            raise FreshSourceError("HTML table contains duplicate source cell identities")
        expected_grid = [[None for _ in row] for row in grid]
        def span_values(cell):
            attrs = cell.get("span_attributes") or {}
            spans = []
            for name, resolved in (("rowspan", cell.get("row_span")),
                                   ("colspan", cell.get("column_span"))):
                raw_value = attrs.get(name)
                if resolved is None or type(resolved) is not int or resolved < 1:
                    raise FreshSourceError("HTML table has an invalid source span")
                if raw_value is None:
                    if resolved != 1:
                        raise FreshSourceError("HTML table span is not bound to its source attribute")
                    spans.append(None)
                    continue
                try:
                    source_span = int(raw_value.strip())
                except (AttributeError, ValueError) as exc:
                    raise FreshSourceError("HTML table has a malformed source span") from exc
                if source_span < 0 or (name == "colspan" and source_span == 0):
                    raise FreshSourceError("HTML table has a nonpositive source span")
                if source_span == 0:
                    if name != "rowspan":
                        raise FreshSourceError("zero colspan is not a valid source span")
                elif source_span != resolved:
                    raise FreshSourceError("resolved table span differs from source attribute")
                spans.append(raw_value.strip())
            return spans

        source_rows = [[] for _ in grid]
        for cell in cells:
            row_index, column_index = cell.get("row"), cell.get("column")
            if (type(row_index) is not int or type(column_index) is not int
                    or row_index < 0 or column_index < 0 or row_index >= len(grid)):
                raise FreshSourceError("HTML table cell has invalid source coordinates")
            row_span, column_span = cell.get("row_span"), cell.get("column_span")
            span_values(cell)
            if row_index + row_span > len(grid) or column_index + column_span > len(grid[row_index]):
                raise FreshSourceError("HTML table span exceeds its source grid")
            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    rr, cc = row_index + row_offset, column_index + column_offset
                    if cc >= len(grid[rr]) or expected_grid[rr][cc] is not None:
                        raise FreshSourceError("HTML table source spans overlap or exceed the grid")
                    expected_grid[rr][cc] = cell["id"]
            source_rows[row_index].append(cell)
        if any(list(row) != list(expected) for row, expected in zip(grid, expected_grid)):
            raise FreshSourceError("HTML table IR grid does not match its source cells and spans")
        for row in source_rows:
            row.sort(key=lambda cell: (cell["column"], cell["source_span"]["start"]))
        rows = []
        row_groups = []
        for row_index, row_cells in enumerate(source_rows):
            tags = []
            for cell in row_cells:
                tag = "th" if cell.get("is_header") else "td"
                attrs = []
                if cell.get("is_header") and cell.get("scope") in {"row", "col", "rowgroup", "colgroup"}:
                    attrs.append(' scope="' + cell["scope"] + '"')
                for attr_name, source_value in zip(("rowspan", "colspan"), span_values(cell)):
                    if source_value is not None:
                        attrs.append(' ' + attr_name + '="' + html.escape(source_value, quote=True) + '"')
                body = render_content(cell.get("content", []), table_cell=True).strip()
                tags.append("<" + tag + "".join(attrs) + ">" + body + "</" + tag + ">")
            rows.append("<tr>" + "".join(tags) + "</tr>")
        groups = []
        for row_index, row_cells in enumerate(source_rows):
            group = (row_cells[0].get("row_group", "tbody") if row_cells else
                     next((by_id[cell_id].get("row_group", "tbody") for cell_id in grid[row_index]
                           if cell_id in by_id), None))
            if group is None:
                raise FreshSourceError("HTML table contains a source row with no row-group binding")
            row_groups.append(group)
            if groups and groups[-1][0] == group:
                groups[-1][1].append(rows[row_index])
            else:
                groups.append((group, [rows[row_index]]))
        prefix = ""
        captions = [render_content(caption, table_cell=True).strip()
                    for caption in record.get("captions", [])]
        if any(captions):
            prefix = "<caption>" + " ".join(c for c in captions if c) + "</caption>"
        body = []
        for group, group_rows in groups:
            tag = group if group in {"thead", "tbody", "tfoot"} else "tbody"
            body.append("<" + tag + ">" + "".join(group_rows) + "</" + tag + ">")
        used_tables.add(identity)
        return "\n<table>" + prefix + "".join(body) + "</table>\n"

    parts = []
    for block in article_blocks:
        body = render_content(block.get("content", [])).strip()
        if not body:
            continue
        if block.get("kind") == "heading":
            span = block.get("source_span", {})
            tag_source = raw[span.get("start", 0):span.get("end", 0)]
            heading = re.search(r"<h([1-6])\b", tag_source, re.I)
            level = int(heading.group(1)) if heading else 2
            parts.append("#" * level + " " + body)
        elif block.get("kind") == "li":
            parts.append("- " + body)
        else:
            parts.append(body)
    if not parts:
        raise FreshSourceError("HTML article body is empty")
    if (used_math != set(math_records) or used_tables != set(table_records)
            or used_equation_layouts != set(equation_layout_records)):
        raise FreshSourceError("HTML article contains math/table structures not represented in derived text")
    text = "\n\n".join(parts)
    body_headings = [h for h in headings if _normalize_title(h.get("text", "")) not in
                     {_normalize_title(expected_title or ""), "abstract", "keywords", "references", "bibliography"}]
    if not body_headings:
        raise FreshSourceError("HTML article has no non-abstract section headings")
    return text, {"text": text, "issues": [], "warnings": warnings,
                  "article_block_count": len(article_blocks),
                  "article_source_chars": article_end - article_start,
                  "article_math_count": len(math_records), "article_math_rendered_count": len(used_math),
                  "article_table_count": len(table_records), "article_tables_rendered_count": len(used_tables),
                  "article_equation_layout_count": len(equation_layout_records),
                  "article_equation_layouts_rendered_count": len(used_equation_layouts),
                  "source_title_match": True, "article_heading_count": len(headings)}, math_alttext


def _load_latex_assembler():
    """Load the existing assembly functions without importing pipeline.service.

    `service.py` opens its runtime log and initializes service resources at
    import time. The AST load keeps those service effects out of source prep
    while executing the same `_is_commented` and `assemble_latex_source` code.
    """
    source_path = _repo_root() / "pipeline" / "service.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names = {"_is_commented", "assemble_latex_source"}
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id in
                           {"_INC_RE", "LATEX_MAX_INCLUDE_DEPTH"} for target in node.targets)]
    found = {node.name for node in functions}
    if found != names or len(assignments) != 2:
        raise FreshSourceError("existing LaTeX assembly contract could not be loaded")
    module = ast.Module(body=assignments + functions, type_ignores=[])
    namespace = {"re": re, "posixpath": posixpath,
                 "log": logging.getLogger("dtox-fresh-source-assembly")}
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)
    return namespace["assemble_latex_source"]


def _parse_tex_archive(artifact):
    assembler = _load_latex_assembler()
    files, warnings = [], []
    logger = logging.getLogger("dtox-fresh-source-assembly")
    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())
    capture = Capture()
    logger.addHandler(capture)
    try:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(artifact.raw_bytes), mode="r:gz")
        except tarfile.ReadError:
            try:
                text = gzip.decompress(artifact.raw_bytes).decode("utf-8", errors="strict")
            except (OSError, EOFError, UnicodeError, zlib.error) as exc:
                raise FreshSourceError("e-print is neither a readable tar archive nor gzip TeX") from exc
            if not ("\\documentclass" in text or "\\section" in text):
                raise FreshSourceError("gzip e-print has no recognizable TeX body")
            # OriginalStore records rawTexarchive only for real tar/zip archives.
            # Preserve the actual e-print bytes; do not wrap this single-file
            # gzip payload in a synthetic archive merely to satisfy that kind.
            raise FreshSourceError(
                "single-file gzip TeX is not an ORIGINAL rawTexarchive; use the PDF source route")
        with archive:
            members = archive.getmembers()
            if len(members) > 10000:
                raise FreshSourceError("e-print archive member limit exceeded")
            total = 0
            for member in members:
                if not member.isfile():
                    continue
                name = member.name.replace("\\", "/")
                parts = name.split("/")
                if name.startswith("/") or ".." in parts or "\x00" in name:
                    raise FreshSourceError("unsafe path in e-print archive")
                is_tex = name.endswith(".tex")
                is_bibliography = name.endswith((".bib", ".bbl"))
                if is_bibliography or (not is_tex and "." not in name.rsplit("/", 1)[-1]):
                    if member.size > 2_000_000:
                        continue
                elif not is_tex:
                    continue
                total += member.size
                if total > 64 * 1024 * 1024:
                    raise FreshSourceError("e-print uncompressed size limit exceeded")
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                try:
                    content = stream.read(2_000_001)
                finally:
                    stream.close()
                if len(content) > 2_000_000:
                    continue
                try:
                    decoded = content.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise FreshSourceError("e-print text member is not valid UTF-8") from exc
                files.append((name, decoded))
        if not any(name.endswith(".tex") for name, _ in files):
            raise FreshSourceError("e-print archive has no TeX source")
        text = assembler(artifact.canonical_id, files)
        diagnostics = {"parser": "pipeline.service.assemble_latex_source",
                       "parse_status": "warning" if warnings else "ok",
                       "archive_member_count": len(files),
                       "unresolved_include_warnings": warnings,
                       "replacement_characters": text.count("\ufffd"),
                       "word_count": len(text.split())}
        return text, diagnostics
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise FreshSourceError("e-print archive is truncated or has invalid compression") from exc
    finally:
        logger.removeHandler(capture)


def _normalize_title(value):
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _validate_candidate_sections(paper_id, sections, source_text, parser_mode, chunks):
    """Reject source sections that the publication-side cache contract cannot accept."""
    import sys
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from pipeline import document_cache
    except (ImportError, OSError) as exc:
        raise FreshSourceDependencyError("existing document-cache validator is unavailable") from exc
    try:
        # Validate the actual candidate with the same cache serialization and
        # unsplit section records that the publication owner will receive.
        cache_bytes = json.dumps(chunks, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
        document_cache.make_section_manifest(
            paper_id, sections, cache_bytes, source_text.encode("utf-8"), parser_mode, 5)
    except document_cache.ManifestInvalid as exc:
        raise FreshSourceError(
            "fresh source sections do not satisfy the canonical document-cache contract") from exc


def parse_fresh_artifact(artifact, *, timeout=120, native_build=None, expected_title=None):
    """Parse a sealed artifact with the existing PDF or structured HTML adapter."""
    if not isinstance(artifact, FreshArtifact):
        raise TypeError("FreshArtifact required")
    if hashlib.sha256(artifact.raw_bytes).hexdigest() != artifact.raw_sha256:
        raise FreshSourceError("artifact raw SHA-256 mismatch")
    if artifact.original_kind == "PDF":
        text, diagnostics = _parse_pdf(artifact, timeout, native_build=native_build)
        diagnostics["markdown_text_chars"] = len(text or "")
        diagnostics["replacement_characters"] = (text or "").count("\ufffd")
        diagnostics["word_count"] = len((text or "").split())
    elif artifact.original_kind == "HTML":
        _, structured_html = _pipeline_modules()
        try:
            ir = structured_html.extract_html(artifact.raw_bytes)
        except structured_html.HTMLLimitError as exc:
            raise FreshSourceError(
                "HTML source exceeds the structured parser limits; try another route for this edition") from exc
        ir["_raw_text"] = artifact.raw_bytes.decode("utf-8", errors="strict")
        arxiv = _ARXIV_ID.fullmatch(artifact.canonical_id)
        if arxiv:
            bare, id_version = arxiv.groups()
            version = artifact.edition[1:]
            if id_version and id_version != version:
                raise FreshSourceError("HTML canonical ID and explicit edition disagree")
            pattern = rf"arXiv\s*:\s*{re.escape(bare)}v{re.escape(version)}\b"
            if not re.search(pattern, ir["_raw_text"], re.I):
                raise FreshSourceError("arXiv HTML header does not confirm requested ID/version")
        text, projection, math_alttext = _html_search_text(ir, expected_title)
        diagnostics = {"parser": "pipeline.structured_html.extract_html+project_search_text",
                       "parse_status": "ok" if not ir["diagnostics"] else "warning",
                       "replacement_characters": text.count("\ufffd"),
                       "word_count": len(text.split()), "blocks": projection["article_block_count"],
                       "headings": projection["article_heading_count"],
                       "tables": projection["article_table_count"], "math": projection["article_math_count"],
                       "math_from_alttext": math_alttext,
                       "unsupported_structures": projection["issues"],
                       "structure_warnings": projection["warnings"],
                       "article_source_chars": projection["article_source_chars"],
                       "article_math_rendered_count": projection["article_math_rendered_count"],
                       "article_tables_rendered_count": projection["article_tables_rendered_count"],
                       "equation_layouts": projection["article_equation_layout_count"],
                       "equation_layouts_rendered_count": projection["article_equation_layouts_rendered_count"],
                       "source_title_match": projection["source_title_match"],
                       "nonabstract_section_count": max(0, projection["article_heading_count"] - 1),
                       "quality_measured": True}
    elif artifact.original_kind == "rawTexarchive":
        text, diagnostics = _parse_tex_archive(artifact)
    else:
        raise FreshSourceError("artifact parser is not supported by this bounded helper")
    if (artifact.original_kind == "PDF"
            and not _ACL_ID.fullmatch(artifact.canonical_id)
            and not _ARXIV_ID.fullmatch(artifact.canonical_id)):
        if not expected_title or len(_normalize_title(expected_title)) < 8:
            raise FreshSourceError("generic PDF source requires a usable expected title")
        title = _normalize_title(expected_title)
        extracted = _normalize_title(text[:12000])
        if title not in extracted:
            raise FreshSourceError("generic PDF first-page text does not match expected title")
    if not isinstance(text, str) or not text.strip():
        raise FreshSourceError("source parser produced no readable body")
    return text, diagnostics


def _write_content_once(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise FreshSourceError("candidate content path already contains different bytes")
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    return True


def _register_original(artifact, raw_path, original_store_root):
    """Append fresh provenance to the existing originals CAS, never a new store."""
    try:
        root = Path(original_store_root).resolve(strict=True)
    except OSError as exc:
        raise FreshSourceDependencyError("configured ORIGINAL store root is unavailable") from exc
    manifest = root / "manifest.sqlite3"
    if not root.is_dir() or not manifest.is_file():
        raise FreshSourceDependencyError("configured ORIGINAL store is not an existing manifest root")
    extractor_root = _repo_root()
    import sys
    if str(extractor_root) not in sys.path:
        sys.path.insert(0, str(extractor_root))
    try:
        from pipeline.originals import OriginalStore, Provenance
        from pipeline.originals import OriginalError
        store = OriginalStore(root)
        provenance_edition = (artifact.edition if re.fullmatch(r"v[1-9]\d*", artifact.edition)
                              else "sha256:" + artifact.raw_sha256)
        provenance = Provenance(
            canonical_id=artifact.canonical_id,
            source_url=artifact.source_url,
            retrieval_url=artifact.retrieval_url,
            original_kind=artifact.original_kind,
            edition=provenance_edition,
            retrieved_at=artifact.retrieved_at,
            raw_sha256=artifact.raw_sha256,
        )
        blob = store.blob_path(artifact.raw_sha256)
        if blob.exists():
            store.add_ref_from_verified_blob(
                provenance, max_bytes=_MAX_SOURCE_BYTES,
                expected_bytes=artifact.raw_size_bytes,
                source_name=raw_path.name,
            )
            storage = "existing_verified_blob_reference"
        else:
            store.ingest_file(raw_path, provenance, max_bytes=_MAX_SOURCE_BYTES)
            storage = "new_blob_and_reference"
    except OriginalError as exc:
        if (artifact.original_kind == "rawTexarchive"
                and str(exc) == "not a raw TeX tar/zip archive"):
            raise FreshSourceError(
                "e-print bytes are not an ORIGINAL rawTexarchive; use the PDF source route") from exc
        raise FreshSourceDependencyError(
            "fresh raw source could not be registered in existing ORIGINAL store") from exc
    except (OSError, ValueError) as exc:
        raise FreshSourceDependencyError("fresh raw source could not be registered in existing ORIGINAL store") from exc
    canonical_blob = store.verify_blob(artifact.raw_sha256, artifact.raw_size_bytes)
    return {"store_root": str(root), "raw_sha256": artifact.raw_sha256,
            "canonical_path": str(canonical_blob), "edition": provenance_edition,
            "storage": storage}


def acquire_and_parse(row, stage_dir, *, timeout=120, session=None, fetched_at=None,
                      native_build=None,
                      original_store_root="/opt/dtox-source-recovery-20261012/originals-store-v1"):
    """Acquire and parse one ACL PDF or known-version arXiv HTML artifact.

    `row` needs `paper_id`; arXiv rows also need an explicit `fresh_version`.
    All writes go below the caller-provided candidate stage directory.
    """
    paper_id = row.get("paper_id") or row.get("arxiv_id")
    if not isinstance(paper_id, str):
        raise FreshSourceError("row is missing canonical paper ID")
    fresh_version = row.get("fresh_version")
    fresh_url = row.get("fresh_url") or row.get("source_url")
    if paper_id.startswith("acl:"):
        kind, edition = "PDF", paper_id
    elif _ARXIV_ID.fullmatch(paper_id):
        if not fresh_version:
            raise FreshSourceError("arXiv acquisition requires an explicitly resolved fresh version")
        kind, edition = row.get("fresh_kind", "rawTexarchive"), fresh_version
    else:
        kind = row.get("fresh_kind", "PDF")
        if kind != "PDF" or not isinstance(fresh_url, str):
            raise FreshSourceError("generic source requires a fresh HTTPS PDF URL")
        edition = fresh_version or "sha256:pending"
    fetch_started = time.perf_counter()
    artifact = fetch_fresh_artifact(paper_id, original_kind=kind, edition=edition,
                                    timeout=timeout, session=session, retrieved_at=fetched_at,
                                    source_url=fresh_url if not paper_id.startswith("acl:") and
                                    not _ARXIV_ID.fullmatch(paper_id) else None)
    fetch_seconds = time.perf_counter() - fetch_started
    parse_started = time.perf_counter()
    text, diagnostics = parse_fresh_artifact(artifact, timeout=timeout, native_build=native_build,
                                             expected_title=row.get("title"))
    diagnostics["fetch_seconds"] = round(fetch_seconds, 6)
    diagnostics["parse_seconds"] = round(time.perf_counter() - parse_started, 6)
    if diagnostics.get("replacement_characters", 0):
        diagnostics["body_quality_candidate"] = False
        diagnostics["body_quality_reason"] = "replacement_characters_present"
    elif diagnostics.get("unsupported_structures"):
        diagnostics["body_quality_candidate"] = False
        diagnostics["body_quality_reason"] = "unresolved_source_structures"
    elif kind == "HTML" and not (
            diagnostics.get("source_title_match") is True
            and diagnostics.get("article_source_chars", 0) > 0
            and diagnostics.get("nonabstract_section_count", 0) > 0
            and diagnostics.get("math") == diagnostics.get("article_math_rendered_count")
            and diagnostics.get("tables") == diagnostics.get("article_tables_rendered_count")
            and diagnostics.get("equation_layouts") == diagnostics.get("equation_layouts_rendered_count")):
        diagnostics["body_quality_candidate"] = False
        diagnostics["body_quality_reason"] = "incomplete_structured_article_projection"
    elif kind == "PDF" and not (
            diagnostics.get("page_count", 0) > 0
            and diagnostics.get("page_text_chars", 0) > 0
            and diagnostics.get("markdown_text_chars", 0) > 0
            and not diagnostics.get("unresolved_unicode_mappings")):
        diagnostics["body_quality_candidate"] = False
        diagnostics["body_quality_reason"] = "incomplete_pdf_page_or_unicode_coverage"
    else:
        diagnostics["body_quality_candidate"] = True
        diagnostics["body_quality_reason"] = "source_identity_and_extraction_coverage_verified"
    stage = Path(stage_dir)
    original_path = stage / "originals" / artifact.raw_sha256
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    suffix = ".tex" if kind == "rawTexarchive" else ".md"
    source_path = stage / "sources" / re.sub(r"[^A-Za-z0-9._-]", "_", paper_id) / (source_hash + suffix)
    staged_original_created = _write_content_once(original_path, artifact.raw_bytes)
    _write_content_once(source_path, text.encode("utf-8"))
    original_receipt = _register_original(artifact, original_path, original_store_root)
    if staged_original_created:
        if hashlib.sha256(original_path.read_bytes()).hexdigest() != artifact.raw_sha256:
            raise FreshSourceError("candidate raw stage changed after ORIGINAL registration")
        original_path.unlink()
    binding = {key: value for key, value in asdict(artifact).items() if key != "raw_bytes"}
    # `source_url` is the successful request endpoint that binds this exact raw SHA.
    binding["source_url"] = artifact.retrieval_url
    binding["url"] = artifact.retrieval_url
    binding["edition"] = original_receipt["edition"]
    binding["version"] = original_receipt["edition"]
    result = {"canonical_id": paper_id, "source_path": str(source_path),
              "source_sha256": source_hash, "source_binding": binding,
              "source_route": {"PDF": "paper_markdown", "HTML": "paper_markdown",
                               "rawTexarchive": "latex"}[kind],
              "raw_path": original_receipt["canonical_path"], "original_receipt": original_receipt,
              "chunks": [], "sections": [], "parser_mode": diagnostics.get("parser"),
              "diagnostics": diagnostics, "body_quality_candidate": diagnostics["body_quality_candidate"]}
    extractor, _ = _pipeline_modules()
    sections, elements = [], []
    chunk_started = time.perf_counter()
    if kind in {"PDF", "HTML"}:
        chunks, mode = extractor.build_chunks_markdown(
            text, section_sink=sections, element_sink=elements, document_kind="paper")
    else:
        chunks, mode = extractor.build_chunks(text, section_sink=sections, element_sink=elements)
    _validate_candidate_sections(paper_id, sections, text, mode, chunks)
    diagnostics["chunk_build_seconds"] = round(time.perf_counter() - chunk_started, 6)
    result.update(chunks=chunks, sections=sections, elements=elements, parser_mode=mode)
    return result
