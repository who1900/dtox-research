# Citation-context search benchmark

An independent quality check for `/v1/search`, built without touching dtox's
own retrieval code.

## Why this is independent

If paper A's bibliography cites paper B, the sentence in A where that citation
appears is a natural-language description of B, written by someone who is not
us and did not know dtox exists. That gives a free (query, correct-answer)
pair: feed the sentence (with the citation marker removed) into `/v1/search`
and check whether B comes back. The source of these pairs is the
[Semantic Scholar Graph API](https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_citations),
not dtox, so a search bug and a benchmark bug cannot cancel each other out the
way they could if we wrote the queries ourselves.

## Pipeline

1. **`build_citation_bench.py`** (run on the server, needs `state.db`):
   - Samples target papers from `state.db` (`status='done'`, plain arXiv id),
     stratified by layer and by in-corpus popularity (in-degree bucket:
     1-4 / 5-49 / 50+).
   - For each target, pulls its citing contexts from Semantic Scholar
     (`GET /graph/v1/paper/arXiv:{id}/citations?fields=contexts,intents,isInfluential,externalIds`),
     cached to disk so a restart doesn't re-hit the API.
   - Filters contexts down to ones with exactly one citation marker (a single
     `[31]`-style bracket or a single author-year mention), reasonable length
     (60-400 chars, 8+ words), and not a citation-list or filler sentence.
   - Strips the marker to get the query text, and flags whether the query
     names the target explicitly (`named`) or only describes it
     (`named=false`) using title-token overlap.
   - Splits by `sha1(target_id) % 2` (by target, not by query, so a target's
     two queries never land on opposite sides).
   - Writes `eval/bench/citation_bench.jsonl`.

2. **`run_bench.py`**: posts each query to `/v1/search`, excludes the citing
   paper itself from the results (otherwise a query could "find itself"),
   and computes hit@1/3/10, MRR@10, nDCG@10, error/partial rates, and
   p50/p95 latency, sliced by layer / popularity bucket / named vs.
   descriptive. `--compare run_a.json run_b.json` prints metric deltas and the
   specific queries that gained or lost a top-3 hit.

3. **`eval/tests/test_bench.py`**: unit tests (no network, no DB) for the
   marker filter, query cleanup, `named` heuristic, deterministic split,
   source-id exclusion, and metric math.

## Running

Build the benchmark on the server (state.db lives there):

```bash
cd /opt/dtox-research
set -a; . /opt/dtox-research/s2.env; set +a   # exports S2_API_KEY, never printed
python eval/build_citation_bench.py --per-layer 120 --seed 7 \
    --cache-dir eval/.cache_s2 --out eval/bench/citation_bench.jsonl
```

Trial run on a handful of targets first:

```bash
python eval/build_citation_bench.py --limit-targets 10 --out /tmp/trial.jsonl
```

Run the benchmark against a live API:

```bash
python eval/run_bench.py --bench eval/bench/citation_bench.jsonl \
    --api http://127.0.0.1:8010 --keys /opt/dtox-research-api/keys.json \
    --split dev --limit 10 --tag baseline
```

Compare two configurations (e.g. hybrid on/off):

```bash
python eval/run_bench.py --bench eval/bench/citation_bench.jsonl \
    --params '{"hybrid": false}' --tag no_hybrid
python eval/run_bench.py --compare eval/runs/baseline.json eval/runs/no_hybrid.json
```

Unit tests (no network or DB needed, run anywhere):

```bash
python -m unittest discover -s eval/tests
```

## Known limitations

- **Single relevant document per query.** A citation context calls out one
  paper, but the real correct set may be larger (the idea may also be
  covered by a survey or an earlier paper by the same authors). Metrics here
  are a lower bound on true quality, not an exact score.
- **S2 contexts are not always precise.** Roughly 75% of citation edges have
  `contexts` at all, and the sentence boundary S2 extracts sometimes drifts
  from the actual claim. The marker + length + filler filters cut this down
  but do not eliminate it.
- **The `named` flag is a heuristic**, not ground truth: it only checks for
  literal token/acronym overlap with the target's title, so a generic word
  that happens to appear in a title (e.g. "transformer") can register as
  "named" when the sentence isn't really naming the paper.
- **dev/test split protects against re-tuning on the same targets**, but both
  splits are drawn from the same underlying corpus and time period; this is
  not a check against distribution shift over time.
