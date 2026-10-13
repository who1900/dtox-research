# Source-centric retrieval: implementation boundary and migration

Status: local foundation and opt-in runtime integration implemented, not deployed.
Real-corpus inventory has run; the complete generation and its semantic acceptance
are still pending. See `2026-10-07-source-generation-rollout.md` for current build
status. This document is not a production-readiness certificate.

## Why this is a data-model change

The last frozen primary-result check was 14/24 known-source hits and only 1/8
mechanisms retained a known source under all three formulations. That check has
not been repeated or redefined here. Unit tests below do not replace it.

The inspected implementation loses information at two different boundaries:

1. `fetch_latex_source` originally discarded separate `.bib` and `.bbl` archive
   members. Some cached sources do contain inline `\bibitem` entries; this is
   not evidence that the entire corpus retains its bibliography.
2. Installed pylatexenc 2.10 renders `Method \cite{key} does this.` as
   `Method <cit.> does this.`. A chunk can therefore describe a method while
   losing the key identifying its source.

The `citations(src,dst)` table records article-level edges from the external
reference feed. It does not identify the passage that explains a cited method.
Existing graph expansion and the rejected query-conditioned graph prototype
cannot reconstruct that missing binding.

There is also a retrieval/evidence coupling: `_hier_run` requires a surviving
target chunk, and final source selection applies a passage similarity floor.
Finding an explanatory passage in a later paper is not enough to retain the
source it cites. Lowering the floor globally would introduce unrelated hits.

## Target model

```text
Owned abstract / method passage ───────┐
                                     ├─ typed representations ─ canonical source ranking
Citation-bearing passage             │                                  │
  └─ exact bibliography binding ──────┘                                  └─ read original evidence
```

A representation has a target source, a text owner, an owner artifact hash, an
exact locator, and a kind. A citation context additionally has a verified
key-to-identifier binding. Its owner remains the citing paper, never the target.

Dense and lexical retrieval should search the same representation corpus.
Representations are aggregated by canonical source before rank fusion and the
result cut. Repeated chunks, aliases and many citations cannot occupy repeated
source slots or provide repeated votes within one ranking.

The following states remain independent:

- a source was found;
- its original text is available;
- a relevant original passage has been read;
- that passage asserts, contradicts or does not address a particular claim.

Similarity and citation context establish neither the last state nor scientific
truth. The adjudication registry remains the owner of recorded reader judgments.

## Local implementation

- `pipeline/citation_contexts.py`: extracts raw citation-bearing contexts before
  text conversion. Resolves explicit arXiv, DOI and IACR identifiers, retaining
  source offsets, requested versions and bibliography provenance. Unknown keys,
  conflicting duplicate keys and multiple distinct identifiers are diagnosed,
  not resolved by fuzzy titles or embeddings. Raw provenance and masked search
  text are separate, so comments and literal code do not become search signals.
- `pipeline/service.py`: retains referenced archive bibliographies in a separate
  bounded, hash-bound `references.json`. Does not append bibliography bytes to
  the searchable TeX or mutate existing published point snapshots. Old caches
  without the sidecar are not silently downloaded or declared complete.
- `api/source_representations.py`: a derived, immutable generation of typed
  source representations, with a bounded read-only lexical adapter and
  canonical-source rank aggregation. Its structural checks do not independently
  attest the input artifacts or current live publication state. Target filters
  and canonical-source diversity precede the candidate cap. Representation kinds
  are provenance, not independent ranking votes. Matching orphan rows are invalid,
  not a successful empty search.
- `ops/build_source_representations.py`: explicit offline producer from supplied
  source/document records. No automatic production scan, external requests,
  model download, embedding call or mutation of the primary database. Repeated
  mentions of one target in one context are deduplicated; diagnostic examples are
  bounded, with explicit totals and truncation.

The catalog is not a stronger embedding model. The opt-in integration in
`api/source_generation.py` and `api/main.py` uses the existing 384-dimensional
original-passage index through an exact text/owner/revision adapter. Only new
summary and citation-context views enter a separate 768-dimensional collection.
Those two physical dense feeds contribute one canonical-source ranking, which
is fused once with the catalog's lexical ranking. This avoids re-embedding the
original corpus or treating representation kinds as independent votes.

`SOURCE_GENERATION_MANIFEST` selects an explicitly built generation. Unconfigured
production retains its existing path. A configured invalid or incomplete
generation fails explicitly, rather than silently switching back. Original
evidence is still fetched separately. No production activation has occurred.

## What makes the wider solution different

The retrieval unit is a source with several attributable descriptions, not an
isolated chunk competing with other chunks. The model need not recognise the
original author's wording alone: it can find a later explanation and follow an
exact binding back to the work. This is a corpus representation change, not a
claim that citation graphs or BM25 understand arbitrary paraphrases.

This addresses a verified information-loss path. It has not established how much
of the failed semantic baseline that path explains. Uncited or unresolved works
still depend on their own representations and the dense model. A stronger model
may still be necessary; the architecture must allow that substitution without
adding another competing promotion branch.

The end-state has one candidate-selection contract shared by search, validation
and comparisons. Discovery returns sources and attributable leads. Evidence
qualification reads the original text; registry judgments remain a separate
layer. A single canonical source does not gain extra votes from aliases, repeated
citation keys or the number of representation kinds.

## Local checks and use

Final offline command:

```powershell
python -B -m pytest -q api/tests pipeline/tests/test_citation_contexts.py pipeline/tests/test_bibliography_cache.py ops/tests/test_build_source_representations.py -p no:cacheprovider --disable-warnings
```

Prior foundation result: **1001 passed, 1 skipped, 3 warnings, 399 subtests passed
in 23.58 s**, before the runtime integration and corpus exporter changes.
This covers API regression and the new extraction, cache, catalog and producer
contracts. It is not an end-to-end production or semantic-quality result.
Independent review found and corrected unmasked search text, pre-filter candidate
starvation, duplicate lexical votes and matching orphan handling. A cache-write
fault check also covers sidecar failure before source publication.

The producer consumes explicit JSONL packets, not a database path. Source packets
carry `type: source`, exact `source_id`, title, year, layers, status and
`publication_revision` (SHA-256 or null); abstract and verified aliases are
optional. Document packets carry `type: document`, exact `source_id`, raw `text`,
`bibliography_files` (path/text pairs) and `origin_publication_revision`.

```powershell
python -B ops/build_source_representations.py --input packets.jsonl --output new-generation.sqlite
python -B ops/build_source_representations.py --query-only new-generation.sqlite --query "technical question"
```

These are operator examples, not jobs launched here. Existing output is never
overwritten. An explicit `--max-documents N` subset is labelled partial. SHA-256
and offsets identify input artifacts; they are not independent attestation of
the input producer or proof of live publication.

## Global migration, not another experiment loop

1. **Inventory binding coverage.** Use persisted source artifacts and exact
   source identifiers, across the corpus rather than the 16 known anchors.
   Count recoverable contexts, missing bibliographies, ambiguous entries and
   missing targets separately. A small sample is not a coverage estimate.
2. **Build a resumable derived generation.** Reuse available artifacts. Missing
   bibliography files require a separately scoped recovery policy; do not guess
   their contents from an article-level graph. Original snapshots stay immutable.
3. **Represent sources through several real descriptions.** Retain the complete
   abstract in bounded views, owned method passages and exactly bound citation
   contexts. Do not concatenate everything into a single truncating embedding.
4. **Connect dense and lexical adapters to this corpus.** Reuse existing chunk
   vectors only where their precise owner, revision and text-to-context binding
   can be verified. A vector for a whole paragraph is not automatically a vector
   for a different citation sentence. Otherwise encode the derived representation
   with an existing approved model; no automatic paid API or model substitution.
5. **Replace, rather than accumulate, ranking branches.** Source aggregation
   replaces the competing shortlist/graph-quota/discovery promotion decisions.
   Evidence reading keeps its existing constraints. A source can be discoverable
   without an original evidence snippet; it must be labelled accordingly.
6. **Release only the verified generation.** Activation must check publication
   visibility of both owner and target, alias identity, strict filters, overall
   latency budget and generation-aware caching. Rollback selects the previous
   derived generation, not a rewrite of the primary corpus.

These steps are the implementation path. A frozen release run, hard negatives,
different formulations and an independent holdout are the final acceptance
check, not a method-selection pastime. No source names, guessed synonyms or
known-answer injections belong in the retrieval algorithm.

## What this cannot promise

- An exact citation binding means “this paragraph cites this work”, not “every
  sentence in the paragraph describes that work”. Multi-work paragraphs are leads.
- Many bibliographies omit explicit identifiers; those bindings remain unresolved.
- PDF/Markdown numerical citations are not inferred by this LaTeX resolver. They
  need their own exact label-to-reference extraction, not reuse of unrelated IDs.
- More faithful representations can reduce vocabulary dependence, but cannot
  make a finite embedding model correct for every possible paraphrase.
- No latency gain, extra recall, 8/8 semantic pass, current corpus coverage or
  production readiness is established by this local change.
- OCR mathematical accuracy remains a separate unresolved release gate.

## Changes intentionally not made

No public API/MCP contract change, deployment, production indexing, model switch,
payment, access, environment, infrastructure, commit or push. Unrelated working
tree changes remain intact. Existing product features have not been disabled.
