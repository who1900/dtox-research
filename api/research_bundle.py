"""Bounded deterministic research packets: indexed evidence, no generation."""
from typing import Optional

from pydantic import BaseModel, Field

try:
    from .evidence import provenance
except ImportError:
    from evidence import provenance


BUNDLE_CHUNK_LIMIT = 200
BUNDLE_SECONDS = 20
BUNDLE_RETRIEVAL_TIMEOUT = 3
CATEGORIES = ("equations", "algorithms", "tables", "limitations", "code")
ELEMENT_CATEGORY = {"equation": "equations", "algorithm": "algorithms", "table": "tables", "code": "code"}


class ResearchBundleBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    layer: Optional[str] = None
    limit: int = Field(default=3, ge=1, le=5)
    max_chars: int = Field(default=12000, ge=500, le=20000)
    strict: bool = True


class BoundedRetriever:
    def __init__(self, retrieve, deadline, clock):
        self.retrieve = retrieve
        self.deadline = deadline
        self.clock = clock
        self.calls = 0

    def __call__(self, pid):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("bundle time budget exhausted")
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


def build_bundle(body, search_result, selected, retrieve):
    out = {"query": body.query.strip(), "layer": body.layer, "strict": body.strict,
           "papers": [], **{category: [] for category in CATEGORIES},
           "partial": bool(search_result.get("partial")), "search_partial": bool(search_result.get("partial")),
           "missing": {}, "warnings": [], "generated": False}
    if search_result.get("retrieval_warning"):
        out["warnings"].append(search_result["retrieval_warning"])
    used, calls = 0, 0
    selected = selected[:body.limit]
    for paper_index, hit in enumerate(selected):
        pid = hit["arxiv_id"]
        source_evidence = hit.get("evidence") or {}
        metadata = {"fulltext_source": source_evidence.get("acquisition_type"), "source_url": hit.get("url")}
        paper = {"id": pid, "title": hit.get("title"), "year": hit.get("year"), "score": hit.get("score"),
                 **{key: hit.get(key) for key in ("source", "url", "license", "fulltext", "completeness")},
                 "acquisition_type": source_evidence.get("acquisition_type"),
                 "extraction_type": source_evidence.get("extraction_type", "unknown"),
                 "parse_completeness": source_evidence.get("parse_completeness", "unknown"),
                 "missing": {}, "partial": hit.get("fulltext") is not True,
                 "chunks_retrieved": 0, "chunk_limit_reached": False}
        calls += 1
        failed = False
        try:
            points = retrieve(pid)
        except Exception as error:
            points = []
            failed = True
            paper["retrieval_error"] = type(error).__name__
            paper["time_budget_exhausted"] = isinstance(error, TimeoutError)
        paper["chunks_retrieved"] = min(len(points), BUNDLE_CHUNK_LIMIT)
        capped = len(points) >= BUNDLE_CHUNK_LIMIT
        paper["chunk_limit_reached"] = capped
        fragments = []
        for point in points[:BUNDLE_CHUNK_LIMIT]:
            fragment = point.get("payload") or {}
            if fragment.get("arxiv_id") not in (None, pid):
                paper["partial"] = True
                continue
            category = _category(fragment)
            if not category or not fragment.get("text"):
                continue
            evidence = provenance(pid, fragment, metadata, hit.get("url"), scope="indexed_chunk")
            fragments.append((CATEGORIES.index(category), fragment.get("chunk_index") or 0,
                              evidence["fragment_hash"], category, fragment, evidence))
        fragments.sort(key=lambda item: item[:3])
        unique, hashes = [], set()
        capacities = {}
        for _, _, fragment_hash, category, fragment, evidence in fragments:
            if (category, fragment_hash) in hashes:
                continue
            hashes.add((category, fragment_hash))
            unique.append((category, fragment, evidence))
            capacities[category] = capacities.get(category, 0) + len(fragment["text"])
        paper_budget = body.max_chars // len(selected) + (paper_index < body.max_chars % len(selected))
        quotas = _category_quotas(capacities, paper_budget)
        paper["budgets"] = {"max_chars": paper_budget, "category_chars": dict(quotas)}
        available, returned = set(capacities), set()
        for category, fragment, evidence in unique:
            remaining = quotas[category]
            if remaining <= 0:
                paper["partial"] = True
                continue
            raw = fragment["text"]
            text = raw[:remaining]
            clipped = len(text) < len(raw)
            evidence["returned_chars"] = len(text)
            out[category].append({"paper_id": pid, "section_title": fragment.get("section_title"),
                                  "section_type": fragment.get("section_type"),
                                  "element_type": fragment.get("element_type"), "text": text,
                                  "clipped": clipped, "evidence": evidence})
            returned.add(category)
            used += len(text)
            quotas[category] -= len(text)
            paper["partial"] = paper["partial"] or clipped
        for category in CATEGORIES:
            if category not in returned:
                paper["missing"][category] = ("time_budget" if paper.get("time_budget_exhausted") else
                    "retrieval_failed" if failed else
                    "character_budget" if category in available else
                    "chunk_limit" if capped else
                    "abstract_only" if hit.get("fulltext") is False else
                    "metadata_missing" if hit.get("fulltext") is None else "not_found_in_retrieved_chunks")
        paper["partial"] = paper["partial"] or failed or capped or not points
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
    out["note"] = ("Evidence from indexed chunks only. Fulltext acquired does not guarantee complete parsing; "
                   "missing elements are not proof of absence. PDF text is not exact LaTeX.")
    return out
