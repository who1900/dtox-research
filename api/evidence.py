"""Read-only evidence labels derived from acquisition metadata, never IDs."""
import hashlib
import json


ABSTRACT_SOURCES = {"abstract-fallback", "iacr-abstract", "acl-abstract", "openalex-abstract"}
FULLTEXT_SOURCES = {"latex", "spec-markdown", "whitepaper-pdf", "acl-pdf",
                    "openalex-oa-pdf", "oa-mirror-pdf"}


def completeness(source):
    if source in ABSTRACT_SOURCES:
        return False, "abstract_only"
    if source in FULLTEXT_SOURCES:
        return True, "fulltext_acquired"
    return None, "unknown_acquisition" if source else "metadata_missing"


def provenance(paper_id, fragment, metadata=None, url=None, scope="returned_text"):
    metadata = metadata or {}
    acquisition = metadata.get("fulltext_source")
    fulltext, status = completeness(acquisition)
    latex = acquisition == "latex" and not str(paper_id).startswith("gh:")
    extraction = ("latex_source" if latex else
                  "markdown_source" if acquisition == "latex" and str(paper_id).startswith("gh:") else
                  "markdown_source" if acquisition == "spec-markdown" else
                  "pdf_text" if acquisition in FULLTEXT_SOURCES else
                  "abstract_text" if acquisition in ABSTRACT_SOURCES else "unknown")
    section = {"title": fragment.get("section_title"), "type": fragment.get("section_type")}
    identity = json.dumps([paper_id, section, fragment.get("element_type"),
                           fragment.get("text") or ""], sort_keys=True, ensure_ascii=False)
    return {"acquisition_type": acquisition, "extraction_type": extraction,
            "source_url": metadata.get("source_url") or url,
            "fulltext": fulltext, "completeness": status, "section": section,
            "parse_completeness": "unknown",
            "exact_latex": latex,
            "fragment_scope": scope, "fragment_chars": len(fragment.get("text") or ""),
            "fragment_hash": hashlib.sha256(identity.encode("utf-8")).hexdigest()}


def hydrate_results(results, rows, url_for, license_for, source_for):
    out = []
    for result in results:
        item = dict(result)
        pid = item.get("arxiv_id") or item.get("id")
        metadata = rows.get(pid) or {}
        url = metadata.get("source_url") or item.get("url") or url_for(pid)
        item["source"] = source_for(pid)
        item["url"] = url
        if "arxiv_url" in item:
            item["arxiv_url"] = url
        item["license"] = license_for(pid)
        item["evidence"] = provenance(pid, item, metadata, url)
        original = result.get("evidence") or {}
        if original.get("fragment_scope") == "indexed_chunk" and original.get("fragment_hash"):
            for key in ("fragment_scope", "fragment_chars", "fragment_hash"):
                item["evidence"][key] = original[key]
        item["evidence"]["returned_chars"] = len(item.get("text") or "")
        item["fulltext"] = item["evidence"]["fulltext"]
        item["completeness"] = item["evidence"]["completeness"]
        out.append(item)
    return out
