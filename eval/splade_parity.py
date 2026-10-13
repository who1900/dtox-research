#!/usr/bin/env python3
"""Offline parity probe for cached Qdrant ONNX CPU and SPLADE CUDA/FP16."""
import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path

import numpy as np


DEFAULT_TORCH_CACHE = "C:/Users/User/Documents/work/Claude/.scratch_dtox/splade-20261003/models"
DEFAULT_ONNX_DIR = (
    "C:/Users/User/Documents/work/Claude/.scratch_dtox/splade-20261003/models/"
    "models--Qdrant--Splade_PP_en_v1/snapshots/"
    "600ce23fd7611e71694fcf724f46778f1ad24c1c"
)
TORCH_SOURCE = "prithivida/Splade_PP_en_v1"
EXPECTED_QUERIES = 33
TIME_LIMIT_SECONDS = 120


class DeadlineExceeded(TimeoutError):
    pass


def _check_deadline(started):
    if time.monotonic() - started >= TIME_LIMIT_SECONDS:
        raise DeadlineExceeded(f"global {TIME_LIMIT_SECONDS}s time limit reached")


def _sha256(path, started):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            _check_deadline(started)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root, started):
    root = Path(root)
    if not root.exists():
        return {"path": str(root), "files": {}, "error": "path_not_found"}
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        _check_deadline(started)
        files[str(path.relative_to(root))] = _sha256(path, started)
    return {"path": str(root), "files": files}


def _read_corpus(path, started):
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            _check_deadline(started)
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                raise ValueError(f"corpus line {line_no} must contain a text string")
            rows.append(row)
    if not rows:
        raise ValueError("corpus has no records")
    return rows


def _extract_queries(data):
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict) and isinstance(data.get("queries"), list):
        values = data["queries"]
    elif isinstance(data, dict) and isinstance(data.get("groups"), list):
        values = [phrase for group in data["groups"]
                  for phrase in group.get("phrases", [])]
    elif isinstance(data, dict) and data and all(isinstance(key, str) for key in data):
        values = list(data)
    else:
        raise ValueError("queries JSON must be a list, contain 'queries', or contain fixture 'groups'")
    queries = []
    seen = set()
    for value in values:
        query = value if isinstance(value, str) else value.get("query") if isinstance(value, dict) else None
        if isinstance(query, str) and query.strip() and query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def _sample_corpus(rows, count=5):
    if len(rows) <= count:
        return rows
    return [rows[(i * (len(rows) - 1)) // (count - 1)] for i in range(count)]


def _sample_indices(length, count=5):
    if length <= count:
        return list(range(length))
    return [(i * (length - 1)) // (count - 1) for i in range(count)]


def _metrics(left, right):
    pairs = []
    nnz_left, nnz_right = [], []
    max_abs = 0.0
    for a, b in zip(left, right):
        av = dict(zip(a.indices, a.values))
        bv = dict(zip(b.indices, b.values))
        nnz_left.append(len(av)); nnz_right.append(len(bv))
        keys = av.keys() | bv.keys()
        max_abs = max(max_abs, max((abs(av.get(k, 0.0) - bv.get(k, 0.0)) for k in keys), default=0.0))
        dot = sum(av[k] * bv.get(k, 0.0) for k in av)
        anorm = math.sqrt(sum(v * v for v in av.values()))
        bnorm = math.sqrt(sum(v * v for v in bv.values()))
        pairs.append(dot / (anorm * bnorm) if anorm and bnorm else (1.0 if not anorm and not bnorm else 0.0))
    return {
        "count": len(pairs),
        "pair_cosine": pairs,
        "pair_cosine_min": min(pairs) if pairs else None,
        "pair_cosine_mean": sum(pairs) / len(pairs) if pairs else None,
        "max_abs_difference": max_abs if pairs else None,
        "onnx_nnz": nnz_left,
        "torch_nnz": nnz_right,
    }


def _latency_summary(samples):
    values = sorted(samples)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}

    def percentile(p):
        return values[max(0, math.ceil(p * len(values)) - 1)] * 1000

    return {"count": len(values), "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95), "max_ms": values[-1] * 1000}


def _process_rss():
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except (ImportError, OSError):
        return None


def _find_torch_snapshot(cache_root):
    root = Path(cache_root) / "models--prithivida--Splade_PP_en_v1" / "snapshots"
    if not root.is_dir():
        return None
    refs = Path(cache_root) / "models--prithivida--Splade_PP_en_v1" / "refs" / "main"
    if refs.is_file():
        revision = refs.read_text(encoding="utf-8").strip()
        candidate = root / revision
        if candidate.is_dir():
            return candidate
    snapshots = sorted((p for p in root.iterdir() if p.is_dir()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    return snapshots[0] if snapshots else None


def _make_onnx_encoder(onnx_dir):
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(onnx_dir), local_files_only=True, use_fast=True
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    root = Path(onnx_dir)
    candidates = (root / "model.onnx", root / "onnx" / "model.onnx",
                  root / "model_quantized.onnx", root / "onnx" / "model_quantized.onnx")
    model_path = next((p for p in candidates if p.is_file()), None)
    if model_path is None:
        raise FileNotFoundError(f"no local ONNX model found in {root}")
    session = ort.InferenceSession(str(model_path), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    input_names = {item.name for item in session.get_inputs()}
    if not {"input_ids", "attention_mask"}.issubset(input_names):
        raise ValueError(f"unexpected ONNX input names: {sorted(input_names)}")
    input_shapes = {item.name: item.shape for item in session.get_inputs()}
    ids_shape = input_shapes["input_ids"]
    static_sequence_length = ids_shape[1] if len(ids_shape) > 1 and isinstance(ids_shape[1], int) else None
    forced_static_padding = static_sequence_length is not None

    def encode(text):
        inputs = tokenizer(text, truncation=True,
                           padding="max_length" if forced_static_padding else True,
                           max_length=static_sequence_length or 512,
                           return_tensors="np")
        feed = {}
        for name in input_names:
            if name in inputs:
                feed[name] = np.asarray(inputs[name], dtype=np.int64)
            elif name == "token_type_ids":
                feed[name] = np.zeros_like(np.asarray(inputs["input_ids"], dtype=np.int64))
            else:
                raise ValueError(f"tokenizer cannot supply required ONNX input {name!r}")
        logits = session.run(None, feed)[0]
        if logits.ndim != 3:
            raise ValueError(f"expected ONNX logits [batch, seq, vocab], got {logits.shape}")
        pooled = np.max(np.log1p(np.maximum(logits, 0)) *
                        np.asarray(inputs["attention_mask"])[..., None], axis=1)
        try:
            from splade_torch_backend import sparse_embeddings
        except ImportError:
            from eval.splade_torch_backend import sparse_embeddings
        return sparse_embeddings(pooled[0])[0]

    return encode, tokenizer, model_path, session, {
        "static_sequence_length": static_sequence_length,
        "forced_static_padding": forced_static_padding,
    }


def _timed_queries(texts, encoder, synchronize, started, progress=None):
    if texts:
        _check_deadline(started)
        encoder(texts[0])  # warm-up, excluded from latency samples
        synchronize()
    vectors, durations = [], []
    for text in texts:
        _check_deadline(started)
        synchronize()
        before = time.perf_counter()
        vector = encoder(text)
        synchronize()
        durations.append(time.perf_counter() - before)
        vectors.append(vector)
        if progress is not None:
            progress["encoded"] = len(vectors)
    return vectors, durations


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="JSONL corpus with text fields")
    parser.add_argument("--queries", required=True, help="JSON list / fixture groups, expected 33 unique queries")
    parser.add_argument("--output", required=True)
    parser.add_argument("--torch-cache", default=DEFAULT_TORCH_CACHE)
    parser.add_argument("--onnx-dir", default=DEFAULT_ONNX_DIR)
    parser.add_argument("--query-vectors-out", help="optional ONNX CPU sparse query vectors JSON")
    args = parser.parse_args()

    started = time.monotonic()
    report = {
        "status": "partial", "deadline_seconds": TIME_LIMIT_SECONDS,
        "models": {}, "corpus": {}, "queries": {}, "parity": {}, "latency": {},
        "memory": {"process_rss_bytes": _process_rss()}, "errors": [],
    }
    query_vectors_out = None
    try:
        corpus_rows = _read_corpus(args.corpus, started)
        report["corpus"] = {
            "path": str(Path(args.corpus)), "records": len(corpus_rows),
            "sha256": _sha256(args.corpus, started),
            "sample_indices": _sample_indices(len(corpus_rows)),
        }
        sampled_rows = _sample_corpus(corpus_rows)
        if len(sampled_rows) != 5:
            report["errors"].append(f"corpus has {len(sampled_rows)} records; parity target is 5")
        with open(args.queries, encoding="utf-8") as stream:
            raw_queries = json.load(stream)
        queries = _extract_queries(raw_queries)
        report["queries"] = {"path": str(Path(args.queries)), "sha256": _sha256(args.queries, started),
                             "count": len(queries), "expected_count": EXPECTED_QUERIES}
        if len(queries) != EXPECTED_QUERIES:
            report["errors"].append(f"found {len(queries)} unique queries; expected {EXPECTED_QUERIES}")

        # This process is deliberately offline: both model sources must already be cached.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        onnx_dir = Path(args.onnx_dir)
        onnx_encode, tokenizer, onnx_path, onnx_session, onnx_padding = _make_onnx_encoder(onnx_dir)
        onnx_hashes = _hash_tree(onnx_dir, started)
        report["models"]["onnx"] = {
            "backend": "onnxruntime_cpu", "model_source": "Qdrant/Splade_PP_en_v1",
            "model_revision": onnx_dir.name, "model_file": str(onnx_path),
            "inputs": [item.name for item in onnx_session.get_inputs()],
            "outputs": [item.name for item in onnx_session.get_outputs()],
            "threads": 2, "precision": "model_file_native",
            "files_sha256": onnx_hashes,
            "tokenizer_files_sha256": {
                name: digest for name, digest in onnx_hashes.get("files", {}).items()
                if "tokenizer" in name or "vocab" in name or "special_tokens" in name
            },
            "tokenizer_source": str(onnx_dir),
            **onnx_padding,
        }

        try:
            from splade_torch_backend import TorchSpladeEncoder
        except ImportError:
            from eval.splade_torch_backend import TorchSpladeEncoder
        torch_encoder = TorchSpladeEncoder(model_name=TORCH_SOURCE, cache_dir=args.torch_cache,
                                           max_length=512, precision="fp16")
        # Use the exact local Qdrant tokenizer for both paths; model provenance remains separate.
        torch_encoder.tokenizer = tokenizer
        torch = torch_encoder._torch
        report["models"]["torch"] = {
            "backend": torch_encoder.metadata["backend"],
            "model_source": torch_encoder.model_source,
            "model_revision": getattr(torch_encoder.model.config, "_commit_hash", None),
            "precision": torch_encoder.metadata["precision"], "device": torch_encoder.metadata["device"],
            "max_length": 512, "inference_batch_size": 1,
            "parity_status": "measured_below_not_assumed",
            "tokenizer_source": str(onnx_dir),
        }
        snapshot = _find_torch_snapshot(args.torch_cache)
        report["models"]["torch"]["cached_snapshot"] = str(snapshot) if snapshot else None
        if snapshot:
            report["models"]["torch"]["files_sha256"] = _hash_tree(snapshot, started)
        def gpu_encode(text):
            return next(torch_encoder.embed([text]))

        torch.cuda.synchronize()
        onnx_docs, torch_docs = [], []
        report["corpus"]["onnx_encoded"] = 0
        report["corpus"]["torch_encoded"] = 0
        for row in sampled_rows:
            _check_deadline(started)
            onnx_docs.append(onnx_encode(row["text"]))
            report["corpus"]["onnx_encoded"] = len(onnx_docs)
            torch_docs.append(gpu_encode(row["text"]))
            report["corpus"]["torch_encoded"] = len(torch_docs)
        report["parity"]["corpus"] = _metrics(onnx_docs, torch_docs)

        report["queries"]["onnx_encoded"] = 0
        report["queries"]["torch_encoded"] = 0
        onnx_queries, onnx_times = _timed_queries(
            queries, onnx_encode, lambda: None, started, report["queries"]
        )
        report["queries"]["onnx_encoded"] = len(onnx_queries)
        if args.query_vectors_out:
            query_vectors_out = {
                "backend": "onnxruntime_cpu", "model_source": "Qdrant/Splade_PP_en_v1",
                "model_revision": onnx_dir.name,
                "query_vectors": {
                    query: {"indices": vector.indices, "values": vector.values}
                    for query, vector in zip(queries, onnx_queries)
                },
            }
        # Use a separate progress mapping: the public field tracks each backend independently.
        torch_progress = {"encoded": 0}
        torch_queries, torch_times = _timed_queries(
            queries, gpu_encode, torch.cuda.synchronize, started, torch_progress
        )
        report["queries"]["torch_encoded"] = torch_progress["encoded"]
        report["parity"]["queries"] = _metrics(onnx_queries, torch_queries)
        report["latency"] = {"onnx_cpu": _latency_summary(onnx_times),
                              "torch_cuda_fp16": _latency_summary(torch_times),
                              "includes_tokenization": True, "warmup_excluded": True,
                              "comparable": not onnx_padding["forced_static_padding"],
                              "note": ("ONNX required static-shape padding; latency is not directly comparable"
                                       if onnx_padding["forced_static_padding"] else None)}
        report["memory"]["torch_cuda_peak_allocated_bytes"] = int(torch.cuda.max_memory_allocated())
        report["memory"]["torch_cuda_peak_reserved_bytes"] = int(torch.cuda.max_memory_reserved())
        report["memory"]["process_rss_bytes"] = _process_rss()
        torch_encoder.metadata["max_memory_allocated_bytes"] = report["memory"]["torch_cuda_peak_allocated_bytes"]
        report["models"]["torch"].update(torch_encoder.metadata)
        _check_deadline(started)
        report["status"] = "complete" if not report["errors"] else "partial"
    except Exception as exc:
        report["status"] = "partial"
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)[:1000]})
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if report["elapsed_seconds"] >= TIME_LIMIT_SECONDS and not report["errors"]:
            report["errors"].append(f"global {TIME_LIMIT_SECONDS}s time limit reached")
            report["status"] = "partial"
        try:
            _atomic_json(args.output, report)
            if args.query_vectors_out and query_vectors_out is not None:
                _atomic_json(args.query_vectors_out, query_vectors_out)
        except Exception as exc:
            print(json.dumps({"status": "output_write_failed", "error": str(exc)[:1000]}))
            return 2
    print(json.dumps({"status": report["status"], "elapsed_seconds": report["elapsed_seconds"],
                      "errors": report["errors"], "output": args.output}, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
