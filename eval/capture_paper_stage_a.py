#!/usr/bin/env python3
"""Read-only full-index observer for the production paper Stage A channels."""
import argparse
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

DIM, DENSE_LIMIT, LEX_LIMIT, WEB3_LEX_FETCH = 768, 40, 15, 120
RRF_K, GLOBAL_DEADLINE, FTS_SECONDS = 60, 90, 2
STOPWORDS = frozenset("a an and are as at be been but by can do does for from has have how in into is it its of on or our such that the their them then there these they this to use used uses using was we were what when where which while who why will with within without you your".split())


def fts_query_any(text, max_terms=16):
    import re
    words, seen = [], set()
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", text or ""):
        low = word.lower()
        if len(low) > 2 and low not in STOPWORDS and low not in seen:
            seen.add(low)
            words.append(word)
    return " OR ".join(f'"{word}"' for word in words[:max_terms])


def _canon(paper_id, twins):
    return str(twins.get(str(paper_id), paper_id))


def _validate_qdrant(endpoint):
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Qdrant observer only allows loopback port 16335") from error
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or port != 16335 or parsed.username or parsed.password:
        raise ValueError("Qdrant observer only allows loopback port 16335")
    return endpoint.rstrip("/")


def load_queries(paths, twins):
    records, queries, hashes = [], set(), {}
    for path in paths:
        raw = Path(path).read_bytes()
        fixture = json.loads(raw)
        hashes[Path(path).name] = hashlib.sha256(raw).hexdigest()
        for group in fixture.get("groups", []):
            targets, negatives = set(), set()
            for item in group.get("targets", []):
                targets.update(_canon(x, twins) for x in [item.get("canonical_id"), item.get("id"), *item.get("aliases", [])] if x)
            for item in group.get("hard_negatives", []):
                negatives.update(_canon(x, twins) for x in [item.get("canonical_id"), item.get("id"), *item.get("aliases", [])] if x)
            for phrase in group.get("phrases", []):
                query = phrase.get("query")
                if not isinstance(query, str) or not query.strip() or query in queries:
                    raise ValueError("fixture phrase needs a unique non-empty query")
                queries.add(query)
                records.append({"fixture": Path(path).name, "group_id": group.get("id"),
                    "phrase_id": phrase.get("id"), "query": query,
                    "layer": str(group.get("layer") or ""), "targets": sorted(targets),
                    "hard_negatives": sorted(negatives)})
    if len(records) != 33 or len(queries) != 33:
        raise ValueError(f"expected 33 unique fixture phrases, found {len(records)}")
    return records, hashes


def _dense(session, endpoint, vector, layer, twins, deadline):
    body = {"vector": vector, "filter": {"must": [{"key": "layers", "match": {"any": [layer]}}]},
            "limit": DENSE_LIMIT, "with_payload": ["arxiv_id", "title", "in_citations"], "with_vector": False}
    try:
        response = session.post(endpoint + "/collections/papers_coarse_base/points/search",
                                json=body, timeout=min(5, max(0.1, deadline-time.monotonic())), allow_redirects=False)
        response.raise_for_status()
        raw = []
        for hit in response.json().get("result", []):
            payload = hit.get("payload") or {}
            aid = payload.get("arxiv_id")
            if not aid:
                continue
            citations = max(0, payload.get("in_citations") or 0)
            score = float(hit.get("score") or 0) + 0.03 * min(1, math.log1p(citations) / math.log1p(1000))
            raw.append((score, str(aid)))
        source_ids = [aid for _, aid in raw]
        raw.sort(key=lambda item: -item[0])
        prior_ids = [aid for _, aid in raw]
        canonical = list(dict.fromkeys(_canon(aid, twins) for aid in prior_ids))
        return source_ids, prior_ids, canonical, None
    except Exception as error:
        return None, None, None, f"{type(error).__name__}: {error}"


def _lexical(conn, query, layer, twins, deadline):
    match = fts_query_any(query)
    if not match:
        return []
    sql = ("SELECT arxiv_id FROM papers_fts WHERE papers_fts MATCH ? AND layers LIKE ? "
           "AND rank MATCH 'bm25(0, 3.0, 1.0, 0, 0)' ORDER BY rank LIMIT ?")
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        ids = [row[0] for row in conn.execute(sql, (match, f"%{layer}%", WEB3_LEX_FETCH if layer == "web3" else LEX_LIMIT)) if row[0]]
    finally:
        conn.set_progress_handler(None, 0)
    if layer != "web3":
        return ids[:LEX_LIMIT]
    wp = [aid for aid in ids if aid.startswith("wp:")][:2]
    standards = [aid for aid in ids if aid.startswith(("eip:", "simd:"))][:2]
    score = {}
    for ranked in (ids, wp, standards):
        for rank, aid in enumerate(ranked, 1):
            score[aid] = score.get(aid, 0.0) + 1 / (RRF_K + rank)
    return sorted(ids, key=lambda aid: -score[aid])[:LEX_LIMIT]


def _pilot_additions(path, twins):
    if not path:
        return {}, "not_requested"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    found = {}
    for row in data.get("phrases", []):
        key = (row.get("fixture"), row.get("group_id"), row.get("phrase_id"))
        found[key] = list(dict.fromkeys(_canon(x, twins) for x in row.get("ranked_ids", {}).get("splade", [])[:40]))
    return found, "sampled_4000_restricted_pool_not_full_corpus_or_splade_ann"


def capture(fixtures, query_path, twins_path, fts_path, qdrant, output, pilot_path=None):
    qdrant = _validate_qdrant(qdrant)
    twins_raw = Path(twins_path).read_bytes()
    twins_hash = hashlib.sha256(twins_raw).hexdigest()
    twins_data = json.loads(twins_raw).get("canonical", {})
    del twins_raw
    if not isinstance(twins_data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in twins_data.items()):
        raise ValueError("twins canonical must map string IDs to string IDs")
    queries, fixture_hashes = load_queries(fixtures, twins_data)
    dense_raw = Path(query_path).read_bytes()
    query_hash = hashlib.sha256(dense_raw).hexdigest()
    vectors = json.loads(dense_raw)
    del dense_raw
    if isinstance(vectors, dict) and "queries" in vectors:
        vectors = vectors["queries"]
    if not isinstance(vectors, dict):
        raise ValueError("cached dense queries must be a plain query-to-vector object")
    missing = [row["query"] for row in queries if row["query"] not in vectors]
    if missing:
        raise ValueError(f"missing cached 768-D vectors for {len(missing)} fixture queries")
    for row in queries:
        vec = vectors[row["query"]]
        if not isinstance(vec, list) or len(vec) != DIM or any(not math.isfinite(float(x)) for x in vec):
            raise ValueError(f"invalid cached 768-D vector for {row['query']!r}")
    pilot, pilot_scope = _pilot_additions(pilot_path, twins_data)
    try:
        conn = sqlite3.connect(f"file:{Path(fts_path).resolve().as_posix()}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        conn = None
    session, results = requests.Session(), []
    session.trust_env = False
    global_deadline = time.monotonic() + GLOBAL_DEADLINE
    try:
        for row in queries:
            layer = row["layer"]
            if time.monotonic() >= global_deadline:
                dense_source, dense_prior, dense, dense_error = None, None, None, "global 90s budget exhausted"
                lexical_source, lexical, lex_error = None, None, "global 90s budget exhausted"
            else:
                dense_source, dense_prior, dense, dense_error = _dense(
                    session, qdrant, vectors[row["query"]], layer, twins_data, global_deadline)
                try:
                    lexical_source = _lexical(conn, row["query"], layer, twins_data,
                        min(global_deadline, time.monotonic()+FTS_SECONDS)) if conn else None
                    lexical = list(dict.fromkeys(_canon(aid, twins_data) for aid in lexical_source)) if lexical_source is not None else None
                    lex_error = None if conn else "FTS database unavailable"
                except sqlite3.Error as error:
                    lexical_source, lexical, lex_error = None, None, f"{type(error).__name__}: {error}"
            union = sorted(set(dense or []) | set(lexical or [])) if dense is not None and lexical is not None else None
            targets, negatives = set(row["targets"]), set(row["hard_negatives"])
            pilot_ids = pilot.get((row["fixture"], row["group_id"], row["phrase_id"]), [])
            added = bool(union is not None and not (targets & set(union)) and targets & set(pilot_ids))
            results.append({**row, "raw_dense_source_ids": dense_source,
                "prior_reordered_dense_source_ids": dense_prior, "canonical_raw_dense": dense,
                "raw_lex_source_ids": lexical_source, "raw_lex": lexical,
                "union_stage_a_55": union,
                "target_hit": (bool(targets & set(union)) if union is not None else None),
                "hard_negative_present": (sorted(negatives & set(union)) if union is not None else None),
                "dense_error": dense_error, "fts_error": lex_error,
                "pilot_splade_added_missing_target": added if pilot_path else None})
    finally:
        session.close()
        if conn:
            conn.close()
    report = {"scope": "full-index bounded read-only Stage A observer; no judgments or production-latency claim",
        "counts": {"queries": len(results), "dense_limit_raw": DENSE_LIMIT, "lexical_limit": LEX_LIMIT,
                   "union_max": DENSE_LIMIT+LEX_LIMIT}, "fixture_sha256": fixture_hashes,
        "query_vectors_sha256": query_hash, "twins_sha256": twins_hash,
        "timeouts_seconds": {"global_capture": GLOBAL_DEADLINE, "fts_query": FTS_SECONDS, "qdrant_request": 5},
        "pilot_splade_scope": pilot_scope,
        "phrases": results}
    out = Path(output); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="append", required=True)
    parser.add_argument("--queries", required=True, help="exported plain query -> cached 768-D vector JSON")
    parser.add_argument("--twins", required=True)
    parser.add_argument("--db", default="/opt/dtox-research/papers_fts.db")
    parser.add_argument("--qdrant", default="http://127.0.0.1:16335")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pilot-results", help="optional offline sampled-corpus SPLADE comparison JSON")
    args = parser.parse_args(argv)
    report = capture(args.fixture, args.queries, args.twins, args.db, args.qdrant, args.output, args.pilot_results)
    print(json.dumps({"queries": report["counts"]["queries"], "output": str(args.output),
                      "dense_errors": sum(bool(x["dense_error"]) for x in report["phrases"]),
                      "fts_errors": sum(bool(x["fts_error"]) for x in report["phrases"])}))


if __name__ == "__main__":
    main()
