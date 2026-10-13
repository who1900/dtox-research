#!/usr/bin/env python3
"""Export a bounded, reproducible Qdrant corpus for offline SPLADE evaluation."""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


COLLECTION = "papers_fulltext"
FIXTURES = (
    "paraphrase_cases_20261003.json",
    "paraphrase_holdout_20261003.json",
)
PAGE_SIZE = 256
PAPER_CAP = 20
USEFUL_SECTIONS = {
    "method": 0,
    "limitations": 1,
    "introduction": 2,
    "experiments": 3,
    "results": 4,
    "conclusion": 5,
    "abstract": 6,
}


def _sha(seed, point_id):
    return hashlib.sha256(f"{seed}\0{point_id}".encode("utf-8")).hexdigest()


def _fixture_ids(eval_dir):
    requested = set()
    hashes = {}
    for filename in FIXTURES:
        path = eval_dir / filename
        raw = path.read_bytes()
        hashes[filename] = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        for group in data.get("groups", []):
            for key in ("targets", "hard_negatives"):
                for item in group.get(key, []):
                    for value in (item.get("canonical_id"), item.get("id")):
                        if isinstance(value, str) and value.strip():
                            requested.add(value.strip())
                    for alias in item.get("aliases", []):
                        if isinstance(alias, str) and alias.strip():
                            requested.add(alias.strip())
    return sorted(requested), hashes


def _vector(point):
    value = point.get("vector")
    if isinstance(value, dict):
        value = value.get("dense")
        if value is None and len(point["vector"]) == 1:
            value = next(iter(point["vector"].values()))
    if not isinstance(value, list) or len(value) != 384:
        return None
    if any(not isinstance(x, (int, float)) for x in value):
        return None
    return value


def _record(point, allow_other=False):
    payload = point.get("payload") or {}
    point_id = point.get("id")
    paper_id = payload.get("arxiv_id")
    text = payload.get("text")
    vector = _vector(point)
    if point_id is None or not isinstance(paper_id, str) or not paper_id:
        return None
    if not isinstance(text, str) or not text.strip() or vector is None:
        return None
    section = str(payload.get("section_type") or "").strip().lower()
    if section not in USEFUL_SECTIONS and not allow_other:
        return None
    return {
        "chunk_id": str(point_id),
        "paper_id": paper_id,
        "text": text,
        "layers": payload.get("layers", []),
        "title": payload.get("title"),
        "dense": vector,
        "section_type": payload.get("section_type"),
    }


def _choose(points, seed, limit, paper_cap, anchors=False):
    by_paper = defaultdict(list)
    seen = set()
    for point in points:
        record = _record(point, allow_other=anchors)
        if record is None or record["chunk_id"] in seen:
            continue
        seen.add(record["chunk_id"])
        section = str(record["section_type"]).lower()
        rank = USEFUL_SECTIONS.get(section, 99)
        if anchors:
            order = (rank, int((point.get("payload") or {}).get("chunk_index") or 0),
                     _sha(seed, record["chunk_id"]))
        else:
            order = (_sha(seed, record["chunk_id"]), rank)
        by_paper[record["paper_id"]].append((order, record))

    selected = []
    if anchors:
        for paper_id in sorted(by_paper):
            selected.extend(item[1] for item in sorted(by_paper[paper_id])[:paper_cap])
        selected.sort(key=lambda row: (row["paper_id"], row["chunk_id"]))
        return selected[:limit]

    ranked = []
    for paper_id, entries in by_paper.items():
        for order, record in entries:
            ranked.append((order[0], paper_id, record["chunk_id"], record))
    counts = Counter()
    for _, paper_id, _, record in sorted(ranked):
        if counts[paper_id] >= paper_cap:
            continue
        selected.append(record)
        counts[paper_id] += 1
        if len(selected) >= limit:
            break
    return selected


def _fixture_queries(eval_dir):
    queries = []
    seen = set()
    for filename in FIXTURES:
        data = json.loads((eval_dir / filename).read_text(encoding="utf-8"))
        for group in data.get("groups", []):
            for phrase in group.get("phrases", []):
                query = phrase.get("query")
                if isinstance(query, str) and query not in seen:
                    seen.add(query)
                    queries.append(query)
    if len(queries) > 33:
        raise ValueError(f"Found {len(queries)} unique fixture queries; hard limit is 33")
    return queries


def _embed_queries(embed_url, queries, deadline):
    parsed = urlsplit(embed_url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username or parsed.password):
        raise ValueError("--embed-url must be anonymous HTTP on loopback")
    if not parsed.path.endswith("/embed"):
        raise ValueError("--embed-url must point to the /embed endpoint")
    vectors = {}
    embed_deadline = min(deadline, time.monotonic() + 45.0)
    for query in queries:
        remaining = embed_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("query embedding exceeded its 45-second budget")
        request = Request(
            embed_url,
            data=json.dumps({"text": query, "query": True}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=min(2.0, remaining)) as response:
                body = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"query embedding failed for a fixture query: {exc}") from exc
        vector = body.get("vector")
        if (not isinstance(vector, list) or len(vector) != 384
                or any(not isinstance(value, (int, float)) for value in vector)):
            raise ValueError("embed service returned a vector other than 384 numeric values")
        vectors[query] = vector
    return vectors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", required=True,
                        help="Anonymous loopback Qdrant base URL, e.g. http://127.0.0.1:6333")
    parser.add_argument("--out", type=Path, required=True, help="JSONL output path")
    parser.add_argument("--manifest", type=Path,
                        help="Manifest path (default: <out>.manifest.json)")
    parser.add_argument("--embed-url",
                        help="Optional anonymous loopback query embed endpoint (e.g. http://127.0.0.1:8006/embed)")
    parser.add_argument("--query-output", type=Path,
                        help="JSON mapping exact fixture query strings to 384-D vectors")
    parser.add_argument("--seed", default="splade-corpus-v1")
    parser.add_argument("--max-scan", type=int, default=3000)
    parser.add_argument("--max-background", type=int, default=1000)
    parser.add_argument("--paper-cap", type=int, default=20)
    parser.add_argument("--max-total", type=int, default=1600)
    args = parser.parse_args()

    if not (1 <= args.max_scan <= 3000):
        parser.error("--max-scan must be between 1 and 3000")
    if not (1 <= args.max_background <= 1000):
        parser.error("--max-background must be between 1 and 1000")
    if not (10 <= args.paper_cap <= 20):
        parser.error("--paper-cap must be between 10 and 20")
    if not (1 <= args.max_total <= 1600):
        parser.error("--max-total must be between 1 and 1600")
    if bool(args.embed_url) != bool(args.query_output):
        parser.error("--embed-url and --query-output must be provided together")
    base = args.qdrant_url.rstrip("/")
    qdrant = urlsplit(base)
    if (qdrant.scheme != "http" or qdrant.hostname not in ("127.0.0.1", "localhost")
            or qdrant.username or qdrant.password):
        parser.error("Only anonymous loopback Qdrant URLs are allowed")

    eval_dir = Path(__file__).resolve().parent
    requested, fixture_hashes = _fixture_ids(eval_dir)
    endpoint = f"{base}/collections/{COLLECTION}/points/scroll"
    deadline = time.monotonic() + 120.0
    anchor_filter = {"must": [{"key": "arxiv_id", "match": {"any": requested}}]}
    anchor_points = []
    anchor_scan = 0
    anchor_pages = 0
    offset = None
    while anchor_scan < args.max_scan:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("120-second export budget exhausted during anchor scroll")
        body = {
            "filter": anchor_filter,
            "limit": min(PAGE_SIZE, args.max_scan - anchor_scan),
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        request = Request(endpoint, data=json.dumps(body, separators=(",", ":")).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=max(0.1, min(15.0, remaining))) as response:
                result = json.loads(response.read())["result"]
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"Anchor Qdrant scroll failed: {exc}") from exc
        points = result.get("points", [])
        anchor_points.extend(points)
        anchor_scan += len(points)
        anchor_pages += 1
        next_offset = result.get("next_page_offset")
        if not points or next_offset is None or next_offset == offset:
            break
        offset = next_offset

    anchors = _choose(anchor_points, args.seed, args.max_total, args.paper_cap, anchors=True)
    found_ids = {str((point.get("payload") or {}).get("arxiv_id")) for point in anchor_points}
    raw_scan_absent_ids = [paper_id for paper_id in requested if paper_id not in found_ids]
    selected_anchor_ids = {row["paper_id"] for row in anchors}
    selected_anchor_absent_ids = [paper_id for paper_id in requested
                                  if paper_id not in selected_anchor_ids]
    available = max(0, args.max_total - len(anchors))
    background_limit = min(args.max_background, available)
    bg_filter = {"must_not": [{"key": "arxiv_id", "match": {"any": requested}}]}
    background_points = []
    background_pages = 0
    background_scan = 0
    offset = None
    while background_scan < args.max_scan and len(background_points) < args.max_scan:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or background_limit == 0:
            break
        body = {
            "filter": bg_filter,
            "limit": min(PAGE_SIZE, args.max_scan - background_scan),
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        request = Request(endpoint, data=json.dumps(body, separators=(",", ":")).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=max(0.1, min(15.0, remaining))) as response:
                result = json.loads(response.read())["result"]
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"Background Qdrant scroll failed: {exc}") from exc
        points = result.get("points", [])
        background_points.extend(points)
        background_scan += len(points)
        background_pages += 1
        next_offset = result.get("next_page_offset")
        if not points or next_offset is None or next_offset == offset:
            break
        offset = next_offset

    background = _choose(background_points, args.seed, background_limit,
                         args.paper_cap, anchors=False)
    records = anchors + background
    records = records[:args.max_total]
    query_vectors = None
    if args.embed_url:
        query_vectors = _embed_queries(args.embed_url, _fixture_queries(eval_dir), deadline)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    corpus_digest = hashlib.sha256()
    with args.out.open("w", encoding="utf-8", newline="\n") as stream:
        for row in records:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            stream.write(line)
            corpus_digest.update(line.encode("utf-8"))

    manifest_path = args.manifest or Path(str(args.out) + ".manifest.json")
    manifest = {
        "format": "splade-qdrant-corpus-v1",
        "collection": COLLECTION,
        "corpus_sha256": corpus_digest.hexdigest(),
        "record_count": len(records),
        "anchor_record_count": len(anchors),
        "background_record_count": len(background),
        "unique_paper_count": len({row["paper_id"] for row in records}),
        "requested_anchor_ids": requested,
        "requested_anchor_ids_absent_in_bounded_scan": raw_scan_absent_ids,
        "requested_anchor_ids_absent_from_selected_anchor_records": selected_anchor_absent_ids,
        "fixture_sha256": fixture_hashes,
        "query_vectors": None,
        "seed": args.seed,
        "selection": {
            "anchor_order": "fixture IDs; known useful sections first, then chunk_index, seeded SHA256 tie-break (unknown section priority 99)",
            "background_order": "Qdrant scroll order for bounded scan; SHA256(point_id, seed) rank within scan",
            "background_is_stratified": False,
            "max_scan_per_population": args.max_scan,
            "max_background_records": args.max_background,
            "per_paper_cap": args.paper_cap,
            "max_total_records": args.max_total,
            "section_priority": list(USEFUL_SECTIONS),
            "anchor_scroll_pages": anchor_pages,
            "anchor_points_scanned": anchor_scan,
            "background_scroll_pages": background_pages,
            "background_points_scanned": background_scan,
            "limitations": [
                "Anchors are diagnostic fixtures, not a sample of the corpus population.",
                "Background is not stratified or representative; scroll order can favor lexicographically early point IDs.",
                "Deterministic hash ranking applies only to points observed inside the bounded scroll window.",
                "Missing IDs mean not observed in the bounded anchor scan, not proven absent from the collection.",
                "Background retains only method, limitations, introduction, experiments, results, conclusion, and abstract chunks with 384-D vectors.",
                "Unknown section types are excluded from background, but may be retained for anchors after known useful sections when needed to fill the per-paper cap.",
                "Raw document text is exported unchanged; embed-service tokenizer truncation applies only when queries are embedded.",
            ],
        },
    }
    if query_vectors is not None:
        args.query_output.parent.mkdir(parents=True, exist_ok=True)
        query_json = json.dumps(query_vectors, ensure_ascii=False, separators=(",", ":")) + "\n"
        args.query_output.write_text(query_json, encoding="utf-8")
        manifest["query_vectors"] = {
            "path": str(args.query_output),
            "count": len(query_vectors),
            "sha256": hashlib.sha256(query_json.encode("utf-8")).hexdigest(),
            "embed_url": args.embed_url,
            "request_body": "{text: exact fixture query, query: true}",
            "serial_request_limit": 33,
            "budget_seconds": 45,
        }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(json.dumps({"corpus": str(args.out), "manifest": str(manifest_path),
                      "records": len(records), "sha256": corpus_digest.hexdigest()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
