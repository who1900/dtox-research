# dtox research: Colosseum Crypto World's Fair submission (draft)

Working draft. Items marked TODO(owner) at the end are known only to the author. Items marked TODO verify must be rechecked against the live system before submitting. Nothing here should claim more than the demo shows.

## One-liner

The evidence layer for crypto agents: full-text research and protocol specs over MCP, paid per result with x402, with claim verdicts signed by the reviewing agent's wallet and anchored on Solana.

## Problem

Web3 teams build on cryptography, distributed systems and, more and more, AI. Before they commit to a design they need to know three things: has this been done, what exactly is the mechanism, and where does it break. Today that answer is scattered across arXiv, IACR, EIPs, Solana SIMDs and whitepapers, and the tools on top of them return links or confident paraphrases. An AI agent doing the research has two extra problems: a PDF wastes its context, and a similarity score says nothing about whether a paper actually makes the claim.

## What dtox does

- **Full text, cut by structure.** Papers are parsed from LaTeX sources. Equations (raw LaTeX), algorithms, code listings, tables, method sections and stated limitations are first-class results, so an agent can implement from them.
- **Retrieval channels that fail differently:** dense search over papers (bge-base-en-v1.5, 768 dim), BM25 over papers, global chunk search (bge-small-en-v1.5, 384 dim, plus BM25), and one hop through the citation graph. Copies of one paper from different sources are merged, boilerplate chunks are filtered out, and results carry a scope signal. There is no reranker in production.
- **Honest answers.** Out-of-scope ideas are refused, empty results come with corpus coverage, abstract-only sources say so, and every answer carries the corpus snapshot date.
- **Twelve public MCP tools:** `search_research_paper`, `find_papers`, `get_paper`, `read_paper_section`, `count_papers`, `similar_papers`, `get_code_or_math_spec`, `compare_methods`, `research_trends`, `validate_project`, `get_verdict_message`, `record_signed_verdict`. An agent can explore by paper (`find_papers` with `sort=foundational`, `get_paper`, `read_paper_section`) or by passage.
- **One MCP line to start:** `claude mcp add --transport http dtox https://read.whoim.space/mcp`. No key, no signup.

## How it uses Solana

1. **Pay per result with x402.** A paid MCP surface at `https://read.whoim.space/x402/mcp` answers with HTTP 402 and a USDC price ($0.01 for an evidence bundle, up to $0.25 for a full project audit). The agent signs a payment, the facilitator settles it on Solana, the result is returned. The buyer needs USDC only; the facilitator pays the network fee.
2. **Verifiable claim verdicts.** After reading the candidates, the agent signs a canonical verdict message ("paper X asserts / does not assert claim Y") with its own Solana wallet. dtox verifies the ed25519 signature and writes an attestation through the Solana Attestation Service. The reviewer's pubkey and signature live inside the attestation, so anyone can check who judged what without trusting dtox.
3. **Consensus you can audit.** A claim is only reported as settled (`confirmed_prior_art` or `ruled_out`) when distinct readers agree. Writing a signed verdict through the free `/mcp` endpoint costs nothing and is signed by the wallet, so sybil cost there is zero. The paid path `/x402/mcp` charges $0.01 USDC per verdict write, which raises the cost of flooding the registry but does not remove it.

Live on devnet today:
- Attestation 1, reviewer wallet A, claim "GRPO removes the separate value critic model by estimating the baseline from group relative rewards of sampled outputs", paper arXiv 2402.03300, verdict asserts: https://explorer.solana.com/address/729nUikYt2SUJaq1Brx2YwXvgLT5J1eVp9sfo31ncswu?cluster=devnet
- Attestation 2, reviewer wallet B (a different wallet), claim "DeepSeek-R1-Zero is trained with large-scale reinforcement learning without supervised fine-tuning as a preliminary step", paper arXiv 2501.12948, verdict asserts, written through a paid x402 call: https://explorer.solana.com/address/6pC4NS96euD9G7jPtjvXbVs1ffaWAjKdJMkX799Mq6eL?cluster=devnet
- x402 settlement: https://explorer.solana.com/tx/5DWkk85AFVHSTCqeSEbq8SpxEAtfkSRgQZLPJy9ZyCnTvZczsyD86yKELGWf5xYrDrF5wTvt3txUL19NFLYmrBvG?cluster=devnet

Both reviewer wallets are demo wallets run by the team, and the two attestations are on different claims. They show that the mechanism works on chain with two distinct signers. They are not evidence of independent consensus, and we do not present them as such. The registry reports the GRPO claim as `confirmed_prior_art` with two readers (checked live on 2026-09-29); only one on-chain attestation exists for that claim.

## Corpus (live counters)

https://read.whoim.space/research/ shows the current numbers from the running system (also https://read.whoim.space/research/stats.json). On 2026-09-29 (17:30 UTC): 167,900 documents, 9,480,081 structural chunks, 3,163,762 citation edges, 13,769 documents in the Web3 layer. TODO verify: refresh these numbers on the day of submission.

Layers: web3 (primary), ai-agents, llm-slm, builder-tech. Sources: arXiv (full text from LaTeX sources), IACR ePrint (abstract only), ACL Anthology, PMLR, OpenAlex open-access works, Ethereum EIPs and ERCs, Solana SIMDs, industry whitepapers, and curated GitHub technical docs (Solana, Anchor, SVM spec, Jupiter, Meteora, Orca, Marinade, Jito, Uniswap, Lido, Compound).

## Retrieval quality (held-out numbers, from eval/results)

- Citation-context benchmark, test split (334 queries): hit@3 0.195 (production before hierarchical retrieval, 2026-09-26) -> 0.359 (paper-then-chunk search, 2026-09-27) -> 0.377 (paper index on bge-base, 2026-09-28). hit@10 0.272 -> 0.425 -> 0.458.
- Security hand set (28 queries): hit@3 0.571 -> 0.714 after twin merging, boilerplate filtering and the citation-graph channel (2026-09-28 and 2026-09-29).
- Rank harness (canonical paper in top 3): 4/19 -> 7/19 after the graph channel. TODO verify: rerun on the final build.
- Golden harness: 17/17.
- Weak spot: the web3 hand set (16 test queries) sits at hit@3 0.406 and did not improve. Details in `eval/results/`.

## Built before vs during the hackathon

The contest period is September 14 to October 12, 2026. dtox existed before it, and we list the split plainly.

Before September 14 (from July 31, 2026):
- Ingestion pipeline (harvest, quality gate, LaTeX extraction, chunking, embedding, citation graph), REST API, MCP server with read tools, claim registry keyed by API identity.
- x402 gateway in shadow mode (prices returned, no settlement).

During the contest (git history from 2026-09-14, https://github.com/who1900/dtox-research/commits/main):
- Solana: wallet-signed claim verdicts anchored with the Solana Attestation Service (`attestor/`), API and MCP write path for signed verdicts, quorum by distinct wallets. x402 switched to live settlement on Solana devnet, including paid verdict writes.
- Retrieval: hierarchical paper-then-chunk search, paper-level BM25, rank fusion, paper index on bge-base, citation-graph channel in chunk search, merging of duplicate papers across sources, boilerplate chunk filter, scope signal with case-sensitive acronyms, a citation-context benchmark and a hand-verified security set.
- Corpus: fix for LaTeX `\input` resolution (about 24% of papers had lost their body) with reprocessing of affected papers, ERC standards, Solana and DeFi docs and whitepapers, a full arXiv cs harvest through OAI-PMH after the export API blocked the server, and a purge of 2665 papers admitted by case-blind acronym matches. Earlier in the period: HAL, GitHub technical docs and the builder-tech layer.
- Tools: `find_papers` (relevance, citations, recent, foundational), `get_paper`, `read_paper_section`, `count_papers`, `similar_papers`, and compact search output for MCP.
- Infrastructure: Qdrant and a second embedding service on an Oracle host, a second chunk embedder with failover, OpenAlex daily-budget backoff, latency percentiles in health.
- Security: public MCP credential moved out of source into a root-only environment file; registry mutation removed from the public endpoint.
- Public landing page with live corpus counters.

## Business model

Agents pay per result through x402: no accounts, no subscriptions, priced by cost of the call. The free MCP stays as the top of the funnel. Paid tiers are the deep project audit, bulk evidence bundles for agent frameworks, and verified-verdict feeds that other protocols can read on chain (for example grant programs or audit firms checking a team's prior-art claims).

Demand evidence: see TODO(owner) below. We will not submit numbers we cannot show.

## Open source and composability

AGPL-3.0. Standard interfaces only: MCP (streamable HTTP), x402 v2, Solana Attestation Service schemas. Any SAS-aware program or indexer can read dtox verdicts without our API.

## Limits we state up front

- Devnet only. Mainnet waits for an external review of the attestor and gateway.
- Four layers: Web3, AI agents, LLMs and small language models, builder technology.
- No patents, no guarantee that a given paper is present, IACR entries are abstract-only.
- Distinct wallets are cheap. Writes through the free `/mcp` endpoint have no sybil cost; the paid `/x402/mcp` path raises the cost but does not remove it. Reviewer reputation is on the roadmap.
- Search quality is measured but modest: hit@3 0.377 on the citation-context benchmark, and web3 queries lag the other layers.

## Links

- MCP: https://read.whoim.space/mcp
- Paid MCP (x402, devnet): https://read.whoim.space/x402/mcp
- Site: https://read.whoim.space/research/
- Code: https://github.com/who1900/dtox-research
- Demo video: see TODO(owner) below

## TODO(owner)

Facts only the author knows. Do not fill with guesses.

- Team: names, background, why this problem.
- Contact: email or handle for judges.
- Demo video URL.
- Demand evidence: distinct MCP clients, paid calls, named users and quotes.
