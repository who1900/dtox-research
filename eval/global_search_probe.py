"""Isolated diagnostic runner for paraphrase fixtures; emits one JSON document."""

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CASES = Path(__file__).with_name("paraphrase_holdout_20261003.json")
QUERY_TIMEOUT_SECONDS = 22


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _progress(message):
    print(message, file=sys.stderr, flush=True)


def _load_cases(path, maximum):
    raw = path.read_bytes()
    fixture = json.loads(raw.decode("utf-8"))
    canonical = json.dumps(fixture, sort_keys=True).encode()
    selected = []
    for group in fixture["groups"]:
        for phrase in group["phrases"]:
            if len(selected) >= maximum:
                return fixture, hashlib.sha256(canonical).hexdigest(), selected
            selected.append((group, phrase))
    return fixture, hashlib.sha256(canonical).hexdigest(), selected


def _set_nonsecret_environment(variant):
    config = {
        "QDRANT_URL": "http://127.0.0.1:16335",
        "COARSE_EMBED_URL": "http://127.0.0.1:16337/embed",
        "EMBED_URL": "http://127.0.0.1:8006/embed",
        "QDRANT_COLLECTION": "papers_fulltext",
        "COARSE_COLLECTION": "papers_coarse_base",
        "PAPER_FTS_PATH": "/opt/dtox-research/papers_fts.db",
        "HIER_SEARCH": "1",
        "PAPER_LEXICAL": "1",
        "HIER_FUSION": "rrf",
        "HIER_W_DENSE": "2",
        "HIER_W_LEX": "0.8",
        "HIER_W_GLOBAL": "1",
        "HIER_CHUNK_LEXICAL": "0",
        "PAPER_LEXICAL_SOURCE_BALANCE": "1",
        "HIER_GROUPED_SEARCH": "1" if variant == "candidate" else "0",
        "HIER_RANK_CHANNELS": "1" if variant == "candidate" else "0",
        "API_KEYS_PATH": "/__codex_probe_no_api_keys__",
    }
    os.environ.update(config)
    return config


class _QueryTimeout(BaseException):
    pass


def _run_with_timeout(call, timeout):
    """Bound each query; a daemon worker lets Windows exit even if blocked."""
    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGALRM)

        def alarm_handler(_signum, _frame):
            raise _QueryTimeout("query exceeded per-query timeout")

        signal.signal(signal.SIGALRM, alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return call()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    finished = threading.Event()
    value = {}

    def worker():
        try:
            value["result"] = call()
        except BaseException as exc:  # captured and serialized by the caller
            value["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if not finished.wait(timeout):
        raise _QueryTimeout("query exceeded per-query timeout; worker left daemonized")
    if "error" in value:
        raise value["error"]
    return value.get("result")


def _import_main(api_dir):
    api_path = str(api_dir.resolve())
    if api_path not in sys.path:
        sys.path.insert(0, api_path)
    import main  # noqa: PLC0415 - env must be set before importing production module
    return main


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", required=True, type=Path,
                        help="directory containing the selected main.py")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--variant", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--max-queries", type=int, default=24)
    parser.add_argument("--budget-seconds", type=float, default=120)
    parser.add_argument("--output", type=Path,
                        help="optional path for the complete machine-readable JSON result")
    parser.add_argument("--rank-channels", choices=("0", "1"), default=None)
    parser.add_argument("--grouped-search", choices=("0", "1"), default=None)
    args = parser.parse_args()
    if args.budget_seconds < 0:
        parser.error("--budget-seconds must be non-negative")

    started_utc = _utc_now()
    started_clock = time.monotonic()
    budget_seconds = max(0.0, min(args.budget_seconds, 120.0))
    deadline = started_clock + budget_seconds
    fixture, fixture_hash, cases = _load_cases(args.cases, max(0, min(args.max_queries, 24)))
    _progress(f"loaded {len(cases)} queries; variant={args.variant}; fixture_sha256={fixture_hash}")

    output = {
        "fixture": fixture,
        "fixture_sha256": fixture_hash,
        "fixture_hash_algorithm": "sha256(json.dumps(fixture,sort_keys=True).encode()).hexdigest()",
        "started_utc": started_utc,
        "ended_utc": None,
        "variant": args.variant,
        "runner_operational": True,
        "config": {
            "api_dir": str(args.api_dir.resolve()),
            "max_queries": max(0, min(args.max_queries, 24)),
            "budget_seconds": budget_seconds,
        },
        "raw_results": [],
    }
    env_config = _set_nonsecret_environment(args.variant)
    if args.rank_channels is not None:
        env_config["HIER_RANK_CHANNELS"] = args.rank_channels
        os.environ["HIER_RANK_CHANNELS"] = args.rank_channels
    if args.grouped_search is not None:
        env_config["HIER_GROUPED_SEARCH"] = args.grouped_search
        os.environ["HIER_GROUPED_SEARCH"] = args.grouped_search
    output["config"].update(env_config)

    try:
        # The caller selects the production api directory for baseline and the
        # isolated copied api directory for candidate; never import both.
        if not (args.api_dir / "main.py").is_file():
            raise FileNotFoundError(f"main.py not found under --api-dir: {args.api_dir}")
        app = _import_main(args.api_dir)
        for index, (group, phrase) in enumerate(cases, 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _progress("hard budget reached; stopping before next query")
                break
            query_started = time.monotonic()
            _progress(f"query {index}/{len(cases)}: {group['id']}/{phrase['id']}")
            record = {
                "group_id": group["id"],
                "phrase_id": phrase["id"],
                "query": phrase["query"],
                "layer": group["layer"],
                "raw_payload": None,
                "results": [],
                "partial": False,
                "error": None,
                "latency_ms": None,
            }
            try:
                body = app.SearchBody(query=phrase["query"], layer=group["layer"],
                                      compact=True, hybrid=True, limit=10)
                payload = _run_with_timeout(
                    lambda: app._run_search(body),
                    max(0.05, min(QUERY_TIMEOUT_SECONDS, remaining)),
                )
                record["raw_payload"] = payload
                record["results"] = payload.get("results", []) if isinstance(payload, dict) else []
                record["partial"] = bool(payload.get("partial")) if isinstance(payload, dict) else False
            except BaseException as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, _QueryTimeout):
                    record["partial"] = True
                    output["runner_operational"] = False
            finally:
                record["latency_ms"] = round((time.monotonic() - query_started) * 1000, 2)
                output["raw_results"].append(record)
            if time.monotonic() >= deadline:
                _progress("hard budget reached after query")
                break
    except BaseException as exc:
        output["runner_error"] = f"{type(exc).__name__}: {exc}"
        _progress(f"runner error: {type(exc).__name__}: {exc}")
    finally:
        output["ended_utc"] = _utc_now()
        output["elapsed_seconds"] = round(time.monotonic() - started_clock, 3)
        rendered = json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str)
        if args.output is not None:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered + "\n", encoding="utf-8")
                stdout_payload = {
                    "output": str(args.output),
                    "variant": args.variant,
                    "count": len(output["raw_results"]),
                    "elapsed": output["elapsed_seconds"],
                    "runner_operational": output["runner_operational"],
                }
                rendered = json.dumps(stdout_payload, ensure_ascii=False, separators=(",", ":"))
            except OSError as exc:
                output["runner_operational"] = False
                output["runner_error"] = f"output write failed: {type(exc).__name__}: {exc}"
                _progress(output["runner_error"])
                rendered = json.dumps({
                    "output": str(args.output),
                    "variant": args.variant,
                    "count": len(output["raw_results"]),
                    "elapsed": output["elapsed_seconds"],
                    "runner_operational": False,
                    "error": output["runner_error"],
                }, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write(rendered)
        sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main_cli()
