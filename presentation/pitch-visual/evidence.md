# Visual pitch - evidence ledger

This source ledger supports claims in the visual pitch. Observation times belong in this ledger and supporting documentation, not as claims of freshness in the slides. The counter timestamp is when values were observed, not a frozen or reproducible index version. Counts are inventory only; they do not measure usage, customers, or full-text availability.

| Claim | Source | Evidence boundary |
|---|---|---|
| 180,834 total documents; 17,365 Web3 records; 10,801,902 chunks; 3,388,476 citation edges | [Live corpus counters](https://read.whoim.space/research/stats.json) | Observed: 2026-10-05T10:00Z. This is an observation time, not a frozen/reproducible index version. Counts can change; mixed full-text coverage; not traction. |
| Public MCP has 13 tools; read access is free; public landing describes BGE-base dense embeddings for papers and BGE-small embeddings for chunks | [Live research interface](https://read.whoim.space/research/) · [Technical reference](../../docs/technical-reference.md) | Public read workflow; do not imply commercial paid access has launched. |
| Relay auction equation and model assumptions | [arXiv HTML v5, Quantifying Blockchain Extractable Value](https://arxiv.org/html/2101.05511v5), Equation 4; checked against the source PDF | Rechecked live: `get_code_or_math_spec` returned 6/6 equations, 1,245 characters, not truncated; Equation 4 matched the PDF. The model assumes rewards are independently drawn from a common uniform distribution; participant count and distribution are treated as prior knowledge. Example does not generalize to every document or query. Original-PDF crop assets are used in the slides. |
| EntryPoint centralization and account-abstraction security caveats | [EIP-4337 Security Considerations](https://eips.ethereum.org/EIPS/eip-4337) | The specification authors discuss the EntryPoint as a central trust point and possible concentrated risks. This is source retrieval, not a dtox security audit. |
| Literature review, extraction, API, and MCP offerings exist elsewhere | [Elicit API/MCP announcement](https://elicit.com/blog/the-elicit-api-and-mcp-powering-autonomous-research-engines) · [Elicit MCP setup](https://support.elicit.com/en/articles/14757404-use-elicit-via-mcp-server) · [Semantic Scholar API](https://api.semanticscholar.org/api-docs/) | Positioning comparison only; no exclusivity claim. |
| arXiv MCP tooling can retrieve LaTeX/source sections | [arxiv-mcp-server](https://github.com/blazickjp/arxiv-mcp-server) | Demonstrates adjacent overlap; dtox does not claim MCP or LaTeX retrieval as exclusive. |
| Historical Solana devnet payment activity | [Solana Explorer transaction](https://explorer.solana.com/tx/5DWkk85AFVHSTCqeSEbq8SpxEAtfkSRgQZLPJy9ZyCnTvZczsyD86yKELGWf5xYrDrF5wTvt3txUL19NFLYmrBvG?cluster=devnet) | Refer to this as a devnet demo. It is not current paid demand, a launched paid commercial service, or mainnet readiness. |
| Wallet-attributed Solana attestation | [Solana Explorer account](https://explorer.solana.com/address/6pC4NS96euD9G7jPtjvXbVs1ffaWAjKdJMkX799Mq6eL?cluster=devnet) | Attribution to a signing wallet is not proof of truth, reviewer independence, or sybil resistance. |
| EVM test-network payment support | [Project technical reference](../../docs/technical-reference.md) | Base, Ethereum, and Arbitrum test networks are configured; end-to-end settlement validation is pending. No mainnet deployment claim. |
| Free public research access and possible paid heavy usage | [Public MCP endpoint](https://read.whoim.space/mcp) · [Project roadmap](../../docs/pitch-roadmap.md) | Public read access is currently free. Paid heavy-usage access is a plan, not a launched commercial offer; no validated pricing, revenue, or conversion claim. |
| Pilot target proposed for this deck | Pitch target proposed for this deck | Three pilot teams is a proposed target, not confirmed pilots, users, or traction. |
| Founder description | [Portfolio](https://portfolio.whoim.space/) · [CV](https://portfolio.whoim.space/Daniyar_Gabdullin_CV.pdf) | Daniyar Gabdullin is presented as solo founder, mobile/Web3 engineer with blockchain, dApp, and wallet QA experience. |

## Claim guardrails

- Search results are candidates for human inspection, not proof of truth, correctness, or novelty. No-result does not prove absence.
- Corpus counts are not usage metrics. Full-text coverage is mixed and source-dependent.
- A checked exact-LaTeX example is not a universal extraction guarantee; paraphrase-invariance is not established.
- MCP, academic search, and LaTeX-capable workflows also exist in competing tools. Position dtox around its Web3 source mix and joined-up citations, formulas, provenance, and author limitations, not exclusive features.
- Historical devnet activity is not mainnet readiness, current paid demand, or validated revenue.
- Three pilot teams is a goal, not traction.
