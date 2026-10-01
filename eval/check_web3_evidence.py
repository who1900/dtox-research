#!/usr/bin/env python3
"""Tiny golden retrieval/structure check. Offline unless --network --api are set.

Only POST /v1/search and GET /v1/paper/{canonical_id}/spec are permitted.
Expected structural elements are assertions, not claimed live observations;
no Qdrant point/chunk IDs are invented or used.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent / "bench" / "web3_evidence.jsonl"
KNOWN_IDS = {"eip:7702", "eip:4337", "eip:4844", "simd:0085", "simd:0297",
             "wp:chainlink-v2", "iacr:2018/601"}
ELEMENT_TYPES = {"algorithm", "equation", "code", "table"}


def load_bench(path=BENCH):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if not 1 <= len(rows) <= 10:
        raise ValueError("golden bench must contain 1..10 rows")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("qid"), str)
                or not row["qid"] or row["qid"] in seen
                or row.get("expected_id") not in KNOWN_IDS
                or not isinstance(row.get("query"), str) or not row["query"].strip()
                or type(row.get("max_rank")) is not int or not 1 <= row["max_rank"] <= 10):
            raise ValueError("invalid/duplicate golden row or noncanonical ID")
        seen.add(row["qid"])
        if "baseline" in row:
            baseline = row["baseline"]
            rank = baseline.get("rank")
            if (not isinstance(baseline.get("source"), str)
                    or (rank is not None and (type(rank) is not int or not 1 <= rank <= 10))):
                raise ValueError("invalid reported baseline")
        if "structure" in row:
            spec = row["structure"]
            if (not isinstance(spec, dict) or type(spec.get("min_elements")) is not int
                    or not 1 <= spec["min_elements"] <= 10
                    or not isinstance(spec.get("types_any"), list) or not spec["types_any"]
                    or not all(t in ELEMENT_TYPES for t in spec["types_any"])):
                raise ValueError("invalid structural assertion")
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_base(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("--api must be an HTTP(S) origin without credentials/path/query")
    parsed.port  # validate a malformed port before any request
    return value.rstrip("/")


def request_json(base, method, path, body=None, timeout=15, api_key=None):
    spec_paths = {"/v1/paper/" + urllib.parse.quote(pid, safe="") + "/spec?max_chars=12000"
                  for pid in KNOWN_IDS}
    if not ((method == "POST" and path == "/v1/search")
            or (method == "GET" and path in spec_paths)):
        raise ValueError("endpoint is not read-only allowlisted")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(base + path, data=payload, method=method,
                                     headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("response exceeds bounded read")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    return data


def structural_pass(data, row):
    if data.get("arxiv_id") != row["expected_id"]:
        return False
    sections = data.get("sections")
    if not isinstance(sections, list) or len(sections) < row["structure"]["min_elements"]:
        return False
    allowed = row["structure"]["types_any"]
    valid = all(isinstance(section, dict)
               and {"element_type", "section_type", "section_title", "text"} <= section.keys()
               and section["element_type"] in ELEMENT_TYPES | {"prose"}
               and isinstance(section["text"], str) and bool(section["text"].strip())
               for section in sections)
    return valid and sum(s["element_type"] in allowed for s in sections) >= row["structure"]["min_elements"]


def run_case(row, base, timeout=15, fetch=None):
    fetch = fetch or request_json
    start = time.monotonic()
    result = {"qid": row["qid"], "expected_id": row["expected_id"], "rank": None,
              "known_expected_pass": False, "structural_pass": None,
              "errors": [], "partial": False, "context_truncated": False, "timing_ms": {},
              "baseline": row.get("baseline"), "baseline_hit_at3": None,
              "known_extraction_gap": row.get("known_extraction_gap")}
    if "baseline" in row:
        baseline_rank = row["baseline"]["rank"]
        result["baseline_hit_at3"] = baseline_rank is not None and baseline_rank <= 3
    for phase in ("search", "structure"):
        if phase == "structure" and "structure" not in row:
            continue
        phase_start = time.monotonic()
        try:
            if phase == "search":
                data = fetch(base, "POST", "/v1/search",
                             {"query": row["query"], "layer": "web3", "limit": 10}, timeout)
                hits = data.get("results")
                if not isinstance(hits, list) or not all(isinstance(h, dict) for h in hits):
                    raise ValueError("invalid results")
                result["rank"] = next((i for i, hit in enumerate(hits, 1)
                                       if hit.get("arxiv_id") == row["expected_id"]), None)
            else:
                path = "/v1/paper/" + urllib.parse.quote(row["expected_id"], safe="")
                data = fetch(base, "GET", path + "/spec?max_chars=12000", None, timeout)
                result["structural_pass"] = structural_pass(data, row)
            result["partial"] |= bool(data.get("partial"))
            result["context_truncated"] |= bool(data.get("truncated"))
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            # Never echo server bodies, URLs or credentials from exception text.
            detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            result["errors"].append({"phase": phase, "detail": detail})
        finally:
            result["timing_ms"][phase] = round((time.monotonic() - phase_start) * 1000, 3)
    rank = result["rank"]
    result["hit_at3"] = rank is not None and rank <= 3
    result["hit_at10"] = rank is not None and rank <= 10
    result["rank_delta"] = (rank - row["baseline"]["rank"]
                            if "baseline" in row and rank is not None
                            and row["baseline"]["rank"] is not None else None)
    result["known_expected_pass"] = (rank is not None and rank <= row["max_rank"]
                                     and result["structural_pass"] is not False
                                     and not result["errors"] and not result["partial"])
    result["timing_ms"]["total"] = round((time.monotonic() - start) * 1000, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="explicitly opt into read-only HTTP")
    parser.add_argument("--api", help="explicit HTTP(S) origin; no credentials")
    parser.add_argument("--api-key-env", help="explicit environment variable holding existing REST key")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60 or not 1 <= args.limit <= 10:
        parser.error("timeout must be 0..60 seconds and limit 1..10")
    if args.network != bool(args.api):
        parser.error("live checks require both --network and --api")
    if args.api_key_env and not args.network:
        parser.error("authentication is read only with explicit --network --api")
    try:
        rows = load_bench()[:args.limit]
        base = api_base(args.api) if args.network else None
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"invalid configuration/fixture ({type(exc).__name__})")
    if not args.network:
        print(json.dumps({"mode": "offline-validation", "network": False, "cases": len(rows),
                          "known_expected_pass": None, "note": "fixture valid; live evidence not checked"}))
        return 0
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    if args.api_key_env and not api_key:
        parser.error("selected auth environment variable is empty")

    def fetch(origin, method, path, body, timeout):
        return request_json(origin, method, path, body, timeout, api_key=api_key)

    results = [run_case(row, base, args.timeout, fetch=fetch) for row in rows]
    print(json.dumps({"mode": "read-only-network", "results": results,
                      "known_expected_pass": sum(r["known_expected_pass"] for r in results),
                      "errors": sum(bool(r["errors"]) for r in results),
                      "partial": sum(r["partial"] for r in results)}, indent=2))
    return 0 if all(r["known_expected_pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
