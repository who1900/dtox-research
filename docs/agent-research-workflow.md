# A practical research workflow for agents

This is a usage guide, not a claim that retrieval quality has improved. No
fresh evidence of a semantic-quality gain is established by these instructions.

## Example: data availability for light clients

Suppose you are researching a Web3 design that lets light clients check data
availability without downloading a full block. Start with the mechanism,
object and constraint in ordinary language:

> How can light clients verify data availability with limited bandwidth?

Use `find_papers` to find papers on the subject, or
`search_research_paper` when looking for passages about a particular mechanism
or result. A paper title or identifier is helpful if already known, but not
required. Retrieval scores and `source_candidates` are candidate leads; they
do not prove that two passages make the same claim or express a probability.
Do not assume wording changes or synonyms preserve retrieval results.

For a promising result, call `get_paper` and inspect its outline and source
metadata. Read the relevant method or experiments section with
`read_paper_section`; inspect limitations when evaluating tradeoffs. Use
`get_code_or_math_spec` only when indexed formulas or algorithm elements are
relevant, and `compare_methods` to retrieve two papers' reported results and
limitations side by side. `get_research_bundle` can assemble a bounded packet
with provenance when several sources are useful. For a project review,
`validate_project` should receive distinct technical claims separately.

For `get_code_or_math_spec`, prefer the neutral `paper_id` argument; for
`compare_methods`, use `paper_id_a` and `paper_id_b`. The previous
`arxiv_id`/`arxiv_id_a`/`arxiv_id_b` names remain accepted for existing clients.
If both names are supplied for one identifier, they must agree (apart from
surrounding whitespace); missing or conflicting identifiers are rejected
before the API request. IDs are not rewritten to an assumed provider prefix,
so protocol and EIP-style identifiers such as `iacr:2019/953` and `eip:1559`
remain usable.

`get_code_or_math_spec` returns indexed structured elements, not a promise of
the original PDF or universally exact raw LaTeX. Some extracted equations
retain raw LaTeX; PDF-derived text and equations may not. Check the returned
element type, extraction/source metadata and truncation fields before treating
an object as exact source notation.

Tie each conclusion to the source evidence. Label limitations the authors
explicitly report separately from your inference, and call out missing
information. Cite the exact paper id and URL. Reproduce formulas only from
extracted indexed content; do not reconstruct an equation from prose. If the
record is abstract-only, say that and do not present it as inspected full
text. If results remain sparse or inconsistent after inspecting their source
channels, try a targeted reformulation once; avoid spending tokens on
repeated near-duplicate searches by default.

This workflow guides evidence handling; it does not establish corpus coverage,
synonym invariance, semantic equivalence, or a new retrieval-quality gain.
