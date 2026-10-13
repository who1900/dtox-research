#!/usr/bin/env python3
"""Offline paper-level dense, FTS5, SPLADE and fixed-RRF recall comparison."""
import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from api.search_core import fts_query_any, reciprocal_rank_fusion as core_rrf

sys.path.insert(0, str(ROOT / "eval"))
import splade_compare as helpers

CHANNEL_BUDGET = 40
FINAL_BUDGET = 40
PRODUCTION_DENSE_BUDGET = 40
PRODUCTION_LEXICAL_BUDGET = 15
PRODUCTION_WEB3_LEXICAL_FETCH = 120
RRF_K = 60
RRF_WEIGHTS = {"dense": 2.0, "fts5": 0.8, "splade": 1.0}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _query_map(path, kind):
    raw = _read_json(path)
    if isinstance(raw, dict) and kind == "sparse" and "query_vectors" in raw:
        raw = raw["query_vectors"]
    elif isinstance(raw, dict) and "queries" in raw:
        raw = raw["queries"]
    if isinstance(raw, list):
        mapped = {}
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("query"), str):
                raise ValueError(f"{kind} query records need query and vector fields")
            vector = row.get("vector", row.get("query_vector", row.get("dense_vector" if kind == "dense" else "sparse_vector")))
            if vector is None or row["query"] in mapped:
                raise ValueError(f"{kind} query records need unique queries and vectors")
            mapped[row["query"]] = vector
        raw = mapped
    if not isinstance(raw, dict):
        raise ValueError(f"{kind} queries must be a JSON object or list")
    result = {}
    for query, vector in raw.items():
        if not isinstance(query, str):
            raise ValueError(f"{kind} query key must be text")
        if kind == "dense":
            values = np.asarray(vector, dtype=np.float64).reshape(-1)
            if values.size != 768 or not np.isfinite(values).all():
                raise ValueError(f"dense query vectors must contain 768 finite values: {query!r}")
            result[query] = values
        else:
            indices, values = helpers._sparse_parts(vector)
            result[query] = {"indices": indices, "values": values}
    return result


def _layers(value):
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip().strip('"\'') for item in value.strip("[]").replace("|", ",").split(",") if item.strip().strip('"\'')}
    return set()


def _canonical(row):
    return str(row.get("paper_id") or row.get("canonical_id") or row.get("arxiv_id") or "").strip().lower()


def _dedupe_corpus(rows):
    unique, aliases = [], {}
    seen = {}
    for row in rows:
        paper = _canonical(row)
        if not paper:
            raise ValueError("every corpus paper needs a canonical paper_id")
        identity = helpers._identity(row) | {paper}
        if paper in seen:
            aliases[paper].update(identity)
            continue
        record = dict(row)
        record["paper_id"] = paper
        unique.append(record)
        seen[paper] = record
        aliases[paper] = identity
    return unique, aliases


def _target_sets(group):
    targets, negatives = helpers._target_data(group)
    return targets, negatives


def _eligible(rows, layer):
    return [i for i, row in enumerate(rows) if layer in _layers(row.get("layers"))]


def _rank(scores, ids, limit=CHANNEL_BUDGET):
    return sorted(scores, key=lambda key: (-scores[key], ids[key]))[:limit]


def _fuse(rankings, weights):
    scores = defaultdict(float)
    for channel, ranked in rankings.items():
        for rank, paper in enumerate(ranked, 1):
            scores[paper] += weights[channel] / (RRF_K + rank)
    return scores


def _build_fts(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED, title, abstract, layers UNINDEXED, year UNINDEXED, tokenize='porter unicode61')")
    for row in rows:
        layer_blob = "|" + "|".join(sorted(_layers(row.get("layers")))) + "|"
        conn.execute("INSERT INTO papers_fts(arxiv_id,title,abstract,layers,year) VALUES(?,?,?,?,?)", (
            _canonical(row), str(row.get("title") or ""),
            str(row.get("abstract") or ""), layer_blob,
            str(row.get("year") or "")))
    return conn


def _fts_rank(conn, query, layer, limit=CHANNEL_BUDGET, source_balance=False, fetch_limit=None):
    match = fts_query_any(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT arxiv_id FROM papers_fts WHERE papers_fts MATCH ? AND layers LIKE ? "
        "ORDER BY bm25(papers_fts, 0, 3, 1, 0, 0) LIMIT ?",
        (match, f"%|{layer}|%", fetch_limit or limit),
    ).fetchall()
    ids = [row[0] for row in rows]
    if source_balance and layer == "web3":
        whitepapers = [paper for paper in ids if paper.startswith("wp:")][:2]
        standards = [paper for paper in ids if paper.startswith(("eip:", "simd:"))][:2]
        source_scores = core_rrf(ids, whitepapers, standards)
        ids = sorted(ids, key=lambda paper: -source_scores[paper])
    return ids[:limit]


def _metrics(ranked, targets, negatives):
    target_rank = next((i for i, paper in enumerate(ranked, 1) if paper in targets), None)
    negative_ranks = [(paper, i) for i, paper in enumerate(ranked, 1) if paper in negatives]
    alerts = [{"paper_id": paper, "rank": rank} for paper, rank in negative_ranks
              if rank <= 10 and (target_rank is None or rank < target_rank)]
    return {"target_rank": target_rank,
            "hit@10": bool(target_rank is not None and target_rank <= 10),
            "hit@40": bool(target_rank is not None and target_rank <= 40),
            "mrr@40": 1.0 / target_rank if target_rank is not None and target_rank <= 40 else 0.0,
            "comparator_alerts": alerts}


def _incremental_target_hit(target_ids, baseline_ids, expanded_ids):
    return not bool(set(target_ids) & set(baseline_ids)) and bool(set(target_ids) & set(expanded_ids))


def evaluate(corpus, fixtures, dense_queries, sparse_queries, source_balance=True, citation_prior=0.03):
    rows, identities = _dedupe_corpus(corpus)
    ids = [_canonical(row) for row in rows]
    positions = {paper: i for i, paper in enumerate(ids)}
    matrix = np.asarray([row.get("dense_vector", []) for row in rows], dtype=np.float64)
    if matrix.shape != (len(rows), 768) or not np.isfinite(matrix).all():
        raise ValueError("corpus dense_vector matrix must have shape (papers, 768) and finite values")
    norms = np.linalg.norm(matrix, axis=1)
    matrix = np.divide(matrix, norms[:, None], out=np.zeros_like(matrix), where=norms[:, None] != 0)
    sparse_postings = defaultdict(list)
    short_abstracts = 0
    for i, row in enumerate(rows):
        for term, weight in zip(*helpers._sparse_parts(row.get("sparse_vector"))):
            sparse_postings[term].append((i, weight))
        abstract = str(row.get("abstract") or "")
        short_abstracts += len(abstract) < 80
    fts = _build_fts(rows)
    results = []
    channel_seconds = defaultdict(float)
    all_queries = helpers._fixture_queries(fixtures)
    missing_dense = sorted({item["phrase"]["query"] for item in all_queries} - set(dense_queries))
    missing_sparse = sorted({item["phrase"]["query"] for item in all_queries} - set(sparse_queries))
    if missing_dense or missing_sparse:
        raise ValueError(f"query vectors missing: dense={len(missing_dense)}, sparse={len(missing_sparse)}")
    for item in all_queries:
        group, phrase = item["group"], item["phrase"]
        query, layer = phrase["query"], str(group.get("layer") or "")
        eligible = _eligible(rows, layer)
        eligible_ids = {ids[i] for i in eligible}
        rankings = {}
        started = time.perf_counter()
        q = np.asarray(dense_queries[query], dtype=np.float64).reshape(-1)
        if q.size != 768 or not np.isfinite(q).all():
            raise ValueError(f"dense query vector must contain 768 finite values: {query!r}")
        dense_scores = matrix[eligible] @ (q / np.linalg.norm(q) if np.linalg.norm(q) else q)
        dense_by_paper = {ids[i]: float(dense_scores[j]) for j, i in enumerate(eligible)}
        raw_dense = _rank(dense_by_paper, positions)
        raw_dense = raw_dense[:PRODUCTION_DENSE_BUDGET]
        dense_adjusted = {}
        for paper in raw_dense:
            citations = max(0, int(rows[positions[paper]].get("in_citations") or 0))
            citation_factor = min(1.0, math.log1p(citations) / math.log1p(1000))
            dense_adjusted[paper] = dense_by_paper[paper] + float(citation_prior) * citation_factor
        rankings["dense"] = sorted(raw_dense, key=lambda paper: (-dense_adjusted[paper], positions[paper]))
        channel_seconds["dense"] += time.perf_counter() - started
        started = time.perf_counter()
        indices, values = helpers._sparse_parts(sparse_queries[query])
        sparse_scores = defaultdict(float)
        allowed = set(eligible)
        for term, weight in zip(indices, values):
            for index, doc_weight in sparse_postings.get(term, ()):
                if index in allowed:
                    sparse_scores[ids[index]] += weight * doc_weight
        rankings["splade"] = _rank(sparse_scores, positions)
        channel_seconds["splade"] += time.perf_counter() - started
        started = time.perf_counter()
        rankings["fts5"] = _fts_rank(fts, query, layer, source_balance=source_balance,
                                      fetch_limit=PRODUCTION_WEB3_LEXICAL_FETCH if source_balance and layer == "web3" else CHANNEL_BUDGET)
        stage_a_lexical = _fts_rank(fts, query, layer, limit=PRODUCTION_LEXICAL_BUDGET,
                                    source_balance=source_balance,
                                    fetch_limit=PRODUCTION_WEB3_LEXICAL_FETCH if source_balance and layer == "web3" else PRODUCTION_LEXICAL_BUDGET)
        channel_seconds["fts5"] += time.perf_counter() - started
        for name, channels in {
            "rrf_dense_fts5": ("dense", "fts5"),
            "rrf_dense_splade": ("dense", "splade"),
            "rrf_all": ("dense", "fts5", "splade"),
        }.items():
            weights = {channel: RRF_WEIGHTS[channel] for channel in channels}
            fused = _fuse({channel: rankings[channel] for channel in channels}, weights)
            rankings[name] = _rank(fused, positions, FINAL_BUDGET)
        targets_raw, negatives_raw = _target_sets(group)
        target_ids = {paper for paper, aliases in identities.items()
                      if any(aliases & target for target in targets_raw)}
        target_specs = {str(target.get("canonical_id", "")).strip().lower():
                        {str(target.get("canonical_id", "")).strip().lower(),
                         *(str(alias).strip().lower() for alias in target.get("aliases", []) if alias)}
                        for target in group.get("targets", []) if target.get("canonical_id")}
        target_canonicals = sorted(target_specs)
        eligible_identity = set().union(*(identities[ids[i]] for i in eligible)) if eligible else set()
        negative_ids = {paper for paper, aliases in identities.items()
                        if aliases & negatives_raw or paper in negatives_raw}
        method_metrics = {name: _metrics(ranking, target_ids, negative_ids)
                          for name, ranking in rankings.items()}
        union_base = set(rankings["dense"]) | set(rankings["fts5"])
        union_all = union_base | set(rankings["splade"])
        production_baseline_ids = list(dict.fromkeys(rankings["dense"][:PRODUCTION_DENSE_BUDGET] + stage_a_lexical))
        production_with_splade_ids = list(dict.fromkeys(production_baseline_ids + rankings["splade"]))
        present_targets = sorted(target_ids & eligible_ids)
        results.append({
            "fixture": item["fixture"], "group_id": group.get("id"),
            "phrase_id": phrase.get("id"), "layer": layer,
            "eligible_count": len(eligible), "target_present": bool(present_targets),
            "target_ids": target_canonicals,
            "missing_target_ids": sorted(target for target, target_set in target_specs.items()
                                           if not (eligible_identity & target_set)),
            "methods": method_metrics, "ranked_ids": rankings,
            "candidate_coverage": {"dense_fts5_union_count": len(union_base),
                "dense_fts5_splade_union_count": len(union_all),
                "dense_fts5_target_in_union": bool(target_ids & union_base),
                "dense_fts5_splade_target_in_union": bool(target_ids & union_all),
                "incremental_target_hit": _incremental_target_hit(target_ids, union_base, union_all)},
            "production_stage_a": {"baseline_candidate_count": len(production_baseline_ids),
                "with_splade_candidate_count": len(production_with_splade_ids),
                "incremental_candidate_count": len(set(production_with_splade_ids) - set(production_baseline_ids)),
                "dense_candidate_ids": rankings["dense"][:PRODUCTION_DENSE_BUDGET],
                "lexical_candidate_ids": stage_a_lexical,
                "baseline_candidate_ids": production_baseline_ids,
                "with_splade_candidate_ids": production_with_splade_ids,
                "baseline_target_in_union": bool(target_ids & set(production_baseline_ids)),
                "with_splade_target_in_union": bool(target_ids & set(production_with_splade_ids)),
                "incremental_target_in_union": _incremental_target_hit(
                    target_ids, production_baseline_ids, production_with_splade_ids)},
        })
    fts.close()
    method_names = list(results[0]["methods"]) if results else []

    def summarize(subset):
        summary = {}
        for method in method_names:
            values = [row["methods"][method] for row in subset]
            denom = len(values)
            summary[method] = {"denominator": denom,
                "hit@10": sum(x["hit@10"] for x in values) / denom if denom else 0.0,
                "hit@40": sum(x["hit@40"] for x in values) / denom if denom else 0.0,
                "mrr@40": sum(x["mrr@40"] for x in values) / denom if denom else 0.0,
                "comparator_alerts": sum(len(x["comparator_alerts"]) for x in values)}
        return summary

    paired = {}
    for left, right in (("rrf_dense_fts5", "rrf_all"), ("rrf_dense_fts5", "rrf_dense_splade")):
        win = loss = tie = recovered = regressed = 0
        for row in results:
            a, b = row["methods"][left]["target_rank"], row["methods"][right]["target_rank"]
            a_rank, b_rank = a if a is not None else math.inf, b if b is not None else math.inf
            win += b_rank < a_rank
            loss += b_rank > a_rank
            tie += b_rank == a_rank
            recovered += (a is None or a > 40) and b is not None and b <= 40
            regressed += a is not None and a <= 40 and (b is None or b > 40)
        paired[f"{left}_vs_{right}"] = {"denominator": len(results), "right_wins": win,
            "right_losses": loss, "ties": tie, "incremental_target_in40": recovered,
            "target_lost_from40": regressed}
    groups = defaultdict(list)
    for row in results:
        groups[(row["fixture"], row["group_id"])].append(row)
    group_summaries = {}
    for (fixture, group), group_rows in groups.items():
        group_summaries[f"{fixture}:{group}"] = {method: {
            "phrases": len(group_rows),
            "all_phrases_hit@10": all(row["methods"][method]["hit@10"] for row in group_rows),
            "all_phrases_hit@40": all(row["methods"][method]["hit@40"] for row in group_rows)}
            for method in method_names}
    def gate_for(subset):
        baseline_alerts = sum(len(row["methods"]["rrf_dense_fts5"]["comparator_alerts"]) for row in subset)
        treatment_alerts = sum(len(row["methods"]["rrf_all"]["comparator_alerts"]) for row in subset)
        baseline10 = sum(row["methods"]["rrf_dense_fts5"]["hit@10"] for row in subset)
        treatment10 = sum(row["methods"]["rrf_all"]["hit@10"] for row in subset)
        baseline40 = sum(row["methods"]["rrf_dense_fts5"]["hit@40"] for row in subset)
        treatment40 = sum(row["methods"]["rrf_all"]["hit@40"] for row in subset)
        ok_alerts = treatment_alerts <= baseline_alerts
        positive = treatment10 > baseline10 or treatment40 > baseline40
        return {"denominator": len(subset), "baseline_comparator_alerts": baseline_alerts,
            "with_splade_comparator_alerts": treatment_alerts,
            "baseline_hit@10": baseline10, "with_splade_hit@10": treatment10,
            "baseline_hit@40": baseline40, "with_splade_hit@40": treatment40,
            "no_comparator_regression": ok_alerts, "positive_incremental_hit": positive,
            "decision": "PASS" if ok_alerts and positive else "HOLD_NO_DEPLOY"}
    holdout_rows = [row for row in results if "holdout" in row["fixture"].lower()]
    gate_overall, gate_holdout = gate_for(results), gate_for(holdout_rows)
    gate_pass = gate_overall["decision"] == "PASS" and (not holdout_rows or gate_holdout["no_comparator_regression"])
    coverage = {name: {"phrases": len([r for r in results if r["fixture"] == name]),
                       "metrics": summarize([r for r in results if r["fixture"] == name])}
                for name, _ in fixtures}
    return {"notice": "Offline known-source retrieval recall only; no answer verdict, precision claim, or production search simulation.",
        "denominator_policy": "All supplied frozen fixture phrases are included, including layer-eligible corpus-missing targets.",
        "channel_candidate_budget": CHANNEL_BUDGET, "final_candidate_budget": FINAL_BUDGET,
        "fair_comparator_budget_notice": "Dense, FTS5 and SPLADE each use top-40; this is a fair offline comparator, not the production Stage A shortlist.",
        "production_stage_a": {"dense_papers": PRODUCTION_DENSE_BUDGET,
            "lexical_papers": PRODUCTION_LEXICAL_BUDGET,
            "web3_lexical_fetch_limit": PRODUCTION_WEB3_LEXICAL_FETCH if source_balance else PRODUCTION_LEXICAL_BUDGET,
            "web3_source_reorder": bool(source_balance), "web3_reorder_caps": {"whitepapers": 2, "standards": 2}},
        "rrf": {"k": RRF_K, "weights": RRF_WEIGHTS},
        "source_balance": {"enabled": bool(source_balance), "production_reorder": "whitepapers/specs"},
        "citation_prior": {"weight": float(citation_prior), "formula": "weight * min(1, log1p(max(in_citations,0))/log1p(1000))", "applied_after_raw_dense_top40": True},
        "deployment_gate": {"comparison": "rrf_dense_fts5 baseline vs rrf_all with SPLADE",
            "overall": gate_overall, "holdout": gate_holdout if holdout_rows else None,
            "decision": "PASS" if gate_pass else "HOLD_NO_DEPLOY",
            "rule": "No increase in hard-negative comparator alerts overall or on holdout, plus positive incremental target hit@10 or hit@40; mixed results are HOLD_NO_DEPLOY."},
        "corpus": {"input_records": len(corpus), "canonical_papers": len(rows),
                   "deduplicated_records": len(corpus) - len(rows),
                   "abstract_missing_or_short_lt80_count": int(short_abstracts),
                   "abstract_length_note": "Input abstracts under 80 characters, including empty; not a verified completeness test."},
        "summary": summarize(results), "per_fixture": coverage, "per_group": group_summaries,
        "paired_rank_comparison": paired,
        "candidate_union": {"dense_fts5_vs_dense_fts5_splade": {
            "phrases": len(results),
            "baseline_union_recall": sum(r["candidate_coverage"]["dense_fts5_target_in_union"] for r in results) / len(results) if results else 0.0,
            "expanded_union_recall": sum(r["candidate_coverage"]["dense_fts5_splade_target_in_union"] for r in results) / len(results) if results else 0.0,
            "incremental_target_union_hits": sum(r["candidate_coverage"]["incremental_target_hit"] for r in results),
            "mean_union_candidates_baseline": sum(r["candidate_coverage"]["dense_fts5_union_count"] for r in results) / len(results) if results else 0.0,
            "mean_union_candidates_expanded": sum(r["candidate_coverage"]["dense_fts5_splade_union_count"] for r in results) / len(results) if results else 0.0}},
        "channel_retrieval_seconds": dict(channel_seconds), "phrases": results}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--fixture", action="append", required=True)
    parser.add_argument("--dense-queries", required=True)
    parser.add_argument("--sparse-queries", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--no-source-balance", action="store_true")
    parser.add_argument("--citation-prior", type=float, default=0.03)
    args = parser.parse_args(argv)
    corpus = helpers._jsonl(args.corpus)
    fixtures = [(Path(path).name, _read_json(path)) for path in args.fixture]
    dense = _query_map(args.dense_queries, "dense")
    sparse = _query_map(args.sparse_queries, "sparse")
    report = evaluate(corpus, fixtures, dense, sparse, not args.no_source_balance, args.citation_prior)
    report["input_sha256"] = {"corpus": _sha256(args.corpus), "dense_queries": _sha256(args.dense_queries),
                              "sparse_queries": _sha256(args.sparse_queries),
                              "fixtures": {Path(path).name: _sha256(path) for path in args.fixture}}
    report["input_paths"] = {"corpus": str(Path(args.corpus)), "dense_queries": str(Path(args.dense_queries)),
                             "sparse_queries": str(Path(args.sparse_queries)),
                             "fixtures": [str(Path(path)) for path in args.fixture]}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phrases": len(report["phrases"]), "methods": report["summary"],
                      "output": str(Path(args.output))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
