import re


RRF_K = 60


def fts_query(text):
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", text or "")
             if len(w) > 1]
    return " AND ".join(f'"{w}"' for w in words[:12])


def reciprocal_rank_fusion(*ranked_lists, weights=None):
    weights = weights or [1.0] * len(ranked_lists)
    scores = {}
    for ranked, weight in zip(ranked_lists, weights):
        for rank, key in enumerate(ranked):
            scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank + 1)
    return scores


def merge_layer_hits_calibrated(tagged_result_sets, layer_medians, default_median, limit):
    """Fuse per-layer rankings by how far each hit sits above its layer's median.

    Rank fusion treated every layer's first hit as equal, so a 0.788 hit that
    tops llm-slm (median 0.898) tied with a 0.920 web3 hit (median 0.806), and
    the tie broke arbitrarily. Offsetting by the layer median keeps the small
    layers from being drowned by LLM wording without discarding the score.
    """
    best = {}
    for layer, hits in tagged_result_sets:
        median = layer_medians.get(layer, default_median)
        for hit in hits:
            point_id = hit.get("id")
            if point_id is None:
                continue
            relative = (hit.get("score") or 0) - median
            previous = best.get(point_id)
            if previous is None or relative > previous[0]:
                best[point_id] = (relative, hit)
    ordered = sorted(best.values(), key=lambda pair: -pair[0])
    return [hit for _, hit in ordered[:limit]]


def merge_layer_hits(result_sets, limit):
    """Fuse per-layer Qdrant rankings without favouring the largest layer."""
    ranked = [[hit.get("id") for hit in hits if hit.get("id") is not None]
              for hits in result_sets]
    fused = reciprocal_rank_fusion(*ranked)
    by_id = {}
    for hits in result_sets:
        for hit in hits:
            point_id = hit.get("id")
            if point_id is None:
                continue
            previous = by_id.get(point_id)
            if previous is None or (hit.get("score") or 0) > (previous.get("score") or 0):
                by_id[point_id] = hit
    ordered = sorted(by_id.values(), key=lambda hit: -fused.get(hit.get("id"), 0))
    return ordered[:limit]
