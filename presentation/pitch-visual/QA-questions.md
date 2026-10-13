# Jury Q&A - concise, evidence-bounded answers

### What is dtox?

Dtox helps protocol teams check research before they build. It gives builders and agents a hosted, bounded way to inspect source-backed papers and protocol specifications, including equations and stated limitations where available.

### What is the concrete use case in this pitch?

A protocol engineer choosing an auction rule asks what strategy a published relay-auction model derives and what assumptions it makes. Dtox retrieves the equation and related source text so the team can assess whether those assumptions fit its design. This example is not evidence that the model applies to every auction or to Solana.

### What did the fresh BEV call establish, and what did it not?

The MCP returned all six available equation objects from LaTeX source, including the bid-strategy equation, with `exact_latex=true` and no response truncation. However, `parse_completeness` is unknown and `source_url_status` is `source_not_verified`. The original paper was separately opened and manually checked. A related section response contained a duplicated paragraph/artifact and was truncated, so extraction is not presented as perfect.

### Does dtox prove an auction design is sound?

No. The paper assumes independently distributed rewards from a common uniform distribution, with participant count and distribution treated as prior knowledge. The engineer must decide whether that model fits the protocol. Retrieval supports that check; it does not prove applicability, correctness, or safety.

### What does the EIP-4337 example say?

The specification's Security Considerations describe the EntryPoint as a trust boundary where risk can concentrate. That is retrieved source text, not a dtox audit. A proposed review checklist or architecture implication is our engineering interpretation, not the specification authors' conclusion.

### How does dtox compare with alternatives?

Use the same three criteria: tool/source scope, evidence-reading features, and agent access. Elicit documents broad academic search, structured extraction/reports, and API/MCP access; Semantic Scholar exposes an academic graph API with metadata and citations; arxiv-mcp-server supports an arXiv paper library, Markdown and original-LaTeX section reads, citation-graph traversal, and local MCP. Dtox focuses on Web3 papers and protocol specs, indexed source objects/sections, and hosted MCP+REST. This is a capability comparison, not an accuracy, speed, coverage, or win-rate benchmark. MCP, LaTeX retrieval, and citation graphs are not exclusive to dtox.

### How large is the corpus?

The repository README records an observation at 2026-10-05T10:00Z: 180,834 documents and 17,365 Web3-tagged records, plus chunk and citation-edge counters. This is not a fresh snapshot or reproducible index version; tags can overlap. Counts describe inventory, not users or traction, and do not mean every record has full text.

### Is every result full text or exact LaTeX?

No. Availability and extraction vary by source. The BEV call is one checked example. Its exact-LaTeX flag does not establish universal parsing completeness, and the related section response had a duplicate artifact and truncation. Other records can be abstract-only or have reconstructed rather than original math.

### Is the service paid today?

The public beta offers 13 MCP tools with free research access. The same source bundle stays free. Optional paid transport or intensive usage is a business hypothesis, not exclusive sources, better evidence quality, a launched paid tier, or validated revenue.

### What has been demonstrated on Solana?

There are separate historical team-controlled devnet examples: an x402 USDC payment transaction and SAS wallet-attributed readings about different AI claims. They are not one joined event or an independent quorum. A signature attributes a record to a wallet; it does not establish truth, reviewer independence, or Sybil resistance. Base, Ethereum, and Arbitrum test-network configurations exist, but paid end-to-end settlement validation is pending.

### Who is building dtox?

Daniyar Gabdullin is the solo founder. Public-repository proof points are the open-source hosted research beta and its documented Solana devnet demonstrations. No award, credential, external customer, or unverified CV claim is needed for this pitch.

### Do you have pilot teams?

No participants are confirmed. The proposed pilot is 3 teams over 14 days, one real protocol decision per team. Each team would produce a task-cited design memo naming a source, an assumption, and an engineering consequence, then return with a second research question. The target is at least 2 of 3 teams completing both steps. This is a proposed validation target, not traction.

### Has dtox beaten competitors on a shared test?

No. A shared 20-question comparison is proposed but not measured. The pitch makes no comparative accuracy or speed claim. Any test should use the same questions, sources, and scoring criteria for each tool.
