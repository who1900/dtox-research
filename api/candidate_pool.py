"""Deterministic, in-memory candidate ledger for already hydrated search rows."""

import copy
import math


def finite_score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, TypeError, ValueError):
        return None


def _channels(row):
    found = row.get("channels") or []
    if not isinstance(found, (list, tuple)):
        return []
    return list(dict.fromkeys(channel for channel in found
                              if isinstance(channel, str) and channel))


def _merge_provenance(target, source):
    channels = list(dict.fromkeys(_channels(target) + _channels(source)))
    if channels:
        target["channels"] = channels
    ranks = dict(target.get("fusion_ranks") or {})
    for channel, rank in (source.get("fusion_ranks") or {}).items():
        old_rank = finite_score(ranks.get(channel))
        new_rank = finite_score(rank)
        if channel not in ranks or old_rank is None or (
                new_rank is not None and new_rank < old_rank):
            ranks[channel] = rank
    if ranks:
        target["fusion_ranks"] = ranks


def _hit_row(hit, result_from_hit, metadata):
    row = result_from_hit(hit)
    paper_id = row.get("arxiv_id")
    for name in ("found_via", "fusion_ranks", "fusion_score", "paper_score", "channels"):
        value = metadata.get(name, {}).get(paper_id)
        if value is not None:
            row[name] = copy.deepcopy(value)
    ranks = row.get("fusion_ranks") or {}
    rank_channels = {"dense": "dense", "lex": "lexical", "global": "global",
                     "chunk": "chunk", "graph": "graph"}
    observed = list(row.get("channels") or [])
    observed.extend(rank_channels[key] for key, value in ranks.items()
                    if key in rank_channels and finite_score(value) is not None)
    if observed:
        row["channels"] = list(dict.fromkeys(observed))
    row["score"] = finite_score(hit.get("score"))
    return row


def build_candidate_pool(primary_results, precut_results, hits, canonical_id,
                         result_from_hit, *, found_via=None, fusion_ranks=None,
                         fusion_scores=None, paper_scores=None):
    """Return copied rows ordered primary, qualified pre-cut, then raw hits."""
    metadata = {"found_via": found_via or {}, "fusion_ranks": fusion_ranks or {},
                "fusion_score": fusion_scores or {}, "paper_score": paper_scores or {}}
    ordered = []
    by_key = {}

    def add(row, priority):
        paper_id = row.get("arxiv_id")
        if not paper_id:
            return
        key = canonical_id(paper_id)
        if not key:
            return
        existing = by_key.get(key)
        if existing is None:
            copied = copy.deepcopy(row)
            copied["score"] = finite_score(copied.get("score"))
            by_key[key] = [copied, priority]
            ordered.append(key)
            return
        preferred, old_priority = existing
        _merge_provenance(preferred, row)
        if priority < old_priority:
            replacement = copy.deepcopy(row)
            _merge_provenance(replacement, preferred)
            replacement["score"] = finite_score(replacement.get("score"))
            existing[:] = [replacement, priority]

    for row in primary_results:
        add(row, 0)
    for row in precut_results:
        add(row, 1)
    for hit in hits:
        row = _hit_row(hit, result_from_hit, metadata)
        add(row, 2)

    rows = [by_key[key][0] for key in ordered]
    primary_ids = {canonical_id(row.get("arxiv_id")) for row in primary_results
                   if row.get("arxiv_id")}
    candidates = [row for row in rows if canonical_id(row.get("arxiv_id")) not in primary_ids]
    candidate_channels = {}
    returned_channels = {}
    for row in candidates:
        for channel in _channels(row):
            candidate_channels[channel] = candidate_channels.get(channel, 0) + 1
    for row in primary_results:
        for channel in _channels(row):
            returned_channels[channel] = returned_channels.get(channel, 0) + 1
    channels = sorted(set(candidate_channels) | set(returned_channels))
    summary = {
        "scope": "observed_local",
        "ordering_policy": "final_primary_then_pre_cut_then_hydrated_hierarchy; canonical-id dedupe",
        "observed_candidate_count": len(candidates),
        "returned_unique_count": len({canonical_id(row.get("arxiv_id"))
                                        for row in primary_results if row.get("arxiv_id")}),
        "channels": {name: {"candidate_count": candidate_channels.get(name, 0),
                            "returned_count": returned_channels.get(name, 0)}
                     for name in channels},
    }
    return rows, summary


def rows_as_hits(rows):
    """Adapt ledger rows to the existing bounded source-candidate selector."""
    output = []
    for row in rows:
        output.append({
            "score": finite_score(row.get("score")),
            "payload": {key: copy.deepcopy(row.get(key)) for key in (
                "arxiv_id", "title", "text", "section_type", "section_title",
                "element_type", "layers", "year", "repos", "terms", "venue",
                "citation_count", "channels", "found_via", "fusion_ranks",
                "fusion_score", "paper_score") if key in row},
        })
    return output
