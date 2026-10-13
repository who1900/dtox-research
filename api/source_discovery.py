"""Bounded auxiliary candidates from an existing hierarchical search pool."""


def select_source_candidates(hits, primary_results, limit, floor, canonical_id,
                             result_from_hit, is_boilerplate, fusion_ranks=None, *,
                             metadata_budget=None):
    """Keep bounded leads; metadata_budget caps rows, not execution time."""
    if limit <= 0:
        return []
    if metadata_budget is not None:
        if not isinstance(metadata_budget, int) or isinstance(metadata_budget, bool):
            return []
        if metadata_budget <= 0:
            return []
        limit = min(limit, metadata_budget)
    primary_ids = {canonical_id(row.get("arxiv_id"))
                   for row in primary_results if row.get("arxiv_id")}
    slots = {}
    for index, hit in enumerate(hits):
        payload = hit.get("payload") or {}
        paper_id = payload.get("arxiv_id")
        if not paper_id:
            continue
        key = canonical_id(paper_id)
        if key in primary_ids:
            continue
        existing = slots.get(key)
        if existing is None:
            slots[key] = {"hit": hit, "index": index, "graph_rank": None}
        elif is_boilerplate(existing["hit"]) and not is_boilerplate(hit):
            existing["hit"] = hit
        rank_info = (fusion_ranks or {}).get(paper_id) or {}
        graph_rank = rank_info.get("graph")
        if graph_rank is not None:
            old_rank = slots[key]["graph_rank"]
            if old_rank is None or graph_rank < old_rank:
                slots[key]["graph_rank"] = graph_rank

    main_order = list(slots)
    graph_order = sorted((key for key, slot in slots.items()
                          if slot["graph_rank"] is not None),
                         key=lambda key: (slots[key]["graph_rank"], slots[key]["index"]))
    lanes, main_index, graph_index = {}, 0, 0
    cap = min(limit, 10)
    while len(lanes) < cap:
        progressed = False
        for lane in ("fused", "citation_graph"):
            if len(lanes) >= cap:
                break
            order = main_order if lane == "fused" else graph_order
            position = main_index if lane == "fused" else graph_index
            while position < len(order) and order[position] in lanes:
                position += 1
            if lane == "fused":
                main_index = position
            else:
                graph_index = position
            if position < len(order):
                key = order[position]
                lanes[key] = lane
                if lane == "fused":
                    main_index += 1
                else:
                    graph_index += 1
                progressed = True
        if not progressed:
            break

    candidates = []
    for key, lane in lanes.items():
        hit = slots[key]["hit"]
        candidate = result_from_hit(hit)
        score = hit.get("score")
        passes_floor = isinstance(score, (int, float)) and score >= floor
        candidate.update({
            "retrieval_role": "source_candidate",
            "candidate_reason": ("outside_primary_result_budget" if passes_floor
                                 else "fragment_below_relevance_floor"),
            "relevance_floor": floor,
            "passes_relevance_floor": passes_floor,
            "verification_required": True,
            "candidate_channel": lane,
        })
        if lane == "citation_graph":
            candidate["candidate_channel_note"] = (
                "Citation-graph ranking is an evidence lead only, never corroboration.")
        candidates.append(candidate)
    return candidates


def merge_source_candidates(primary_candidates, partner_candidates, primary_results,
                            limit, canonical_id, reciprocal_rank_fusion):
    """RRF-fuse soft-layer candidates, keeping origin metadata intact."""
    if limit <= 0:
        return []
    lists = [[row.get("arxiv_id") for row in rows if row.get("arxiv_id")]
             for rows in (primary_candidates, partner_candidates)]
    fused = reciprocal_rank_fusion(*lists)
    rows_by_id = {}
    for row in [*partner_candidates, *primary_candidates]:
        paper_id = row.get("arxiv_id")
        if paper_id:
            rows_by_id.setdefault(paper_id, row)

    excluded = {canonical_id(row.get("arxiv_id"))
                for row in primary_results if row.get("arxiv_id")}
    ordered = sorted(rows_by_id, key=lambda paper_id: (
        -fused.get(paper_id, 0),
        lists[0].index(paper_id) if paper_id in lists[0] else len(lists[0])))
    out, seen = [], set(excluded)
    for paper_id in ordered:
        key = canonical_id(paper_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(rows_by_id[paper_id])
        if len(out) >= min(max(limit, 0), 10):
            break
    return out
