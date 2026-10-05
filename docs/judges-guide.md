# dtox research: judges’ guide

## The pitch

dtox is a Web3-first research layer for AI agents and builders. It connects a free MCP interface to research papers, protocol specifications, and technical sources; retrieves source-grounded passages and mathematical material where available; and exposes provenance, coverage limitations, and authors’ stated caveats. It makes technical research easier to inspect, not a declaration that an invention is novel.

## A short live demo

1. Connect an MCP client using the commands on the [project README](../README.md#quick-start), or open the [live research interface](https://read.whoim.space/research/).
2. Ask the agent to discover relevant work before assuming a target paper is indexed.
3. Request a bounded research bundle or read the source section; inspect source IDs, provenance, full-text/abstract-only markers, scope, and limitations.
4. If a formula is relevant, retrieve the equation from the source and compare it with the cited paper. Keep any conclusion narrower than the evidence.

The public MCP currently exposes 13 tools. Read operations are available without payment or signup. Public unsigned registry mutation is disabled. Wallet-signed readings are an optional, separate write path.

## Reproducible source checks

These read-only MCP calls were freshly checked on 5 October 2026. Corpus counters were observed at **2026-10-05T10:00Z**; this is the observation time, not a frozen or reproducible index version:

- `get_code_or_math_spec(arxiv_id="2101.05511", target_elements="equation", max_chars=4500)` returned 6 of 6 equations, 1,245 characters, without truncation, with `exact_latex=true`; Equation 4 matched the source PDF. Source: [Quantifying Blockchain Extractable Value](https://arxiv.org/abs/2101.05511).
- `read_paper_section(paper_id="eip:4337", section_type="limitations", max_chars=1800)` returned 1,205 characters from ERC-4337 Security Considerations, including EntryPoint concentration and fee-draining/hijacking conditions. This is the specification’s own security discussion, not a dtox audit.

To reproduce with an agent, ask it to first discover whether the target source is indexed; then read the source section or formula and report provenance, truncation, and scope. Exact source LaTeX is available for this checked example, not guaranteed for all records or paraphrases. Do not assume a paper exists in the index or treat a retrieval score as a truth score.

## Evidence boundaries

- Counters observed at **2026-10-05T10:00Z**: **180,834 documents**, **17,365 Web3 records**, **10,801,902 chunks**, and **3,388,476 citation edges**. This timestamp records when counters were observed; it is not a frozen or reproducible index version. These are corpus counters, not customers, usage, or proof that all source text is available. Live counts: [stats.json](https://read.whoim.space/research/stats.json).
- Full text is source- and record-dependent; abstract-only fallbacks are explicitly possible. Dates and full-text markers should be read from each returned record.
- Retrieval ranks candidates; it does not establish novelty or correctness. The corpus has four bounded subject layers and no patent collection. No-result is not evidence of absence.
- Wallet signatures establish control of a signing key for an attributed reading. They do not establish that the reading is correct or independent. There is no independent-consensus claim.
- Solana evidence is historical devnet SAS and USDC x402 activity. EVM test-network configuration is not live end-to-end payment evidence; no mainnet-readiness claim is made.
- The service is a public research beta, not a paywall or a claim of customer traction. Proposed pricing/revenue is not actual revenue.

## How it fits among existing tools

This source ledger supports product positioning, not a benchmark or claim of exclusive capability. Semantic Scholar provides a broad academic graph and API. Elicit offers literature-review and extraction workflows, an API, and an MCP server. arXiv MCP tools can expose arXiv papers and LaTeX sections. dtox’s positioning is the combination of a Web3-centered source mix (including EIPs/ERCs and Solana SIMDs), embeddings-based vector search over structured research, source-level provenance and limitations, and a free agent-facing MCP endpoint. These capabilities overlap with existing offerings; do not describe MCP access or LaTeX retrieval as exclusive.

- [Semantic Scholar API](https://api.semanticscholar.org/api-docs/)
- [Elicit API and MCP launch announcement, 15 July 2026](https://elicit.com/blog/the-elicit-api-and-mcp-powering-autonomous-research-engines)
- [Elicit MCP setup documentation](https://support.elicit.com/en/articles/14757404-use-elicit-via-mcp-server)
- [Elicit](https://elicit.com/)
- [arxiv-mcp-server](https://github.com/blazickjp/arxiv-mcp-server)

## Project status and contact

The codebase predates the current hackathon period (project work began in July, before the contest opened in September). The existing [submission notes](../SUBMISSION.md) are repository material, not proof of submission acceptance. Demo video publication is pending owner approval; no unpublished or ignored video is linked here. The live research interface and the reproducible source checks above are available now.

**Daniyar Gabdullin, solo founder.** Mobile/Web3 engineer with blockchain, dApp, and wallet QA experience. [Portfolio](https://portfolio.whoim.space/) · [LinkedIn](https://www.linkedin.com/in/daniyar-gabdullin-11312b252/) · [CV](https://portfolio.whoim.space/Daniyar_Gabdullin_CV.pdf) · [GitHub](https://github.com/who1900).
