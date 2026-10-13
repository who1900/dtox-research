#!/usr/bin/env python3
"""Offline SPLADE/dense retrieval comparison over a frozen JSONL corpus."""
import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_SPLADE = "prithivida/Splade_PP_en_v1"
MAX_THREADS = 8


def _jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected object")
            rows.append(row)
    return rows


def _write_jsonl_atomic(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _manifest(corpus, model, backend="fastembed", precision="default", max_length=512,
              model_revision=None):
    corpus_hash = hashlib.sha256()
    text_hash = hashlib.sha256()
    keys = set()
    for row in corpus:
        key = str(row.get("chunk_id", ""))
        if not key or key in keys:
            raise ValueError("corpus chunk_id values must be present and unique")
        keys.add(key)
        text = row.get("text")
        if not isinstance(text, str):
            raise ValueError(f"corpus chunk {key!r} has no text string")
        packed = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        corpus_hash.update(len(packed).to_bytes(8, "big")); corpus_hash.update(packed)
        text_hash.update(key.encode()); text_hash.update(b"\0"); text_hash.update(text.encode()); text_hash.update(b"\0")
    return {"schema": 1, "model": model, "model_revision": model_revision or "unpinned",
            "backend": backend, "precision": precision, "max_length": max_length,
            "corpus_sha256": corpus_hash.hexdigest(),
            "text_sha256": text_hash.hexdigest(), "records": len(corpus)}


def _sparse_parts(value):
    if value is None:
        return [], []
    if isinstance(value, dict):
        indices, values = value.get("indices", []), value.get("values", [])
    else:
        indices = getattr(value, "indices", None)
        values = getattr(value, "values", None)
    if indices is None or values is None:
        if isinstance(value, (list, tuple)):
            pairs = value
            indices, values = zip(*pairs) if pairs else ([], [])
        else:
            raise ValueError("unsupported sparse embedding representation")
    if len(indices) != len(values):
        raise ValueError("sparse indices/values length mismatch")
    merged = defaultdict(float)
    for index, weight in zip(indices, values):
        weight = float(weight)
        if weight > 0 and math.isfinite(weight):
            merged[int(index)] += weight
    return [k for k, _ in sorted(merged.items())], [v for _, v in sorted(merged.items())]


def _sparse_model(backend, model_name, threads, max_length, precision, cache_dir=None):
    if backend == "torch":
        from splade_torch_backend import TorchSpladeEncoder
        return TorchSpladeEncoder(model_name=model_name, cache_dir=cache_dir, threads=threads,
                                  max_length=max_length, precision=precision)
    try:
        from fastembed import SparseTextEmbedding
    except ImportError as exc:
        raise RuntimeError("FastEmbed is required for encoding; install it separately (tests do not need it)") from exc
    kwargs = {"model_name": model_name, "threads": threads}
    if cache_dir:
        kwargs["cache_dir"] = cache_dir
    model = SparseTextEmbedding(**kwargs)
    if max_length is not None:
        tokenizer = getattr(getattr(model, "model", None), "tokenizer", None)
        enable = getattr(tokenizer, "enable_truncation", None)
        if enable is None:
            raise ValueError("FastEmbed model tokenizer does not support enable_truncation")
        enable(max_length=max_length)
    return model


def _embed(model, texts, method, **kwargs):
    fn = getattr(model, method, None) or getattr(model, "embed", None)
    if fn is None:
        raise RuntimeError(f"FastEmbed model exposes neither {method} nor embed")
    result = fn(texts, **kwargs)
    return list(result)


def _model_revision(model, backend):
    if backend == "torch":
        return getattr(getattr(getattr(model, "model", None), "config", None), "_commit_hash", None) or "unpinned"
    inner = getattr(model, "model", None)
    model_dir = getattr(inner, "_model_dir", None)
    if model_dir:
        parts = Path(str(model_dir)).parts
        if "snapshots" in parts and parts.index("snapshots") + 1 < len(parts):
            return parts[parts.index("snapshots") + 1]
    return "unpinned"


def encode_corpus(corpus_path, output_path, checkpoint_path, model_name=DEFAULT_SPLADE,
                  threads=1, batch_size=1, max_length=512, model=None, backend="fastembed",
                  precision="fp16", cache_dir=None, max_seconds=180):
    if not 1 <= threads <= MAX_THREADS:
        raise ValueError(f"threads must be between 1 and {MAX_THREADS}")
    if batch_size < 1 or max_length < 1:
        raise ValueError("batch_size and max_length must be positive")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    corpus = _jsonl(corpus_path)
    if backend not in {"fastembed", "torch"}:
        raise ValueError("backend must be fastembed or torch")
    checkpoint_path, output_path = Path(checkpoint_path), Path(output_path)
    if model is None:
        model = _sparse_model(backend, model_name, threads, max_length, precision, cache_dir)
    effective_precision = (getattr(model, "precision", precision) if backend == "torch" else "onnx-default")
    manifest = _manifest(corpus, model_name, backend, effective_precision, max_length,
                         _model_revision(model, backend))
    completed = {}
    if checkpoint_path.exists():
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("manifest") != manifest:
            raise ValueError("checkpoint invalidated: corpus/text/model manifest changed")
    elif output_path.exists() and output_path.stat().st_size:
        raise ValueError("output exists without checkpoint; refusing to mix unverified vectors")
    source = {str(row["chunk_id"]): row for row in corpus}
    if output_path.exists():
        for saved_row in _jsonl(output_path):
            cid = str(saved_row.get("chunk_id", ""))
            source_row = {key: value for key, value in saved_row.items() if key != "sparse_vector"}
            if cid not in source or cid in completed:
                raise ValueError("checkpoint output has unknown or duplicate chunk_id")
            if source_row != source[cid] or not isinstance(saved_row.get("sparse_vector"), dict):
                raise ValueError(f"checkpoint output record {cid!r} does not match corpus/text fingerprint")
            _sparse_parts(saved_row["sparse_vector"])
            completed[cid] = saved_row
    if checkpoint_path.exists():
        expected_count = json.loads(checkpoint_path.read_text(encoding="utf-8")).get("completed_count", 0)
        if expected_count > len(completed):
            raise ValueError("checkpoint lists records missing from output")
    if not checkpoint_path.exists():
        _write_json_atomic(checkpoint_path, {"manifest": manifest, "completed_count": len(completed)})
    started = time.perf_counter()
    missing = [row for row in corpus if str(row["chunk_id"]) not in completed]
    newly_encoded = 0
    for offset in range(0, len(missing), batch_size):
        if max_seconds is not None and time.perf_counter() - started >= max_seconds:
            break
        batch = missing[offset:offset + batch_size]
        vectors = _embed(model, [row["text"] for row in batch], "passage_embed")
        if len(vectors) != len(batch):
            raise RuntimeError("encoder returned a different number of vectors than inputs")
        for row, vector in zip(batch, vectors):
            indices, values = _sparse_parts(vector)
            completed[str(row["chunk_id"])] = {**row, "sparse_vector": {"indices": indices, "values": values}}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "a", encoding="utf-8", newline="\n") as stream:
            for row in batch:
                stream.write(json.dumps(completed[str(row["chunk_id"])], ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
            stream.flush(); os.fsync(stream.fileno())
        newly_encoded += len(batch)
        _write_json_atomic(checkpoint_path, {"manifest": manifest,
            "completed_count": len(completed)})
    elapsed = time.perf_counter() - started
    complete = len(completed) == len(corpus)
    if complete:
        _write_jsonl_atomic(output_path, [completed[str(row["chunk_id"])] for row in corpus])
    nnz = sum(len((row.get("sparse_vector") or {}).get("indices", [])) for row in completed.values())
    size = output_path.stat().st_size if output_path.exists() else 0
    return {"manifest": manifest, "encoded": len(completed), "newly_encoded": newly_encoded,
            "elapsed_seconds": elapsed, "documents_per_second": newly_encoded / elapsed if elapsed else 0.0,
            "nnz": nnz, "output_bytes": size, "complete": complete,
            "timed_out": not complete and max_seconds is not None and elapsed >= max_seconds}


def _vector(row, field):
    value = row.get(field)
    if value is None and field == "dense_vector":
        value = row.get("dense")
    if value is None:
        return None
    if isinstance(value, dict) and "values" in value and "indices" not in value:
        return None
    return value


def dense_cosine(query, rows):
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required for dense cosine scoring") from exc
    q = np.asarray(query, dtype=np.float64).reshape(-1)
    qnorm = float(np.linalg.norm(q))
    scores = {}
    for row in rows:
        vector = _vector(row, "dense_vector")
        if vector is None:
            continue
        d = np.asarray(vector, dtype=np.float64).reshape(-1)
        if d.shape != q.shape:
            raise ValueError(f"dense dimension mismatch for chunk {row.get('chunk_id')}")
        denom = qnorm * float(np.linalg.norm(d))
        scores[str(row["chunk_id"])] = float(np.dot(q, d) / denom) if denom else 0.0
    return scores


def sparse_dot(query, rows):
    qi, qv = _sparse_parts(query)
    postings = defaultdict(list)
    for row in rows:
        sparse = row.get("sparse_vector")
        if sparse is None:
            continue
        di, dv = _sparse_parts(sparse)
        cid = str(row["chunk_id"])
        for term, weight in zip(di, dv):
            postings[term].append((cid, weight))
    scores = defaultdict(float)
    for term, weight in zip(qi, qv):
        for cid, doc_weight in postings.get(term, ()):
            scores[cid] += weight * doc_weight
    return dict(scores)


def bm25_scores(query, rows, k1=1.2, b=0.75):
    docs = []
    for row in rows:
        tokens = (str(row.get("title") or "") + " " + str(row.get("text") or "")).lower().split()
        docs.append((str(row["chunk_id"]), Counter(tokens), len(tokens)))
    n = len(docs)
    avgdl = sum(length for _, _, length in docs) / n if n else 0.0
    terms = set(str(query).lower().split())
    df = Counter(term for _, counts, _ in docs for term in terms if counts.get(term))
    result = {}
    for cid, counts, length in docs:
        score = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if freq:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                norm = freq + k1 * (1 - b + b * length / avgdl) if avgdl else freq
                score += idf * freq * (k1 + 1) / norm
        if score > 0:
            result[cid] = score
    return result


def reciprocal_rank_fusion(*rankings, k=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            scores[cid] += 1.0 / (k + rank)
    return dict(scores)


def _fixture_queries(fixtures):
    result = []
    for fixture_name, fixture in fixtures:
        for group in fixture.get("groups", []):
            for phrase in group.get("phrases", []):
                result.append({"fixture": fixture_name, "group": group, "phrase": phrase})
    return result


def _identity(row):
    values = [row.get("paper_id"), row.get("canonical_id"), row.get("arxiv_id")]
    aliases = row.get("aliases") or row.get("twins") or []
    values.extend(aliases if isinstance(aliases, list) else [])
    return {str(value).strip().lower() for value in values if value}


def _paper_id(row):
    return str(row.get("paper_id") or row.get("canonical_id") or row.get("arxiv_id") or row["chunk_id"]).strip().lower()


def _collapse_paper_scores(chunk_scores, rows):
    scores, best_chunks, order = {}, {}, {}
    for pos, row in enumerate(rows):
        cid = str(row["chunk_id"])
        if cid not in chunk_scores:
            continue
        paper = _paper_id(row)
        order.setdefault(paper, pos)
        if paper not in scores or chunk_scores[cid] > scores[paper]:
            scores[paper] = chunk_scores[cid]
            best_chunks[paper] = cid
    return scores, best_chunks, order


def _rank_papers(scores, order):
    return sorted(scores, key=lambda paper: (-scores[paper], order.get(paper, math.inf), paper))


def _target_data(group):
    targets = []
    for target in group.get("targets", []):
        canonical = str(target.get("canonical_id", "")).strip().lower()
        if canonical:
            targets.append({canonical, *(str(alias).strip().lower() for alias in target.get("aliases", []) if alias)})
    negatives = {str(item.get("id", "")).strip().lower() for item in group.get("hard_negatives", []) if item.get("id")}
    return targets, negatives


def evaluate(corpus, fixtures, query_encoder, top_k=50):
    paper_aliases = defaultdict(set)
    for row in corpus:
        paper_aliases[_paper_id(row)].update(_identity(row))
    queries, phrase_results = _fixture_queries(fixtures), []
    scorer_seconds = Counter(); query_encode_seconds = Counter()
    for item in queries:
        group, phrase = item["group"], item["phrase"]
        query = phrase["query"]
        supplied_dense = phrase.get("dense_vector", phrase.get("dense"))
        supplied_sparse = phrase.get("sparse_vector")
        if supplied_dense is not None and supplied_sparse is not None:
            encoded = {"dense": supplied_dense, "sparse": supplied_sparse, "elapsed": {"dense": 0.0, "sparse": 0.0}}
        else:
            encoded = query_encoder(query, need_dense=supplied_dense is None, need_sparse=supplied_sparse is None)
        dense_query = supplied_dense if supplied_dense is not None else encoded.get("dense")
        sparse_query = supplied_sparse if supplied_sparse is not None else encoded.get("sparse")
        query_encode_seconds.update(encoded.get("elapsed", {}))
        score_maps = {}
        if dense_query is not None:
            t = time.perf_counter(); score_maps["dense"] = dense_cosine(dense_query, corpus); scorer_seconds["dense"] += time.perf_counter() - t
        if sparse_query is not None:
            t = time.perf_counter(); score_maps["splade"] = sparse_dot(sparse_query, corpus); scorer_seconds["splade"] += time.perf_counter() - t
        t = time.perf_counter(); score_maps["bm25_simple"] = bm25_scores(query, corpus); scorer_seconds["bm25_simple"] += time.perf_counter() - t
        paper_scores, paper_chunks, paper_order = {}, {}, {}
        for method, scores in score_maps.items():
            paper_scores[method], paper_chunks[method], order = _collapse_paper_scores(scores, corpus)
            paper_order.update(order)
        rankings = {name: _rank_papers(scores, paper_order)[:top_k] for name, scores in paper_scores.items()}
        if "dense" in rankings and "splade" in rankings:
            t = time.perf_counter(); fused = reciprocal_rank_fusion(rankings["dense"], rankings["splade"]); scorer_seconds["rrf_dense_splade"] += time.perf_counter() - t
            paper_scores["rrf_dense_splade"] = fused
            rankings["rrf_dense_splade"] = _rank_papers(fused, paper_order)[:top_k]
        if "dense" in rankings:
            t = time.perf_counter(); fused = reciprocal_rank_fusion(rankings["dense"], rankings["bm25_simple"]); scorer_seconds["rrf_dense_bm25_simple"] += time.perf_counter() - t
            paper_scores["rrf_dense_bm25_simple"] = fused
            rankings["rrf_dense_bm25_simple"] = _rank_papers(fused, paper_order)[:top_k]
        if all(name in rankings for name in ("dense", "bm25_simple", "splade")):
            t = time.perf_counter()
            fused = reciprocal_rank_fusion(rankings["dense"], rankings["bm25_simple"], rankings["splade"], k=60)
            scorer_seconds["rrf_dense_bm25_simple_splade"] += time.perf_counter() - t
            paper_scores["rrf_dense_bm25_simple_splade"] = fused
            rankings["rrf_dense_bm25_simple_splade"] = _rank_papers(fused, paper_order)[:top_k]
        targets, negatives = _target_data(group)
        source_rows = []
        best_chunk_ids = {}
        for method, papers in rankings.items():
            if method in paper_chunks:
                method_chunks = paper_chunks[method]
            else:
                contributors = [paper_chunks[name] for name in ("dense", "splade", "bm25_simple") if name in paper_chunks]
                method_chunks = {paper: next((mapping[paper] for mapping in contributors if paper in mapping), None)
                                 for paper in papers}
            best_chunk_ids[method] = {paper: method_chunks[paper] for paper in papers if method_chunks.get(paper)}
            target_rank = next((i for i, paper in enumerate(papers, 1)
                                if any(paper_aliases[paper] & target for target in targets)), None)
            negative_ranks = {paper: i for i, paper in enumerate(papers, 1)
                              if paper_aliases[paper] & negatives or paper in negatives}
            source_rows.append({"method": method, "paper_rank": target_rank,
                "hit@10": bool(target_rank and target_rank <= 10), "hit@50": bool(target_rank and target_rank <= 50),
                "reciprocal_rank": 1.0 / target_rank if target_rank else 0.0,
                "comparator_alerts": [{"paper_id": nid, "rank": rank} for nid, rank in negative_ranks.items()
                    if (target_rank is None and rank <= 10) or (target_rank is not None and rank < target_rank and rank <= 10)]})
        phrase_results.append({"fixture": item["fixture"], "group_id": group["id"], "phrase_id": phrase.get("id"),
            "query": query, "methods": source_rows,
            "raw_top_ids": rankings, "best_chunk_ids": best_chunk_ids})
    methods = sorted({m["method"] for row in phrase_results for m in row["methods"]})
    coverage = {}
    for fixture_name, fixture in fixtures:
        target_ids = {}
        for group in fixture.get("groups", []):
            for target in group.get("targets", []):
                canonical = str(target.get("canonical_id", "")).strip().lower()
                if canonical:
                    target_ids[canonical] = {canonical, *(str(x).strip().lower() for x in target.get("aliases", []) if x)}
        corpus_ids = set().union(*(_identity(row) for row in corpus)) if corpus else set()
        present = sorted(target for target, aliases in target_ids.items() if aliases & corpus_ids)
        coverage[fixture_name] = {"target_count": len(target_ids), "covered_count": len(present),
            "missing_targets": sorted(set(target_ids) - set(present)), "covered_targets": present,
            "matched_aliases": {target: sorted(target_ids[target] & corpus_ids) for target in present}}
    def metric_summary(rows):
        result = {}
        for method in methods:
            vals = [next(m for m in row["methods"] if m["method"] == method) for row in rows]
            result[method] = {"denominator": len(vals), "hit@10": sum(x["hit@10"] for x in vals) / len(vals) if vals else 0.0,
                "hit@50": sum(x["hit@50"] for x in vals) / len(vals) if vals else 0.0,
                "reciprocal_rank": sum(x["reciprocal_rank"] for x in vals) / len(vals) if vals else 0.0,
                "comparator_alerts": sum(len(x["comparator_alerts"]) for x in vals)}
        return result
    summary = metric_summary(phrase_results)
    groups = defaultdict(list)
    for row in phrase_results:
        groups[(row["fixture"], row["group_id"])].append(row)
    def group_summary(rows):
      result = {}
      local_groups = defaultdict(list)
      for row in rows:
          local_groups[row["group_id"]].append(row)
      for method in methods:
        group_vals = []
        for group_rows in local_groups.values():
            group_vals.append(all(next(m for m in phrase["methods"] if m["method"] == method)["hit@10"] for phrase in group_rows))
        result[method] = {"groups": len(group_vals), "all_phrases_hit@10": sum(group_vals) / len(group_vals) if group_vals else 0.0}
      return result
    fixture_summary = {fixture: {"phrases": len([r for r in phrase_results if r["fixture"] == fixture]),
        "metrics": metric_summary([r for r in phrase_results if r["fixture"] == fixture]),
        "all_phrases_per_group": group_summary([r for r in phrase_results if r["fixture"] == fixture])}
        for fixture, _ in fixtures}
    return {"notice": "Known-source retrieval and comparator alerts only; not precision claims. BM25 is a simple whitespace baseline, not the production matcher.",
        "headline_denominator_policy": "All fixture phrases are included; corpus-missing targets remain misses.",
        "layers_notice": "No layer filtering applied; corpus layers are pooled.",
        "summary": summary, "fixture_summary": fixture_summary, "all_phrases_per_group": group_summary(phrase_results), "corpus_coverage": coverage,
        "query_encode_seconds": dict(query_encode_seconds), "scorer_seconds": dict(scorer_seconds),
        "phrases": phrase_results}


def _eval_encoder(splade_model):
    def encode(query, need_dense=True, need_sparse=True):
        elapsed, result = {}, {}
        if need_sparse:
            started = time.perf_counter(); vector = _embed(splade_model, [query], "query_embed")[0]
            result["sparse"] = {"indices": _sparse_parts(vector)[0], "values": _sparse_parts(vector)[1]}
            elapsed["splade"] = time.perf_counter() - started
        result["elapsed"] = elapsed
        return result
    return encode


def _dense_query_map(path):
    raw = Path(path).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and "queries" in parsed:
        records = parsed["queries"]
    elif isinstance(parsed, dict) and "query" in parsed and any(key in parsed for key in ("vector", "dense_vector", "query_vector", "dense")):
        records = [parsed]
    elif isinstance(parsed, dict):
        records = parsed
    elif isinstance(parsed, list):
        records = parsed
    else:
        records = _jsonl(path)
    if isinstance(records, dict):
        mapping = records
    else:
        mapping = {}
        for row in records:
            query = row.get("query")
            vector = row.get("vector", row.get("dense_vector", row.get("query_vector", row.get("dense"))))
            if not query or vector is None or query in mapping:
                raise ValueError("dense query records need unique query and vector fields")
            mapping[query] = vector
    for query, vector in mapping.items():
        if not isinstance(query, str) or not isinstance(vector, (list, tuple)) or len(vector) != 384:
            raise ValueError("dense query vectors must be keyed by exact query text and have dimension 384")
    return mapping


def pilot_corpus(corpus, fixtures, background_limit=1500, chunks_per_anchor=20):
    """Keep a bounded background sample plus up to N chunks for every known target."""
    anchors = set()
    for fixture in fixtures:
        for group in fixture.get("groups", []):
            for target in group.get("targets", []):
                anchors.add(str(target.get("canonical_id", "")).strip().lower())
                anchors.update(str(x).strip().lower() for x in target.get("aliases", []) if x)
    selected, background = [], []
    counts = Counter()
    for row in corpus:
        if _identity(row) & anchors:
            paper = str(row.get("paper_id") or row.get("canonical_id") or row.get("arxiv_id")).strip().lower()
            if counts[paper] < chunks_per_anchor:
                selected.append(row); counts[paper] += 1
        else:
            background.append(row)
    return selected + background[:background_limit]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    enc = sub.add_parser("encode", help="encode corpus passages into SPLADE vectors")
    enc.add_argument("--corpus", required=True); enc.add_argument("--output", required=True)
    enc.add_argument("--checkpoint", required=True); enc.add_argument("--model", default=DEFAULT_SPLADE)
    enc.add_argument("--threads", type=int, default=1); enc.add_argument("--batch-size", type=int, default=1)
    enc.add_argument("--max-length", type=int, default=512); enc.add_argument("--max-seconds", type=float, default=180)
    enc.add_argument("--backend", choices=("fastembed", "torch"), default="torch")
    enc.add_argument("--precision", choices=("fp16", "fp32"), default="fp16"); enc.add_argument("--cache-dir")
    enc.add_argument("--pilot-fixture", action="append", default=[])
    enc.add_argument("--pilot-background", type=int, default=1500); enc.add_argument("--anchor-chunks", type=int, default=20)
    ev = sub.add_parser("evaluate", help="score frozen paraphrase fixtures against encoded JSONL")
    ev.add_argument("--corpus", required=True); ev.add_argument("--fixture", action="append", required=True)
    ev.add_argument("--dense-queries", required=True, help="JSON/JSONL mapping exact query text to precomputed 384d vector")
    ev.add_argument("--output", required=True); ev.add_argument("--threads", type=int, default=1)
    ev.add_argument("--splade-model", default=DEFAULT_SPLADE); ev.add_argument("--backend", choices=("fastembed", "torch"), default="torch")
    ev.add_argument("--precision", choices=("fp16", "fp32"), default="fp16"); ev.add_argument("--cache-dir")
    ev.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args(argv)
    if args.command == "encode":
        corpus = _jsonl(args.corpus)
        if args.pilot_fixture:
            fixtures = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.pilot_fixture]
            corpus = pilot_corpus(corpus, fixtures, args.pilot_background, args.anchor_chunks)
            # The selected pilot is materialized beside the output so checkpoint hashes match it.
            pilot_path = Path(args.output).with_suffix(".pilot-input.jsonl")
            _write_jsonl_atomic(pilot_path, corpus)
            args.corpus = str(pilot_path)
        result = encode_corpus(args.corpus, args.output, args.checkpoint, args.model,
                               args.threads, args.batch_size, args.max_length, backend=args.backend,
                               max_seconds=args.max_seconds,
                               precision=args.precision, cache_dir=args.cache_dir)
    else:
        if not 1 <= args.threads <= MAX_THREADS:
            parser.error(f"threads must be between 1 and {MAX_THREADS}")
        corpus = _jsonl(args.corpus)
        fixtures = [(Path(path).name, json.loads(Path(path).read_text(encoding="utf-8"))) for path in args.fixture]
        sparse_model = _sparse_model(args.backend, args.splade_model, args.threads,
                                     args.max_length, args.precision, args.cache_dir)
        dense_queries = _dense_query_map(args.dense_queries)
        missing_queries = sorted({item["phrase"]["query"] for item in _fixture_queries(fixtures)} - set(dense_queries))
        if missing_queries:
            raise ValueError(f"dense query file missing {len(missing_queries)} exact fixture queries")
        query_encoder = _eval_encoder(sparse_model)
        def encode_query(query, need_dense=True, need_sparse=True):
            result = query_encoder(query, need_dense=False, need_sparse=need_sparse)
            result["dense"] = dense_queries.get(query)
            return result
        result = evaluate(corpus, fixtures, encode_query)
        result["encoder"] = {"backend": args.backend, "model": args.splade_model,
                             "precision": getattr(sparse_model, "precision", args.precision),
                             "max_length": args.max_length,
                             "model_revision": _model_revision(sparse_model, args.backend)}
        result["encode_throughput"] = {"corpus_documents": len(corpus), "sparse_nnz": sum(len((r.get("sparse_vector") or {}).get("indices", [])) for r in corpus),
                                       "corpus_bytes": Path(args.corpus).stat().st_size}
        _write_jsonl_atomic(args.output, [result])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
