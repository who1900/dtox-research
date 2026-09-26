#!/usr/bin/env python3
"""Copy points between two Qdrant instances, vectors and payload included.

Used once for the move to the Oracle host: the bulk of papers_fulltext went
over as a snapshot, and this carries what a snapshot cannot, namely the small
papers_coarse collection and the chunks the pipeline wrote after the snapshot
was taken. Point ids are kept, so a rerun is an idempotent upsert.

  qdrant_copy.py --src http://127.0.0.1:6333 --dst http://127.0.0.1:16335 \
      --collection papers_coarse --create
  qdrant_copy.py ... --collection papers_fulltext --arxiv-ids-file ids.txt
"""
import argparse
import sys
import time

import requests

BATCH = 256


def ensure_collection(src, dst, name):
    info = requests.get(f"{src}/collections/{name}", timeout=60).json()["result"]
    if requests.get(f"{dst}/collections/{name}", timeout=60).status_code == 200:
        return
    cfg = info["config"]
    body = {"vectors": cfg["params"]["vectors"],
            "on_disk_payload": cfg["params"].get("on_disk_payload", False),
            "hnsw_config": cfg["hnsw_config"]}
    if cfg.get("quantization_config"):
        body["quantization_config"] = cfg["quantization_config"]
    requests.put(f"{dst}/collections/{name}", json=body, timeout=120).raise_for_status()
    for field, schema in (info.get("payload_schema") or {}).items():
        requests.put(f"{dst}/collections/{name}/index?wait=true",
                     json={"field_name": field, "field_schema": schema["data_type"]},
                     timeout=600).raise_for_status()


def copy(src, dst, name, qfilter=None):
    offset, copied, start = None, 0, time.monotonic()
    while True:
        body = {"limit": BATCH, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        if qfilter:
            body["filter"] = qfilter
        page = requests.post(f"{src}/collections/{name}/points/scroll", json=body,
                             timeout=300).json()["result"]
        points = [{"id": p["id"], "vector": p["vector"], "payload": p["payload"]}
                  for p in page["points"]]
        if points:
            requests.put(f"{dst}/collections/{name}/points?wait=true",
                         json={"points": points}, timeout=300).raise_for_status()
            copied += len(points)
        offset = page.get("next_page_offset")
        if copied and copied % (BATCH * 40) < BATCH:
            print(f"{name}: {copied} points ({copied / (time.monotonic() - start):.0f}/s)",
                  flush=True)
        if offset is None:
            return copied


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--arxiv-ids-file")
    args = ap.parse_args()
    if args.create:
        ensure_collection(args.src, args.dst, args.collection)
    if args.arxiv_ids_file:
        ids = [l.strip() for l in open(args.arxiv_ids_file) if l.strip()]
        total = 0
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            total += copy(args.src, args.dst, args.collection,
                          {"must": [{"key": "arxiv_id", "match": {"any": chunk}}]})
        print(f"{args.collection}: {total} points for {len(ids)} papers")
    else:
        print(f"{args.collection}: {copy(args.src, args.dst, args.collection)} points")


if __name__ == "__main__":
    sys.exit(main())
