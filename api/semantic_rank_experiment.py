"""Isolated, bounded semantic reranking experiment for source discovery.

Candidates are retrieval leads, never evidence. The scorer is expected to
return one finite numeric relevance logit per (title, passage) pair; those
logits are neither probabilities nor entailment/verification judgments.
"""

import math
import numbers


MAX_CANDIDATES = 20
MAX_PAIRS = 40
MAX_TITLE_CHARS = 256
MAX_PASSAGE_CHARS = 1200


def _valid_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _ranks_for(fusion_ranks, paper_id, payload):
    ranks = (fusion_ranks or {}).get(paper_id)
    if ranks is None:
        ranks = payload.get("fusion_ranks")
    if not isinstance(ranks, dict):
        ranks = {}
    return {str(channel): rank for channel, rank in ranks.items()
            if isinstance(rank, numbers.Real) and not isinstance(rank, bool)
            and rank >= 1 and math.isfinite(rank)}


def build_candidates(hits, canonical_id, is_boilerplate, fusion_ranks=None,
                     limit=20, passages_per_paper=2):
    """Group hydrated hits by canonical paper, keeping stable hit/channel ranks.

    Returns at most 20 source-candidate dictionaries. Each has ``arxiv_id``,
    ``canonical_id``, ``title``, ``score`` (the best original hit score, or
    None), ``provenance`` (per-passage original score/hit id/rank), ``passages``
    (distinct non-boilerplate text with source scores and section labels),
    ``paper_rank`` and ``channel_ranks``. Missing title/text is recorded in
    ``metadata_status``. The predicate receives a hit, matching the search
    helpers; no score threshold/floor is applied.
    """
    if not _valid_positive_int(limit) or not _valid_positive_int(passages_per_paper):
        return []
    passage_cap = min(passages_per_paper, MAX_PAIRS)
    cap = min(limit, MAX_CANDIDATES, MAX_PAIRS // passage_cap)
    grouped = {}
    for hit_index, hit in enumerate(hits or []):
        if not isinstance(hit, dict):
            continue
        payload = hit.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        paper_id = payload.get("arxiv_id")
        if not paper_id:
            continue
        key = canonical_id(paper_id)
        if not key:
            continue
        group = grouped.setdefault(key, {"paper_id": paper_id, "first": hit_index,
                                         "title": None, "passages": [],
                                         "seen_text": set(), "channel_ranks": {},
                                         "scores": [], "provenance": []})
        title = payload.get("title")
        if isinstance(title, str) and title.strip() and group["title"] is None:
            group["title"] = " ".join(title.split())[:MAX_TITLE_CHARS]
        for channel, rank in _ranks_for(fusion_ranks, paper_id, payload).items():
            old = group["channel_ranks"].get(channel)
            if old is None or rank < old:
                group["channel_ranks"][channel] = rank
        score = hit.get("score")
        valid_score = isinstance(score, numbers.Real) and not isinstance(score, bool)
        if valid_score:
            score = float(score)
            valid_score = math.isfinite(score)
        if not valid_score:
            score = None
        else:
            group["scores"].append(score)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or is_boilerplate(hit):
            continue
        text = " ".join(text.split())[:MAX_PASSAGE_CHARS]
        if text in group["seen_text"] or len(group["passages"]) >= passage_cap:
            continue
        group["seen_text"].add(text)
        passage = {"text": text, "section_title": payload.get("section_title"),
                   "section_type": payload.get("section_type"),
                   "element_type": payload.get("element_type"),
                   "score": score, "hit_id": hit.get("id"),
                   "hit_rank": hit_index + 1}
        group["passages"].append(passage)
        group["provenance"].append({"score": score, "hit_id": hit.get("id"),
                                    "hit_rank": hit_index + 1,
                                    "section_title": passage["section_title"],
                                    "section_type": passage["section_type"]})

    candidates = []
    for paper_rank, group in enumerate(sorted(grouped.values(),
                                              key=lambda item: item["first"]), 1):
        if not group["passages"]:
            continue
        candidates.append({
            "arxiv_id": group["paper_id"], "canonical_id": canonical_id(group["paper_id"]),
            "title": group["title"], "score": max(group["scores"] or [None]),
            "paper_rank": paper_rank,
            "channel_ranks": dict(group["channel_ranks"]),
            "passages": group["passages"], "provenance": group["provenance"],
            "metadata_status": "complete" if group["title"] else "missing_title",
            "retrieval_role": "unverified_source_candidate",
            "verification_required": True,
        })
        if len(candidates) >= cap:
            break
    return candidates


def rerank_candidates(query, candidates, scorer):
    """Rerank by max pair logit; on any scorer/shape/value failure, use baseline.

    ``scorer(query, pairs)`` is invoked exactly once with at most 40
    ``{"title": ..., "passage": ...}`` pairs. The report's ``candidates`` is
    always safe to consume: it is baseline order on failure and reranked order
    on success. Reranking never marks a candidate as evidence or promotes it
    to verified status; negative logits remain ordinary retrieval leads.
    """
    baseline = candidates if isinstance(candidates, list) else list(candidates or [])
    bounded = baseline[:MAX_CANDIDATES]
    pairs = []
    owners = []
    for candidate_index, candidate in enumerate(bounded):
        title = candidate.get("title") if isinstance(candidate, dict) else None
        if not isinstance(title, str) or not title.strip():
            continue
        for passage in candidate.get("passages") or []:
            text = passage.get("text") if isinstance(passage, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            if len(pairs) >= MAX_PAIRS:
                break
            pairs.append({"title": title, "passage": text})
            owners.append(candidate_index)
        if len(pairs) >= MAX_PAIRS:
            break
    if not pairs:
        return {"status": "fallback", "reason": "no_scorable_pairs",
                "pairs_scored": 0, "candidates": baseline,
                "baseline_candidates": baseline, "reranked": False,
                "evidence": False}
    try:
        scores = scorer(query, pairs)
        if not isinstance(scores, (list, tuple)) or len(scores) != len(pairs):
            raise ValueError("scorer output length mismatch")
        if any(isinstance(value, bool) or not isinstance(value, numbers.Real)
               or not math.isfinite(value) for value in scores):
            raise ValueError("scorer scores must be finite numeric logits")
    except Exception as error:
        return {"status": "fallback", "reason": "scorer_failure",
                "error_type": type(error).__name__, "pairs_scored": len(pairs),
                "candidates": baseline, "baseline_candidates": baseline,
                "reranked": False, "evidence": False}

    best = {}
    for owner, value in zip(owners, scores):
        if owner not in best or value > best[owner]:
            best[owner] = value
    ranked = []
    for index, candidate in enumerate(bounded):
        row = dict(candidate)
        row["semantic_logit"] = best.get(index)
        row["_semantic_input_order"] = index
        row["retrieval_role"] = "unverified_source_candidate"
        row["verification_required"] = True
        ranked.append(row)
    ranked.sort(key=lambda row: (row["semantic_logit"] is None,
                                 -row["semantic_logit"]
                                 if row["semantic_logit"] is not None else 0,
                                 row["_semantic_input_order"]))
    for rank, row in enumerate(ranked, 1):
        row["semantic_rank"] = rank
        del row["_semantic_input_order"]
    return {"status": "reranked", "pairs_scored": len(pairs),
            "candidates": ranked, "baseline_candidates": baseline,
            "reranked": True, "evidence": False,
            "score_semantics": "relevance_logit_not_probability_or_entailment"}
