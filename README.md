<p align="center">
  <img src="presentation/pitch-visual/cover.png" alt="dtox research — research infrastructure for AI agents" width="100%">
</p>

# dtox research

**Research infrastructure for AI agents and protocol engineers.**

dtox connects agents to research papers, protocol specifications and technical documents. Find relevant work, read its assumptions, retrieve equations and algorithms, and bring cited evidence into an engineering decision.

[Open research](https://read.whoim.space/research/) · [Connect MCP](https://read.whoim.space/mcp) · [Developer guide](docs/technical-reference.md)

## The research index

**183,854 documents · 11,102,542 indexed passages · 13 public MCP tools**

Index snapshot: **14 October 2026**.

The corpus covers Web3, AI agents, language models and builder technology. Sources include arXiv, IACR ePrint, ACL Anthology, PMLR, open-access research, Ethereum EIPs/ERCs, Solana SIMDs and technical whitepapers.

## What you can do

- Discover papers and specifications by an engineering question.
- Read source sections, equations, algorithms, code and tables.
- Assemble a bounded research bundle with citations and source metadata.
- Compare methods and explore related work and research trends.
- Record wallet-signed readings through Solana SAS on devnet.

Document responses identify their source and available coverage.

## Connect your agent

The public research MCP is free to connect, with no signup or API key.

```bash
# Codex
codex mcp add dtox --url https://read.whoim.space/mcp

# Claude Code
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

For other HTTP MCP clients, use `https://read.whoim.space/mcp`.

Try:

> Find research on data availability for light clients. Read the relevant method and assumptions, then cite the passages that matter to an implementation.

[Research workflow](docs/agent-research-workflow.md) · [Tool catalog and self-hosting](docs/technical-reference.md)

## How it works

Source documents are parsed into sections and structured elements. Paper-level and passage-level vector retrieval combine with lexical search and citation links. The research API exposes these capabilities through MCP.

Python · FastAPI · Qdrant · SQLite FTS5 · ONNX · TypeScript · Solana SAS · x402

The free research endpoint and the optional x402 gateway are separate integrations. See the [gateway guide](x402-gateway/README.md) and [attestor guide](attestor/README.md).

## Build with dtox

- [Developer guide](docs/technical-reference.md)
- [Research workflow](docs/agent-research-workflow.md)
- [Security reporting](SECURITY.md)
- [GNU AGPL-3.0](LICENSE)

Built by **Daniyar Gabdullin**. [Portfolio](https://portfolio.whoim.space/) · [LinkedIn](https://www.linkedin.com/in/daniyar-gabdullin-11312b252/)
