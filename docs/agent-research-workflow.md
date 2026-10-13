# Research with an agent

Connect a client using the [README](../README.md#connect-your-agent), then start with an engineering question:

> How can light clients check data availability with limited bandwidth?

1. Use `find_papers` or `search_research_paper` to discover relevant sources.
2. Open a promising result with `get_paper` and inspect its outline and coverage.
3. Read the relevant method, assumptions or limitations with `read_paper_section`.
4. Request equations, code, algorithms or tables with `get_code_or_math_spec` when they are relevant.
5. Use `get_research_bundle` to assemble a bounded packet, or `compare_methods` to inspect two approaches.

Connect each engineering conclusion to the supporting passage and its source metadata. Distinguish the author's statements from your own interpretation. Document IDs and source links let a reader return to the underlying evidence.

For structured reads use `paper_id`; for comparisons use `paper_id_a` and `paper_id_b`. Tool discovery provides the full schemas, filters and pagination arguments.

[Tool catalog](technical-reference.md#public-mcp-tools)
