# dtox research: Colosseum Crypto World's Fair submission (draft)

Working draft. Items in [brackets] must be filled with verified data before submitting. Nothing here should claim more than the demo shows.

## One-liner

The evidence layer for crypto agents: full-text research and protocol specs over MCP, paid per result with x402, with claim verdicts signed by the reviewing agent's wallet and anchored on Solana.

## Problem

Web3 teams build on cryptography, distributed systems and, more and more, AI. Before they commit to a design they need to know three things: has this been done, what exactly is the mechanism, and where does it break. Today that answer is scattered across arXiv, IACR, EIPs, Solana SIMDs and whitepapers, and the tools on top of them return links or confident paraphrases. An AI agent doing the research has two extra problems: a PDF wastes its context, and a similarity score says nothing about whether a paper actually makes the claim.

## What dtox does

- **Full text, cut by structure.** Papers are parsed from LaTeX sources. Equations (raw LaTeX), algorithms, code listings, tables, method sections and stated limitations are first-class results, so an agent can implement from them.
- **Three retrieval channels that fail differently:** dense vectors, BM25, and one hop through the citation graph.
- **Honest answers.** Out-of-scope ideas are refused, empty results come with corpus coverage, abstract-only sources say so, and every answer carries the corpus snapshot date.
- **One MCP line to start:** `claude mcp add --transport http dtox https://read.whoim.space/mcp`. No key, no signup.

## How it uses Solana

1. **Pay per result with x402.** A paid MCP surface at `https://read.whoim.space/x402/mcp` answers with HTTP 402 and a USDC price ($0.01 for an evidence bundle, up to $0.25 for a full project audit). The agent signs a payment, the facilitator settles it on Solana, the result is returned. The buyer needs USDC only; the facilitator pays the network fee.
2. **Verifiable claim verdicts.** After reading the candidates, the agent signs a canonical verdict message ("paper X asserts / does not assert claim Y") with its own Solana wallet. dtox verifies the ed25519 signature and writes an attestation through the Solana Attestation Service. The reviewer's pubkey and signature live inside the attestation, so anyone can check who judged what without trusting dtox.
3. **Consensus you can audit.** A claim is only reported as settled (`confirmed_prior_art` or `ruled_out`) when distinct wallets agree. Each verdict write is itself an x402 payment, so flooding the registry has a cost.

Live on devnet today:
- Attestation (two independent wallets, confirmed): https://explorer.solana.com/address/729nUikYt2SUJaq1Brx2YwXvgLT5J1eVp9sfo31ncswu?cluster=devnet
- x402 settlement: https://explorer.solana.com/tx/5DWkk85AFVHSTCqeSEbq8SpxEAtfkSRgQZLPJy9ZyCnTvZczsyD86yKELGWf5xYrDrF5wTvt3txUL19NFLYmrBvG?cluster=devnet
- Verdict written through a paid x402 call: https://explorer.solana.com/address/6pC4NS96euD9G7jPtjvXbVs1ffaWAjKdJMkX799Mq6eL?cluster=devnet

## Corpus (live counters)

https://read.whoim.space/research/ shows the current numbers from the running system. At the time of writing: [documents] documents, [chunks] structural chunks, [edges] citation edges, [web3] documents in the Web3 layer. Sources: arXiv, IACR ePrint, EIPs, Solana SIMDs, HAL, PMLR, OpenAlex open-access works, curated GitHub technical docs.

## Built before vs during the hackathon

The contest period is September 14 to October 12, 2026. dtox existed before it, and we list the split plainly.

Before September 14 (from July 31, 2026):
- Ingestion pipeline (harvest, quality gate, LaTeX extraction, chunking, embedding, citation graph), REST API, MCP server with read tools, claim registry keyed by API identity.
- x402 gateway in shadow mode (prices returned, no settlement).

During the contest:
- Wallet-signed claim verdicts anchored with the Solana Attestation Service (`attestor/`), API and MCP write path for signed verdicts, quorum by distinct wallets.
- x402 switched to live settlement on Solana devnet, including paid verdict writes as sybil cost.
- Search reliability: per-layer retrieval with rank fusion and a bounded lexical fallback, int8 vector quantization held in RAM, latency percentiles in health.
- Ingestion recovery: arXiv harvesting moved to OAI-PMH after the export API blocked the server, OpenAlex daily-budget backoff, new sources (HAL, GitHub technical docs, builder-tech layer).
- Security: public MCP credential moved out of source into a root-only environment file; unsigned registry writes removed from the public endpoint.
- Public landing page with live corpus counters.

Commit history with dates: https://github.com/who1900/dtox-research/commits/main

## Business model

Agents pay per result through x402: no accounts, no subscriptions, priced by cost of the call. The free MCP stays as the top of the funnel. Paid tiers are the deep project audit, bulk evidence bundles for agent frameworks, and verified-verdict feeds that other protocols can read on chain (for example grant programs or audit firms checking a team's prior-art claims).

Demand evidence: [number of distinct MCP clients, paid calls, named users and quotes, collected before submission]. We will not submit numbers we cannot show.

## Open source and composability

AGPL-3.0. Standard interfaces only: MCP (streamable HTTP), x402 v2, Solana Attestation Service schemas. Any SAS-aware program or indexer can read dtox verdicts without our API.

## Limits we state up front

- Devnet only. Mainnet waits for an external review of the attestor and gateway.
- Three subjects: LLMs and small language models, AI agents, Web3.
- No patents, no guarantee that a given paper is present, IACR entries are abstract-only.
- Distinct wallets are cheap; the per-write payment raises the cost of sybil agreement but does not remove it. Reviewer reputation is on the roadmap.

## Team

[name, background, why this problem, contact]

## Links

- MCP: https://read.whoim.space/mcp
- Paid MCP (x402, devnet): https://read.whoim.space/x402/mcp
- Site: https://read.whoim.space/research/
- Code: https://github.com/who1900/dtox-research
- Demo video: [link]
