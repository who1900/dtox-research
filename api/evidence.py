"""Read-only evidence labels derived from acquisition metadata, never IDs."""
import base64
import binascii
import hashlib
import json
from urllib.parse import unquote, urlsplit

try:
    from .source_attribution import curated_source, stored_source_url, valid_source_url
except ImportError:
    from source_attribution import curated_source, stored_source_url, valid_source_url


ABSTRACT_SOURCES = {"abstract-fallback", "iacr-abstract", "acl-abstract", "openalex-abstract"}
FULLTEXT_SOURCES = {"latex", "spec-markdown", "whitepaper-pdf", "acl-pdf",
                    "openalex-oa-pdf", "oa-mirror-pdf"}

LATEX_PARSER_MODES = {"pylatexenc", "pylatexenc_recovered"}
PDF_SOURCES = {"whitepaper-pdf", "acl-pdf", "openalex-oa-pdf", "oa-mirror-pdf"}


def _source_extension(url):
    if not url:
        return ""
    path = unquote(urlsplit(str(url)).path)
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def _resolve_acquisition(metadata, url):
    stored = metadata.get("fulltext_source")
    acquisition = stored
    parser_mode = metadata.get("parser_mode")
    if stored == "latex":
        if parser_mode == "markdown" or (
                parser_mode is None and _source_extension(url) in {"md", "mdx"}):
            acquisition = "spec-markdown"
    return acquisition, stored, parser_mode


def _extraction_type(acquisition, parser_mode):
    if acquisition in PDF_SOURCES:
        return "pdf_text", False
    if acquisition == "spec-markdown":
        return "markdown_source", False
    if acquisition == "latex":
        if parser_mode is None or parser_mode in LATEX_PARSER_MODES:
            return "latex_source", True
        return "unknown", False
    if acquisition in ABSTRACT_SOURCES:
        return "abstract_text", False
    return "unknown", False


def completeness(source):
    if source in ABSTRACT_SOURCES:
        return False, "abstract_only"
    if source in FULLTEXT_SOURCES:
        return True, "fulltext_acquired"
    return None, "unknown_acquisition" if source else "metadata_missing"


def resolution_reasons(*, missing_elements=(), errors=(), time_budget_exhausted=False,
                       chunk_limit=False, has_indexed_chunks=None):
    """Reasons for source resolution, independent of legacy partial semantics.

    Missing requested types mean not found in retrieved chunks, not source-wide
    absence or a retrieval error. Acquisition/fulltext labels are not consulted.
    Callers can retain partial=bool(missing_elements or errors or time_budget).
    """
    reasons = set()
    if missing_elements:
        reasons.add("not_found_in_retrieved_chunks")
    if errors:
        reasons.add("retrieval_failed")
    if time_budget_exhausted:
        reasons.add("time_budget")
    if chunk_limit:
        reasons.add("chunk_limit")
    if has_indexed_chunks is False:
        reasons.add("no_indexed_chunks")
    return sorted(reasons)


class EvidenceCursorError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def bounded_evidence_page(fragments, max_chars, cursor=None):
    """Clip even an oversized first element; resume against the same indexed snapshot.

    Input rows contain full text and indexed-chunk evidence (hash of full text).
    Public cursor/next_cursor is v1:<base64url(JSON)> (<=1024 characters), avoiding
    FastMCP's automatic decoding of plain JSON strings before type validation.
    Internal callers may also pass the continuation dict. The cursor counts
    Unicode characters, rejects changed/reordered snapshots (409; malformed 400),
    and needs no database writes. It is a local helper, not a new public route.
    Main's paper_spec can pass its provenance-labelled elements here after its
    existing ordering, then merge the returned fields into the legacy response.
    """
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    hashes = []
    for row in fragments:
        evidence = row.get("evidence") if isinstance(row, dict) else None
        fragment_hash = evidence.get("fragment_hash") if isinstance(evidence, dict) else None
        if not isinstance(row.get("text") if isinstance(row, dict) else None, str) or (
                not isinstance(fragment_hash, str) or len(fragment_hash) != 64 or
                any(char not in "0123456789abcdef" for char in fragment_hash)) or (
                evidence.get("fragment_scope") != "indexed_chunk"):
            raise ValueError("indexed fragment text and provenance required")
        hashes.append([fragment_hash, row["text"]])
    snapshot = hashlib.sha256(json.dumps(hashes).encode("utf-8")).hexdigest()
    index, offset = 0, 0
    if cursor is not None:
        if isinstance(cursor, str):
            if not cursor or len(cursor) > 1024:
                raise EvidenceCursorError("invalid continuation cursor")
            try:
                if not cursor.startswith("v1:"):
                    raise ValueError("unsupported cursor version")
                encoded = cursor[3:]
                raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                cursor = json.loads(raw.decode("utf-8"))
            except (ValueError, binascii.Error, RecursionError) as error:
                raise EvidenceCursorError("invalid continuation cursor") from error
        if not isinstance(cursor, dict) or set(cursor) != {"snapshot_hash", "fragment_index", "offset"} or (
                not isinstance(cursor.get("snapshot_hash"), str) or len(cursor["snapshot_hash"]) != 64 or
                any(char not in "0123456789abcdef" for char in cursor["snapshot_hash"])):
            raise EvidenceCursorError("invalid continuation cursor")
        if cursor["snapshot_hash"] != snapshot:
            raise EvidenceCursorError("continuation snapshot changed", status_code=409)
        index, offset = cursor.get("fragment_index"), cursor.get("offset")
        if type(index) is not int or type(offset) is not int or not 0 <= index < len(fragments) or (
                not 0 <= offset < len(fragments[index]["text"])):
            raise EvidenceCursorError("invalid continuation position")
    sections, used = [], 0
    while index < len(fragments):
        row = fragments[index]
        raw = row["text"]
        if not raw:
            index += 1
            continue
        if used == max_chars:
            break
        text = raw[offset:offset + max_chars - used]
        sections.append({**row, "text": text, "clipped": offset > 0 or len(text) < len(raw),
                         "evidence": {**row["evidence"], "returned_chars": len(text), "returned_offset": offset}})
        used += len(text)
        offset += len(text)
        if offset < len(raw):
            break
        index, offset = index + 1, 0
    continuation = ({"snapshot_hash": snapshot, "fragment_index": index, "offset": offset}
                    if index < len(fragments) else None)
    next_cursor = ("v1:" + base64.urlsafe_b64encode(json.dumps(continuation, separators=(",", ":")).encode("utf-8"))
                   .decode("ascii").rstrip("=")) if continuation is not None else None
    return {"sections": sections, "returned": len(sections), "available": len(fragments),
            "chars": used, "truncated": continuation is not None, "continuation": continuation,
            "next_cursor": next_cursor,
            "partial_reasons": ["character_budget"] if continuation is not None else []}


def provenance(paper_id, fragment, metadata=None, url=None, scope="returned_text"):
    metadata = metadata or {}
    curated = curated_source(paper_id, fragment.get("title") or metadata.get("title"))
    resolved_url = stored_source_url(metadata) or valid_source_url(url)
    supplied_urls = [metadata.get(key) for key in
                     ("source_url", "link", "pdf_url", "landing_page_url", "url")] + [url]
    provenance_reason = (None if resolved_url else
                         "source_url_invalid" if any(value not in (None, "") for value in supplied_urls)
                         else "source_url_missing")
    acquisition, stored_acquisition, parser_mode = _resolve_acquisition(metadata, resolved_url)
    fulltext, status = completeness(acquisition)
    extraction, latex = _extraction_type(acquisition, parser_mode)
    section = {"title": fragment.get("section_title"), "type": fragment.get("section_type")}
    identity = json.dumps([paper_id, section, fragment.get("element_type"),
                           fragment.get("text") or ""], sort_keys=True, ensure_ascii=False)
    catalog_match = curated and resolved_url == curated["url"]
    source_url_status = metadata.get("_source_url_status") or (
        curated["source_url_status"] if catalog_match else
        "source_not_verified" if resolved_url else "source_missing")
    source_url_origin = metadata.get("_source_url_origin") or (
        curated["origin"] if catalog_match else "payload" if resolved_url else None)
    if resolved_url is None:
        source_url_status, source_url_origin = "source_missing", None
    legacy_id_alias = metadata.get("_legacy_id_alias")
    if not legacy_id_alias and catalog_match:
        legacy_id_alias = curated.get("legacy_id_alias")
    evidence = {"acquisition_type": acquisition, "extraction_type": extraction,
            "source_url": resolved_url,
            "source_url_origin": source_url_origin,
            "source_url_status": source_url_status,
            "legacy_id_alias": legacy_id_alias,
            "provenance_missing": resolved_url is None,
            "provenance_reason": provenance_reason,
            "fulltext": fulltext, "completeness": status, "section": section,
            "parse_completeness": "unknown",
            "exact_latex": latex,
            "fragment_scope": scope, "fragment_chars": len(fragment.get("text") or ""),
            "fragment_hash": hashlib.sha256(identity.encode("utf-8")).hexdigest()}
    if stored_acquisition != acquisition:
        evidence["stored_acquisition_type"] = stored_acquisition
    if parser_mode is not None:
        evidence["parser_mode"] = parser_mode
    if metadata.get("parse_status") is not None:
        evidence["parse_status"] = metadata["parse_status"]
    return evidence


def hydrate_results(results, rows, url_for, license_for, source_for):
    out = []
    for result in results:
        item = dict(result)
        pid = item.get("arxiv_id") or item.get("id")
        metadata = rows.get(pid) or {}
        item_title = item.get("title") or metadata.get("title")
        curated = curated_source(pid, item_title)
        explicit_item_url = next((valid_source_url(item.get(key)) for key in
                                  ("source_url", "url") if valid_source_url(item.get(key))), None)
        stored_url = stored_source_url(metadata)
        url = stored_url or explicit_item_url or (curated or {}).get("url") or url_for(pid, metadata)
        url = valid_source_url(url)
        source_metadata = dict(metadata)
        catalog_match = curated and url == curated["url"]
        source_metadata["_source_url_origin"] = (
            "stored_metadata" if stored_source_url(metadata) else
            (curated or {}).get("origin") if catalog_match else
            "payload" if explicit_item_url and url == explicit_item_url else
            "canonical_id" if url else None)
        source_metadata["_source_url_status"] = (
            "source_not_verified" if stored_url else
            (curated or {}).get("source_url_status") if catalog_match else
            "source_not_verified" if explicit_item_url and url == explicit_item_url else
            "source_not_verified" if url else "source_missing")
        source_metadata["_legacy_id_alias"] = (curated or {}).get("legacy_id_alias")
        item["source"] = source_for(pid)
        item["provenance_missing"] = url is None
        item["source_url_origin"] = source_metadata["_source_url_origin"]
        item["source_url_status"] = source_metadata["_source_url_status"]
        if source_metadata["_legacy_id_alias"]:
            item["legacy_id_alias"] = source_metadata["_legacy_id_alias"]
        item["url"] = url
        if "arxiv_url" in item:
            item["arxiv_url"] = url
        item["license"] = license_for(pid)
        item["evidence"] = provenance(pid, item, source_metadata, url)
        original = result.get("evidence") or {}
        if original.get("fragment_scope") == "indexed_chunk" and original.get("fragment_hash"):
            for key in ("fragment_scope", "fragment_chars", "fragment_hash"):
                item["evidence"][key] = original[key]
        item["evidence"]["returned_chars"] = len(item.get("text") or "")
        item["fulltext"] = item["evidence"]["fulltext"]
        item["completeness"] = item["evidence"]["completeness"]
        out.append(item)
    return out
