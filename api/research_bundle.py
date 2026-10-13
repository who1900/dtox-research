"""Bounded deterministic research packets: indexed evidence, no generation."""
from typing import Optional

from pydantic import BaseModel, Field

try:
    from .evidence import provenance, resolution_reasons
except ImportError:
    from evidence import provenance, resolution_reasons


BUNDLE_CHUNK_LIMIT = 200
BUNDLE_SECONDS = 20
BUNDLE_RETRIEVAL_TIMEOUT = 3
MAX_ALIAS_SOURCE_READS_PER_PAPER = 3
CATEGORIES = ("equations", "algorithms", "tables", "limitations", "code")
ELEMENT_CATEGORY = {"equation": "equations", "algorithm": "algorithms", "table": "tables", "code": "code"}


class ResearchBundleBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    layer: Optional[str] = None
    limit: int = Field(default=3, ge=1, le=5)
    max_chars: int = Field(default=12000, ge=500, le=20000)
    strict: bool = True


class BundleTimeBudgetExceeded(TimeoutError):
    pass


class BoundedRetriever:
    def __init__(self, retrieve, deadline, clock):
        self.retrieve = retrieve
        self.deadline = deadline
        self.clock = clock
        self.calls = 0

    def __call__(self, pid):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BundleTimeBudgetExceeded("bundle time budget exhausted")
        self.calls += 1
        return self.retrieve(pid, timeout=min(BUNDLE_RETRIEVAL_TIMEOUT, remaining))


def _category(fragment):
    section = (fragment.get("section_type") or "").lower()
    title = (fragment.get("section_title") or "").lower()
    if section == "limitations" or "limitation" in title:
        return "limitations"
    return ELEMENT_CATEGORY.get((fragment.get("element_type") or "").lower())


def _category_quotas(capacities, budget):
    quotas = {category: 0 for category in capacities}
    pending = list(capacities)
    while pending and budget:
        share = max(1, budget // len(pending))
        for category in pending:
            take = min(share, capacities[category] - quotas[category], budget)
            quotas[category] += take
            budget -= take
        pending = [category for category in pending if quotas[category] < capacities[category]]
    return quotas


def _valid_points(points, source_id, reasons):
    if not isinstance(points, list):
        reasons.add("retrieval_failed")
        return []
    accepted = []
    for point in points:
        fragment = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(fragment, dict) or any(
                fragment.get(key) is not None and not isinstance(fragment[key], str)
                for key in ("text", "element_type", "section_type", "section_title")) or (
                fragment.get("chunk_index") is not None and not isinstance(fragment["chunk_index"], int)):
            reasons.add("retrieval_failed")
            continue
        if fragment.get("arxiv_id") not in (None, source_id):
            reasons.add("invalid_provenance")
            continue
        accepted.append(point)
    return accepted


def build_bundle(body, search_result, selected, retrieve):
    """Keep legacy partial flags; reasons distinguish bounds, failures and availability.

    Missing categories describe only retrieved indexed chunks, never source-wide
    absence. A resolution partial without errors still remains partial: aliases
    may not contain every requested category. Parse completeness stays unknown.
    """
    out = {"query": body.query.strip(), "layer": body.layer, "strict": body.strict,
           "papers": [], **{category: [] for category in CATEGORIES},
           "partial": bool(search_result.get("partial")), "search_partial": bool(search_result.get("partial")),
           "partial_reasons": ["search_partial"] if search_result.get("partial") else [],
           "missing": {}, "warnings": [], "generated": False}
    if search_result.get("retrieval_warning"):
        out["warnings"].append(search_result["retrieval_warning"])
    used, calls, envelope_seen = 0, 0, False
    selected = selected[:body.limit]
    for paper_index, hit in enumerate(selected):
        pid = hit["arxiv_id"]
        source_evidence = hit.get("evidence") or {}
        metadata = {"fulltext_source": source_evidence.get("stored_acquisition_type") or source_evidence.get("acquisition_type"),
                    "source_url": hit.get("url") or source_evidence.get("source_url"), "title": hit.get("title"),
                    **{key: source_evidence[key] for key in ("parser_mode", "parse_status") if key in source_evidence},
                    "_source_url_origin": source_evidence.get("source_url_origin"),
                    "_source_url_status": source_evidence.get("source_url_status"),
                    "_legacy_id_alias": source_evidence.get("legacy_id_alias")}
        paper = {"id": pid, "title": hit.get("title"), "year": hit.get("year"), "score": hit.get("score"),
                 **{key: hit.get(key) for key in ("source", "url", "license", "fulltext", "completeness",
                                                  "provenance_missing", "source_url_origin",
                                                  "source_url_status", "legacy_id_alias")},
                 "acquisition_type": source_evidence.get("acquisition_type"),
                 "extraction_type": source_evidence.get("extraction_type", "unknown"),
                 "parse_completeness": source_evidence.get("parse_completeness", "unknown"),
                 "missing": {}, "partial": hit.get("fulltext") is not True,
                 "chunks_retrieved": 0, "chunk_limit_reached": False}
        reasons = set()
        if hit.get("fulltext") is not True:
            reasons.add("abstract_only" if hit.get("fulltext") is False else "metadata_missing")
        calls += 1
        failed = False
        try:
            retrieval = retrieve(pid)
        except Exception as error:
            retrieval = []
            points = []
            failed = True
            paper["retrieval_error"] = type(error).__name__
            paper["time_budget_exhausted"] = isinstance(error, BundleTimeBudgetExceeded)
            reasons.add("time_budget" if paper["time_budget_exhausted"] else "retrieval_failed")
        envelope = isinstance(retrieval, dict) and isinstance(retrieval.get("sources"), list)
        envelope_seen = envelope_seen or envelope
        resolution = retrieval.get("resolution") or {} if envelope else {}
        source_rows = retrieval.get("sources", [])[:MAX_ALIAS_SOURCE_READS_PER_PAPER] if envelope else []
        if not isinstance(resolution, dict):
            resolution = {}
            reasons.add("retrieval_failed")
        if envelope:
            paper["source_resolution"] = resolution
            paper["extraction_sources"] = []
            if len(retrieval.get("sources", [])) > MAX_ALIAS_SOURCE_READS_PER_PAPER:
                paper["partial"] = True
                reasons.add("source_limit")
            normalized = []
            allowed_source_ids = set(resolution.get("attempted_ids") or resolution.get("source_ids") or [])
            for source_index, source in enumerate(source_rows):
                if not isinstance(source, dict):
                    reasons.add("retrieval_failed")
                    continue
                source_id = source.get("id")
                if not isinstance(source_id, str) or (allowed_source_ids and source_id not in allowed_source_ids):
                    reasons.add("invalid_provenance")
                    continue
                source_metadata = source.get("metadata") or {}
                passport = source.get("passport") or {}
                if not isinstance(source_metadata, dict) or not isinstance(passport, dict):
                    reasons.add("invalid_provenance")
                    source_metadata = source_metadata if isinstance(source_metadata, dict) else {}
                    passport = passport if isinstance(passport, dict) else {}
                passport_labels = {key: passport.get(key) for key in ("url", "source_url_origin", "source_url_status",
                                                                       "legacy_id_alias", "source", "license")
                                   if isinstance(passport, dict) and passport.get(key) is not None}
                if source_id != pid:
                    paper["extraction_sources"].append({"source_paper_id": source_id, **passport_labels})
                for point in _valid_points(source.get("points", []), source_id, reasons):
                    normalized.append((source_index, source_id, point, source_metadata, passport))
            requested_categories = {_category(row[2].get("payload") or {}) for row in normalized if row[1] == pid}
            requested_categories.discard(None)
            normalized = [row for row in normalized if row[1] == pid or
                          _category(row[2].get("payload") or {}) not in requested_categories]
            # Structured evidence from every source is considered before the shared chunk cap.
            normalized.sort(key=lambda row: (_category(row[2].get("payload") or {}) is None, row[0],
                                               (row[2].get("payload") or {}).get("chunk_index") or 0))
            points = normalized[:BUNDLE_CHUNK_LIMIT]
            reasons.update(resolution_reasons(errors=resolution.get("errors"),
                                             time_budget_exhausted=resolution.get("time_budget_exhausted")))
            if resolution.get("partial") and not resolution.get("errors") and not resolution.get("time_budget_exhausted"):
                reasons.add("source_resolution_incomplete")
            paper["partial"] = paper["partial"] or bool(resolution.get("partial")) or bool(resolution.get("time_budget_exhausted"))
            source_counts = [len(source.get("points")) for source in source_rows
                             if isinstance(source, dict) and isinstance(source.get("points"), list)]
            paper["partial"] = paper["partial"] or any(count >= BUNDLE_CHUNK_LIMIT for count in source_counts)
        else:
            raw_points = retrieval if isinstance(retrieval, list) else []
            points = [(0, pid, point, metadata, {}) for point in
                      _valid_points(raw_points[:BUNDLE_CHUNK_LIMIT], pid, reasons)]
            if not isinstance(retrieval, list):
                reasons.add("retrieval_failed")
            source_counts = [len(raw_points)]
        retrieved_count = sum(source_counts)
        paper["chunks_retrieved"] = min(retrieved_count, BUNDLE_CHUNK_LIMIT)
        capped = retrieved_count >= BUNDLE_CHUNK_LIMIT or bool(resolution.get("chunk_limit"))
        if capped:
            reasons.add("chunk_limit")
        paper["chunk_limit_reached"] = capped
        payloads = [point[2]["payload"] for point in points if point[2]["payload"].get("text")]
        paper["indexed_content"] = ("empty" if not payloads else
            "abstract_only" if all((fragment.get("section_type") or "").lower() == "abstract" for fragment in payloads)
            else "structured_evidence" if any(_category(fragment) for fragment in payloads) else "other_text")
        paper["indexed_content_scope"] = "retrieved_chunks"
        if paper["indexed_content"] == "abstract_only":
            reasons.add("abstract_only")
        fragments = []
        for source_index, source_id, point, fragment_metadata, passport in points:
            fragment = point.get("payload") or {}
            category = _category(fragment)
            actual_url = passport.get("url") if isinstance(passport, dict) else None
            evidence_metadata = dict(fragment_metadata)
            if envelope and isinstance(passport, dict):
                evidence_metadata["source_url"] = passport.get("url") or evidence_metadata.get("source_url")
                evidence_metadata["_source_url_origin"] = passport.get("source_url_origin") or evidence_metadata.get("_source_url_origin")
                evidence_metadata["_source_url_status"] = passport.get("source_url_status") or evidence_metadata.get("_source_url_status")
                evidence_metadata["_legacy_id_alias"] = passport.get("legacy_id_alias") or evidence_metadata.get("_legacy_id_alias")
            evidence = provenance(source_id, fragment, evidence_metadata if envelope else metadata,
                                  actual_url if envelope else hit.get("url"), scope="indexed_chunk")
            if envelope:
                evidence["source_paper_id"] = source_id
            if evidence["provenance_missing"]:
                reasons.add("invalid_provenance" if evidence.get("provenance_reason") == "source_url_invalid"
                            else "provenance_missing")
            if not category or not fragment.get("text"):
                continue
            fragments.append((CATEGORIES.index(category), fragment.get("chunk_index") or 0,
                              evidence["fragment_hash"], category, fragment, evidence, source_id, source_index))
        fragments.sort(key=lambda item: (item[0], item[7] if envelope else 0, item[1], item[2]))
        unique, hashes = [], set()
        capacities = {}
        for _, _, fragment_hash, category, fragment, evidence, source_id, source_index in fragments:
            dedup_key = (category, fragment.get("text")) if envelope else (category, fragment_hash)
            if dedup_key in hashes:
                continue
            hashes.add(dedup_key)
            unique.append((category, fragment, evidence, source_id))
            capacities[category] = capacities.get(category, 0) + len(fragment["text"])
        paper_budget = body.max_chars // len(selected) + (paper_index < body.max_chars % len(selected))
        quotas = _category_quotas(capacities, paper_budget)
        paper["budgets"] = {"max_chars": paper_budget, "category_chars": dict(quotas)}
        available, returned = set(capacities), set()
        for category, fragment, evidence, source_id in unique:
            remaining = quotas[category]
            if remaining <= 0:
                paper["partial"] = True
                reasons.add("character_budget")
                continue
            raw = fragment["text"]
            text = raw[:remaining]
            clipped = len(text) < len(raw)
            evidence["returned_chars"] = len(text)
            row = {"paper_id": pid, "section_title": fragment.get("section_title"),
                                  "section_type": fragment.get("section_type"),
                                  "element_type": fragment.get("element_type"), "text": text,
                                  "clipped": clipped, "evidence": evidence}
            if envelope:
                row["source_paper_id"] = source_id
            out[category].append(row)
            returned.add(category)
            used += len(text)
            quotas[category] -= len(text)
            paper["partial"] = paper["partial"] or clipped
            if clipped:
                reasons.add("character_budget")
        for category in CATEGORIES:
            if category not in returned:
                paper["missing"][category] = ("time_budget" if paper.get("time_budget_exhausted") else
                    "time_budget" if envelope and resolution.get("time_budget_exhausted") else
                    "retrieval_failed" if failed else
                    "character_budget" if category in available else
                    "retrieval_failed" if "retrieval_failed" in reasons else
                    "invalid_provenance" if "invalid_provenance" in reasons else
                    "chunk_limit" if capped else
                    "abstract_only" if hit.get("fulltext") is False or paper["indexed_content"] == "abstract_only" else
                    "metadata_missing" if hit.get("fulltext") is None else "not_found_in_retrieved_chunks")
        paper["partial"] = paper["partial"] or failed or capped or not points or (envelope and len(source_rows) > 1 and bool(resolution.get("partial")))
        if not points and not failed and not reasons.intersection({"retrieval_failed", "invalid_provenance", "time_budget"}):
            reasons.add("no_indexed_chunks")
        paper["partial"] = paper["partial"] or bool(reasons)
        paper["partial_reasons"] = sorted(reasons)
        out["partial_reasons"] = sorted(set(out["partial_reasons"]) | reasons)
        out["partial"] = out["partial"] or paper["partial"]
        out["papers"].append(paper)
    out["missing"] = {"papers": not out["papers"], **{category: not out[category] for category in CATEGORIES}}
    out["budgets"] = {"paper_limit": body.limit, "papers_selected": len(out["papers"]),
                      "max_chars": body.max_chars, "chars_returned": used,
                      "chars_scope": "extracted_text", "chunk_limit_per_paper": BUNDLE_CHUNK_LIMIT,
                      "allocation": "equal per-paper quotas; fair per-category quotas within each paper",
                      "paper_retrieval_calls": getattr(retrieve, "calls", calls),
                      "wall_time_budget_seconds": BUNDLE_SECONDS,
                      "max_retrieval_timeout_seconds": BUNDLE_RETRIEVAL_TIMEOUT}
    if envelope_seen:
        out["budgets"]["max_alias_source_reads_per_paper"] = MAX_ALIAS_SOURCE_READS_PER_PAPER
    out["note"] = ("Evidence from indexed chunks only. Fulltext acquired does not guarantee complete parsing; "
                   "missing elements are not proof of absence. PDF text is not exact LaTeX.")
    return out
