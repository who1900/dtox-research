#!/usr/bin/env python3
"""Build a query-blind, reproducible SPLADE Stage A corpus from local state."""

import argparse
import hashlib
import heapq
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = (ROOT / "eval/paraphrase_cases_20261003.json",
            ROOT / "eval/paraphrase_holdout_20261003.json")
LAYERS = ("web3", "agents", "llm", "builder-tech")
COLLECTION = "papers_coarse_base"
PER_LAYER = 1000
MAX_BACKGROUND = 4000
BATCH = 128
DIM = 768


def sentence_truncate(text, limit=800):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind(sep) for sep in (". ", ".\n", "! ", "? "))
    if best > limit * 0.4:
        return cut[:best + 1].strip()
    end = cut.rfind(" ")
    return cut[:end].strip() if end > 0 else cut.strip()


def primary_layer(value):
    layers = [x.strip().lower() for x in (value or "").split(",") if x.strip()]
    aliases = {"llm-slm": "llm", "ai-agents": "agents"}
    normalized = {aliases.get(layer, layer) for layer in layers}
    for layer in LAYERS:
        if layer in normalized:
            return layer
    return None


def rank_key(seed, paper_id):
    return hashlib.sha256(f"{seed}\0{paper_id}".encode("utf-8")).hexdigest()


def reservoir_add(heaps, bucket, row, seed, limit=PER_LAYER):
    """Keep the lexicographically-smallest deterministic hash ranks."""
    key = rank_key(seed, row["canonical_id"])
    heap = heaps[bucket]
    reverse = tuple(-ord(c) for c in key)
    item = (reverse, row["arxiv_id"], row)
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def fixture_data(paths):
    queries, targets, fixture_hashes = [], {}, {}
    for path in paths:
        raw = path.read_bytes()
        fixture_hashes[path.name] = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        for group in data.get("groups", []):
            for phrase in group.get("phrases", []):
                query = phrase.get("query")
                if isinstance(query, str) and query not in queries:
                    queries.append(query)
            for field in ("targets", "hard_negatives"):
                for item in group.get(field, []):
                    canonical = item.get("canonical_id") or item.get("id")
                    aliases = item.get("aliases", [])
                    ids = [canonical] + aliases + ([item["id"]] if item.get("id") else [])
                    if isinstance(canonical, str) and canonical:
                        targets.setdefault(canonical, set()).update(
                            value for value in ids if isinstance(value, str) and value)
    if len(queries) != 33:
        raise ValueError(f"expected frozen 33 fixture queries, found {len(queries)}")
    return queries, targets, fixture_hashes


def _canonical(arxiv_id, twins):
    return twins.get(arxiv_id, arxiv_id)


def validate_twins(data):
    mapping = data.get("canonical") if isinstance(data, dict) else None
    if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in mapping.items()):
        raise ValueError("twins.json canonical must map string IDs to string canonical IDs")
    return mapping


def reverse_aliases(twins):
    grouped = {}
    for alias, canonical in twins.items():
        grouped.setdefault(canonical, []).append(alias)
    return {canonical: tuple(sorted(ids)) for canonical, ids in grouped.items()}


def _load_population(db_path, twins, reverse_aliases, excluded, seed, deadline):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    counts, heaps, seen = Counter(), {layer: [] for layer in LAYERS}, set()
    corpus_hash = hashlib.sha256()
    try:
        for row in conn.execute("SELECT arxiv_id,title,abstract,layers,year FROM papers WHERE status='done' ORDER BY arxiv_id"):
            if time.monotonic() >= deadline:
                raise TimeoutError("export exceeded 180-second total budget while streaming SQLite")
            values = dict(row)
            packed = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            corpus_hash.update(len(packed).to_bytes(8, "big")); corpus_hash.update(packed)
            layer = primary_layer(values["layers"])
            if layer is None:
                counts["unknown_layers"] += 1
                continue
            counts[layer] += 1
            if layer not in LAYERS:
                continue
            aid = values["arxiv_id"]
            canonical = _canonical(aid, twins)
            if aid in excluded or canonical in excluded:
                counts["excluded_anchor_or_alias"] += 1
                continue
            if canonical in seen:
                if aid == canonical:
                    for old_layer in LAYERS:
                        for index, item in enumerate(heaps[old_layer]):
                            if item[2]["canonical_id"] == canonical:
                                heaps[old_layer].pop(index)
                                heapq.heapify(heaps[old_layer])
                                values.update({"canonical_id": canonical,
                                               "aliases": tuple(sorted(set(reverse_aliases.get(canonical, ())) | {canonical})),
                                               "_layer": layer})
                                reservoir_add(heaps, layer, values, seed)
                                break
                continue
            seen.add(canonical)
            values["canonical_id"] = canonical
            values["aliases"] = tuple(sorted(set(reverse_aliases.get(canonical, ())) | {canonical}))
            values["_layer"] = layer
            reservoir_add(heaps, layer, values, seed)
    finally:
        conn.close()
    quotas = {layer: min(PER_LAYER, len(heaps[layer])) for layer in LAYERS}
    selected = []
    for layer in LAYERS:
        selected.extend(item[2] for item in sorted(heaps[layer], reverse=True)[:quotas[layer]])
    return selected, dict(counts), quotas, corpus_hash.hexdigest()


def _point_id(arxiv_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "coarse:" + arxiv_id))


def _anchor_rows(db_path, ids):
    rows = {}
    if not ids:
        return rows
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for start in range(0, len(ids), 800):
            batch = ids[start:start + 800]
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(
                    f"SELECT arxiv_id,title,abstract,layers,year FROM papers WHERE arxiv_id IN ({marks})",
                    batch):
                rows[row["arxiv_id"]] = dict(row)
    finally:
        conn.close()
    return rows


def _retrieve(session, base, ids, deadline):
    out = {}
    for start in range(0, len(ids), BATCH):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("document retrieval exceeded 180-second budget")
        response = session.post(f"{base}/collections/{COLLECTION}/points",
                                json={"ids": ids[start:start + BATCH], "with_payload": True,
                                      "with_vector": True}, timeout=min(15, left))
        response.raise_for_status()
        for point in response.json().get("result", []):
            out[str(point.get("id"))] = point
    return out


def _vector(point):
    value = point.get("vector")
    if isinstance(value, dict):
        value = value.get("dense", next(iter(value.values())) if len(value) == 1 else None)
    if not isinstance(value, list) or len(value) != DIM or any(not isinstance(x, (int, float)) for x in value):
        return None
    return value


def _query_vectors(session, url, queries, deadline):
    result = {}
    end = min(deadline, time.monotonic() + 45)
    for query in queries:
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError("query embedding exceeded 45-second budget")
        response = session.post(url, json={"text": query, "query": True}, timeout=min(15, left))
        response.raise_for_status()
        vector = response.json().get("vector")
        if not isinstance(vector, list) or len(vector) != DIM or any(not isinstance(x, (int, float)) for x in vector):
            raise ValueError("embed service returned a non-768-dimensional query vector")
        result[query] = vector
    return result


def _text(title, abstract):
    title, abstract = (title or "").strip(), (abstract or "").strip()
    short = len(abstract) < 80
    body = sentence_truncate(abstract) if not short else ""
    return (f"{title}. {body}" if title and body else title or body), short


def export(args):
    deadline = time.monotonic() + 180
    queries, anchors, fixture_hashes = fixture_data(FIXTURES)
    twins_raw = args.twins.read_bytes()
    twins_data = json.loads(twins_raw)
    twins = validate_twins(twins_data)
    aliases_by_canonical = reverse_aliases(twins)
    grouped = {}
    for values in anchors.values():
        for aid in values:
            canonical = _canonical(aid, twins)
            grouped.setdefault(canonical, set()).add(aid)
    anchors = {canonical: ids | set(aliases_by_canonical.get(canonical, ())) | {canonical}
               for canonical, ids in grouped.items()}
    excluded = set(anchors)
    for canonical, values in anchors.items():
        excluded.update(values)
        excluded.update(aliases_by_canonical.get(canonical, ()))
    background, population, quotas, corpus_sha = _load_population(args.db, twins, aliases_by_canonical,
                                                                    excluded, args.seed, deadline)
    candidates = {c: sorted(ids, key=lambda x: (x != c, x)) for c, ids in anchors.items()}
    anchor_db = _anchor_rows(args.db, sorted({aid for ids in anchors.values() for aid in ids}))
    selected = background + [{"canonical_id": c, "aliases": sorted(ids), "arxiv_id": ordered[0],
                              **anchor_db.get(ordered[0], {}), "_anchor": True, "_ids": ordered}
                             for c, ids in candidates.items() for ordered in [ids]]
    for row in selected:
        if not row.get("_anchor"):
            row["_ids"] = [row["arxiv_id"]] + [x for x in row["aliases"] if x != row["arxiv_id"]]
    qdrant_ids = [_point_id(alias) for row in selected for alias in row["_ids"]]
    session = requests.Session()
    session.trust_env = False
    points = _retrieve(session, args.qdrant.rstrip("/"), qdrant_ids, deadline)
    rows, missing, short_count, strata_missing = [], 0, 0, Counter()
    presence, aliases_present = {}, {}
    for row in selected:
        point = next((points.get(_point_id(aid)) for aid in row["_ids"] if points.get(_point_id(aid))), None)
        if row.get("_anchor"):
            presence[row["canonical_id"]] = point is not None
            aliases_present[row["canonical_id"]] = sorted(a for a in row["_ids"] if _point_id(a) in points)
        if point is None or _vector(point) is None:
            missing += 1
            if not row.get("_anchor"):
                strata_missing[row["_layer"]] += 1
            continue
        payload = point.get("payload") or {}
        actual = payload.get("arxiv_id") or next(aid for aid in row["_ids"] if _point_id(aid) == str(point["id"]))
        if row.get("_anchor"):
            canonical = row["canonical_id"]
            title, abstract = row.get("title") or payload.get("title") or "", row.get("abstract") or ""
            layers_value = row.get("layers") or payload.get("layers") or []
            layers = ([x.strip() for x in layers_value.split(",") if x.strip()]
                      if isinstance(layers_value, str) else layers_value)
            year = row.get("year") or payload.get("year")
            citations = payload.get("in_citations")
        else:
            title, abstract = row["title"] or "", row["abstract"] or ""
            layers = [x.strip() for x in (row["layers"] or "").split(",") if x.strip()]
            year = row["year"]
            citations = payload.get("in_citations")
        text, short = _text(title, abstract)
        short_count += int(short)
        rows.append({"chunk_id": str(point["id"]), "paper_id": row["canonical_id"], "arxiv_id": actual,
                     "title": title, "text": text, "dense_vector": _vector(point), "abstract": abstract,
                     "layers": layers if isinstance(layers, list) else [], "year": year,
                     "in_citations": citations, "text_input_uncertain": short,
                     "aliases": aliases_present.get(row.get("canonical_id"), row.get("aliases", []))})
    query_vectors = _query_vectors(session, args.embed_url, queries, deadline)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    corpus_digest = hashlib.sha256()
    with args.out.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            stream.write(line)
            corpus_digest.update(line.encode("utf-8"))
    args.queries.parent.mkdir(parents=True, exist_ok=True)
    args.queries.write_text(json.dumps(query_vectors, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ready = missing == 0 and len(rows) == len(selected) and all(presence.get(c, False) for c in anchors)
    manifest = {"ready": ready, "format": "splade-papers-stage-a-v1", "seed": args.seed,
                "records": len(rows), "background_records": sum(1 for r in selected if not r.get("_anchor") and any(x["paper_id"] == r["canonical_id"] for x in rows)),
                "anchor_records": len(anchors), "expected_anchor_presence": presence,
                "missing_anchors": sorted(c for c in anchors if not presence.get(c, False)),
                "anchor_alias_ids_present": aliases_present, "population_counts": population,
                "sampling_quotas": quotas, "id_pool_coverage": {"requested": len(qdrant_ids), "retrieved": len(points)},
                "missing_vector_count": missing, "missing_vector_by_stratum": dict(strata_missing),
                "short_abstract_count": short_count, "fixture_sha256": fixture_hashes,
                "corpus_sha256": corpus_digest.hexdigest(), "population_sha256": corpus_sha,
                "twins_sha256": hashlib.sha256(twins_raw).hexdigest(),
                "query_json_sha256": hashlib.sha256(args.queries.read_bytes()).hexdigest(),
                "query_count": len(queries), "query_vectors_sha256": hashlib.sha256(json.dumps(query_vectors, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/opt/dtox-research/state.db"))
    parser.add_argument("--twins", type=Path, default=Path("/opt/dtox-research/twins.json"))
    parser.add_argument("--qdrant", default="http://127.0.0.1:16335")
    parser.add_argument("--embed-url", default="http://127.0.0.1:16337/embed")
    parser.add_argument("--out", type=Path, required=True, help="Output corpus JSONL")
    parser.add_argument("--queries", type=Path, required=True, help="Output exact fixture query vectors JSON")
    parser.add_argument("--manifest", type=Path, help="Default: <out>.manifest.json")
    parser.add_argument("--seed", default="splade-stageA-v1")
    args = parser.parse_args()
    if os.name == "posix":
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            cap = 512 * 1024 * 1024
            new_hard = cap if hard < 0 else min(hard, cap)
            resource.setrlimit(resource.RLIMIT_AS, (min(cap, new_hard), new_hard))
        except (ImportError, OSError, ValueError):
            pass
        try:
            os.nice(15)
        except OSError:
            pass
        try:
            import subprocess
            subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            pass
    if args.manifest is None:
        args.manifest = Path(str(args.out) + ".manifest.json")
    if args.qdrant != "http://127.0.0.1:16335" or args.embed_url != "http://127.0.0.1:16337/embed":
        parser.error("only the configured anonymous loopback endpoints are permitted")
    manifest = export(args)
    print(json.dumps({"ready": manifest["ready"], "records": manifest["records"], "manifest": str(args.manifest)}))


if __name__ == "__main__":
    main()
