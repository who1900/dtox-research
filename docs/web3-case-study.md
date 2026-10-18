# Web3 case: what a blob commitment actually proves

A rollup engineer has a blob transaction's versioned hash and a matching KZG commitment. Can a verifier conclude that the full blob is available to the network?

This walkthrough uses a real Ethereum specification and responses collected from the public DTOX MCP on **18 October 2026**. It is an engineering example, not a customer deployment or a transaction test.

## Ask, discover, read

Connect to `https://read.whoim.space/mcp` and ask:

> I have a blob transaction's versioned hash and a matching KZG commitment. Does that alone establish that the full blob is available to the network? What checks and what separate claim does EIP-4844 support?

The checked call was:

```json
{"tool":"search_research_paper","arguments":{"query":"I have a blob transaction's versioned hash and a matching KZG commitment. Does that alone establish that the full blob is available to the network? What checks and what separate claim does EIP-4844 support?","layer":"web3","strict":true,"limit":5}}
```

EIP-4844 was the first result. Open it with `get_paper` using `paper_id: "eip:4844"`, then use `read_paper_section` for:

| Section | What the retrieved passage establishes |
| --- | --- |
| `Networking` | Matching list lengths, commitment-to-versioned-hash correspondence, and verification of blobs against commitments and proofs are separate checks. |
| `Point evaluation precompile` | The proof checks a claimed polynomial evaluation and the commitment's correspondence to the supplied versioned hash. |
| `Consensus layer validation` | Blob sidecars and consensus-layer availability checks address a separate responsibility from execution-layer commitment checks. |

These three section reads succeeded through the public endpoint. Responses include source links and fragment provenance so the reader can inspect the evidence. Research reads need no wallet or payment.

## The engineering conclusion

**A matching commitment and versioned hash alone do not establish network data availability.** They connect an identifier to a commitment. A valid point-evaluation proof establishes the claimed evaluation under the commitment scheme's assumptions; it does not establish that every relevant participant can retrieve the blob. The specification assigns blob availability to the consensus layer and describes separate sidecar propagation. See [networking checks](https://eips.ethereum.org/EIPS/eip-4844#networking), [point evaluation](https://eips.ethereum.org/EIPS/eip-4844#point-evaluation-precompile) and [consensus-layer validation](https://eips.ethereum.org/EIPS/eip-4844#consensus-layer-validation).

For a verifier, keep commitment/proof validity and the chosen protocol's availability condition as separate predicates. Read the consensus specification for the deployed network/fork before choosing its availability rule. EIP-4844 describes the proposal's boundary; this walkthrough does not certify a current node implementation.

The useful result is a rejected false premise, backed by inspectable protocol text. Search rank alone would not establish that conclusion. A signed verdict, if the reader later chooses to publish one, attributes that reading; it does not replace these protocol checks.

## Extending the question to sampling

Sampling requires additional assumptions. If a fixed set of `m` out of `N` shares is unavailable, independent uniform draws with replacement have miss probability `(1-m/N)^s`. Uniformly selecting `s` distinct shares instead gives `C(N-m,s)/C(N,s)`. These are probability calculations under a stated model, not proofs that an arbitrary DA protocol is secure. The original [Fraud and Data Availability Proofs paper](https://arxiv.org/abs/1809.09044) discusses sampling and a particular erasure-coded construction.

In the same pilot, DTOX found that paper first, but the sampled PDF extraction omitted displayed equations and contained damaged symbols. The original source remains necessary for that formula check. This is why the workflow reads passages and checks source fidelity before treating a retrieved formula as implementation evidence.
