"""
dtox research MCP server.

Exposes public read-only tools over MCP (streamable-http transport) that proxy to the
internal dtox-research-api (127.0.0.1:8010), which serves semantic search
over a full-text database of curated AI/LLM/Web3 arxiv papers.

The server holds its own server-side X-API-Key for the internal API;
MCP clients never see it.
"""

import json
import os
from pathlib import Path
from urllib.parse import quote
from typing import List, Optional, Union

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

RESEARCH_API_BASE = os.getenv("RESEARCH_API_BASE", "http://127.0.0.1:8010")
RESEARCH_API_KEY = os.getenv("RESEARCH_API_KEY", "")
REGISTRY_WRITES_ENABLED = os.getenv("MCP_REGISTRY_WRITES_ENABLED", "0") == "1"
# The MCP server holds a server-side key for the internal API so that MCP
# clients never need one. Set it in the environment; there is no default.

MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8011"))

# Server sits behind nginx at https://read.whoim.space/mcp — the Host header
# seen by uvicorn is the external hostname, so it must be explicitly allowed
# (DNS-rebinding protection otherwise only allows 127.0.0.1/localhost).
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "read.whoim.space"],
    allowed_origins=["https://read.whoim.space", "http://127.0.0.1:*", "http://localhost:*"],
)

INSTRUCTIONS = """\
dtox research is a full-text index of research papers, protocol specs (EIPs,
SIMDs) and whitepapers in four layers: web3 (primary), ai-agents, llm-slm and
builder-tech. Treat it as a library you explore in steps, not a single search:

- Which papers matter on a subject: find_papers (sort="foundational" for the
  works the field builds on, "citations" for the most cited, "recent" for the
  newest, "relevance" by default).
- How big a subject is, whether it is growing, which venues and terms carry
  it: count_papers (per-year counts; no query = the whole layer).
- One paper in depth: get_paper (abstract, outline, what it cites and what
  cites it), then read_paper_section for the text of a section, or
  get_code_or_math_spec for its algorithms, equations and tables.
- What else is like this paper, without shared references: similar_papers.
- A specific fact, mechanism, number or definition: search_research_paper,
  narrowed with section_type, element_type, terms or year_from.
- A bounded evidence-reading packet: get_research_bundle (strict by default),
  with source provenance and abstract-only fallbacks kept explicit.
- Two approaches side by side: compare_methods.
- Where a field is moving: research_trends (use about= for one subject), then
  find_papers sort="recent" and the limitations sections of what it returns.
- Whether an idea already exists: validate_project with each claim phrased
  two or three ways.

How to ask:
- Keep queries short and exact: a paper's name ("HotStuff", "Reflexion") or
  an identifier ("EIP-7702", "SIMD-96", "2303.11366", "iacr:2019/953") finds
  that document first. Do not pad a name with keywords.
- To find the canon on a subject start with find_papers (sort="foundational"
  or "relevance"), or open a known id with get_paper.
- "Does this idea already exist": validate_project, not search.
  "A versus B": compare_methods.
- Leave min_score unset: scores on this index are calibrated per layer and a
  high min_score only removes good hits (it is capped, and the answer says so).
- Leave layer unset unless you are sure: ai-agents and llm-slm overlap (agent
  papers are often tagged llm-slm), so layer there is a soft preference.
  search_research_paper(strict=True) keeps the requested filters exact and
  disables automatic relaxation; its existing default remains strict=False.

Cite every claim with the paper id and url you got it from. Empty results
reflect index coverage and retrieval limits, never establish novelty; inspect
evidence and scope. The index holds no patents.
"""

mcp = FastMCP(
    "dtox-research",
    instructions=INSTRUCTIONS,
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    transport_security=transport_security,
)


def _path_id(paper_id):
    """A document id as URL path: ids such as iacr:2019/953 keep their slash and
    colon (the API routes take a path), anything else that could break the URL
    (spaces, ?, #) is percent-encoded."""
    return quote(str(paper_id).strip(), safe="/:")


def _headers():
    return {"X-API-Key": RESEARCH_API_KEY, "Content-Type": "application/json"}


def _raise_tool_error(error, status=None, **diagnostics):
    payload = dict(error) if isinstance(error, dict) else {"error": error}
    payload["status"] = status
    payload.update(diagnostics)
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def _handle_error(resp: requests.Response) -> None:
    """Raise an SDK tool error for non-2xx responses, preserving diagnostics."""
    if 200 <= resp.status_code < 300:
        return
    try:
        data = resp.json()
    except ValueError:
        data = {}
    payload = dict(data) if isinstance(data, dict) else {"detail": data}
    error = payload.get("error")
    if resp.status_code == 401:
        error = error or "internal auth error contacting research API (401)"
    elif resp.status_code == 404:
        error = error or payload.get("detail", "not found")
    elif resp.status_code == 429:
        error = error or "rate limit exceeded on research API, try again shortly"
    elif resp.status_code == 400:
        error = error or f"bad request: {payload.get('detail', 'bad request')}"
    else:
        error = error or f"research API error (status {resp.status_code})"
    payload["error"] = error
    headers = {name: value for name, value in resp.headers.items()
               if name.lower() == "retry-after" or name.lower().startswith(("ratelimit", "x-ratelimit"))}
    if headers:
        payload["rate_limit_headers"] = headers
    if not data and isinstance(resp.text, str) and resp.text:
        payload["response_text"] = resp.text[:2000]
    _raise_tool_error(payload, status=resp.status_code)


def _api_result(resp, reject_failed_batch=False):
    _handle_error(resp)
    try:
        payload = resp.json()
    except ValueError:
        _raise_tool_error("research API returned invalid JSON", status=resp.status_code)
    if isinstance(payload, dict) and payload.get("error"):
        _raise_tool_error(payload, status=resp.status_code)
    if reject_failed_batch and isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results and all(
                isinstance(item, dict) and isinstance(item.get("status"), int)
                and item["status"] >= 400 for item in results):
            _raise_tool_error({**payload, "error": "all signed verdict judgments were rejected"},
                              status=resp.status_code)
    return payload


@mcp.tool()
def search_research_paper(
    query: str,
    layer: Optional[str] = None,
    section_type: Optional[str] = None,
    element_type: Optional[str] = None,
    terms: Optional[List[str]] = None,
    min_score: Optional[float] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    dedupe: bool = True,
    limit: int = 8,
    strict: bool = False,
) -> dict:
    """Semantic search over a curated full-text database of high-quality
    AI/LLM and Web3 research papers (arxiv), indexed by section (abstract,
    method, architecture, results, limitations, etc), not just abstracts.

    Args:
        query: Short natural language query (e.g. "graph neural network
            for molecule generation", "sybil resistance in prediction
            markets"), or a document's name or identifier ("HotStuff",
            "EIP-7702", "2303.11366"), which returns that document first
            (found_via "exact id" or "title match"). Prefer short and
            precise over long keyword lists.
        layer: Optional filter restricting results to one topical layer.
            One of: "llm-slm" (LLMs / small language models), "web3"
            (blockchain/crypto), "ai-agents" (agentic systems). Omit to
            search across all layers, and omit it when unsure. "ai-agents"
            and "llm-slm" are soft: each also searches the other, since
            agent classics are often tagged llm-slm only. "web3" and
            "builder-tech" are strict. With strict=True the requested layer
            is an exact filter, including "ai-agents" and "llm-slm".
        section_type: Optional filter restricting results to a specific
            paper section. One of: "method", "experiments" (results and
            benchmarks), "limitations", "analysis", "introduction",
            "related_work", "conclusion", "appendix", "other". Omit to
            search all section types.
        element_type: Optional filter restricting results to a structural
            element: "algorithm" (pseudocode blocks), "equation" (math),
            "table" (usually benchmark numbers), "code" (listings), or
            "prose". Use this to get implementable detail instead of
            narrative text, e.g. element_type="algorithm" for pseudocode.
        terms: Optional list of technical terms the paper must mention,
            e.g. ["kv cache"], ["mev", "rollup"]. Matches a CLOSED indexed
            vocabulary of concrete techniques and protocols (case, hyphens,
            spaces and plurals do not matter: "Chain of Thoughts" finds
            "chain-of-thought"). A term outside it (an EIP/ERC number, "pda",
            "pbs") cannot match anything: it is listed under
            "terms_resolution.unknown". By default the search runs without
            unknown terms; strict=True does not silently drop them. Put such
            identifiers in query instead.
        min_score: Leave unset. The default floor is calibrated per layer
            (0.79 for LLM text, 0.70 for web3) because cosine scores run high
            on this index. A value above the floor is capped at floor + 0.03
            (see "min_score_note"; hits under your value carry
            "below_min_score"), since 0.7-0.9 would drop good web3 hits.
            Lower it only to see weak matches deliberately.
        year_from: Earliest publication year to accept. In these fields a
            2019 result can be actively misleading, so pin recency when the
            question is about current practice: year_from=2025 for "what do
            people do now", left open for foundational work.
        year_to: Latest publication year to accept. Useful to ask what was
            known before a given paper appeared.
        dedupe: One hit per paper (default). Set False when you want several
            chunks of the same paper, e.g. to read a method across sections.
        limit: Max number of results to return (1-50, default 8).
        strict: Keep requested filters exact and disable automatic relaxation.
            Default False preserves the existing soft-layer / auto-relax
            behaviour; results may report dropped filters under "relaxed".

    Returns:
        dict with "results": a list of matched chunks, each containing title,
        url, section_type, section_title, text (excerpt), score, venue,
        citation_count, "fulltext" and "niche_score" (how strongly the paper
        belongs to its layer;
        1 means a single passing mention, which is how a graph-database paper
        once became top evidence for a blockchain claim). "licenses" maps
        each source label seen in the results to its license and reuse
        terms. Some records are abstract-only fallbacks, including IACR
        records without recovered open-access full text. Check actual evidence
        provenance before treating a passage as full text; a source-family
        "fulltext" label alone is not proof of body availability. Also "count",
        "filters_applied", and on an empty result "why_empty" naming the
        filter to relax. In default non-strict mode, "relaxed" may appear when
        too few results matched terms / section_type / element_type / year:
        the search was re-run
        without those filters, {"dropped": [...], "exact_matches": n,
        "note"}. Results with matches_filters=false are NOT exact matches
        (they lack the term / section / element / year you asked for): say so
        when you cite them, and read "relaxed" as "no exact match exists".
        Framework documentation (gh: ids) takes at most 2 of 10 places unless
        the query names that framework ("gh_docs_capped" counts the rest).
        "scope" says whether the
        query looks like a subject this index covers: {"in_scope": true|false|null,
        "confidence": "high"|"medium"|"low", "reason", "note"}. Results are
        never removed, so read it first: with in_scope=false the rows are
        only the nearest neighbours of an uncovered subject (the index
        covers web3, AI agents, LLMs and developer tooling), not literature
        on it; say so instead of presenting them as an answer. in_scope=true
        with confidence "low" means no indexed-topic term was recognised in
        the query, so judge relevance from the titles; in_scope=null means few
        close papers (a thin shelf or an uncovered subject), so check before relying.
    """
    body = {"query": query, "limit": limit, "compact": True, "strict": strict}
    if strict:
        body["auto_relax"] = False
    if layer:
        body["layer"] = layer
    if section_type:
        body["section_type"] = section_type
    if element_type:
        body["element_type"] = element_type
    if terms:
        body["terms"] = terms
    if min_score is not None:
        body["min_score"] = min_score
    if year_from is not None:
        body["year_from"] = year_from
    if year_to is not None:
        body["year_to"] = year_to
    if not dedupe:
        body["dedupe"] = False

    try:
        resp = requests.post(
            f"{RESEARCH_API_BASE}/v1/search", headers=_headers(), json=body, timeout=15
        )
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")

    return _api_result(resp)


@mcp.tool()
def get_research_bundle(
    query: str,
    layer: Optional[str] = None,
    limit: int = 3,
    max_chars: int = 12000,
    strict: bool = True,
) -> dict:
    """Get a bounded, read-only evidence packet for a research question.

    Search and reading context are assembled by the internal API. This tool
    does not record verdicts, modify the registry, or submit chain transactions.
    Read source provenance and abstract-only fallback markers before citing
    evidence; candidates are not automatically confirmed prior art.

    Args:
        query: Short research question, paper name or identifier.
        layer: Optional topical layer; omit when unsure.
        limit: Number of papers to include (default 3).
        max_chars: Evidence text budget in characters (default 12000).
        strict: Exact requested filters, without automatic relaxation
            (default True, unlike search_research_paper).

    Returns:
        The API's research bundle with evidence and provenance, or the same
        structured JSON diagnostics in an MCP tool error on failure.
    """
    body = {"query": query, "limit": limit, "max_chars": max_chars,
            "strict": strict}
    if layer:
        body["layer"] = layer
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/research/bundle",
                             headers=_headers(), json=body, timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def find_papers(
    query: str,
    layer: Optional[str] = None,
    sort: str = "relevance",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    limit: int = 10,
) -> dict:
    """List papers on a subject, one row per paper with its abstract, year,
    venue and citation count. Use this when the question is about papers
    ("the key papers on MEV", "what should I read on KV cache compression",
    "newest work on agent memory") rather than about a passage inside them.

    Args:
        query: The subject in plain words, e.g. "maximal extractable value",
            or a paper's name or identifier ("Reflexion", "EIP-7702"), which
            is returned first (found_via "title match" / "exact id").
        layer: Optional, leave unset if unsure: "web3", "ai-agents",
            "llm-slm" or "builder-tech". A named paper is found in any layer.
        sort: "relevance" (default); "foundational" (the papers the relevant
            ones cite most, found through the citation graph, so classics
            that predate today's vocabulary still appear: best for "where do
            I start" and "what are the key papers"); "citations" (most cited
            among the 30 most relevant); "recent" (newest first among them).
            For documents with no citation data (EIPs, whitepapers, code
            docs) foundational falls back to citations inside the corpus,
            then to relevance; it is never empty when relevance is not.
        year_from / year_to: Publication year window.
        limit: 1-30, default 10.

    Returns:
        dict with "papers": each {id, title, year, venue, citation_count,
        layers, source, url, fulltext, abstract, relevance_rank, and
        cited_by_pool for sort="foundational"}. Pass an id to get_paper to
        read further. Copies of the same paper from other sources are listed
        under "twins".
        Also "scope": whether the
        query looks like a subject this index covers: {"in_scope": true|false|null,
        "confidence": "high"|"medium"|"low", "reason", "note"}. Results are
        never removed, so read it first: with in_scope=false the rows are
        only the nearest neighbours of an uncovered subject (the index
        covers web3, AI agents, LLMs and developer tooling), not literature
        on it; say so instead of presenting them as an answer. in_scope=true
        with confidence "low" means no indexed-topic term was recognised in
        the query, so judge relevance from the titles; in_scope=null means few
        close papers (a thin shelf or an uncovered subject), so check before relying.
    """
    body = {"query": query, "sort": sort, "limit": limit}
    if layer:
        body["layer"] = layer
    if year_from is not None:
        body["year_from"] = year_from
    if year_to is not None:
        body["year_to"] = year_to
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/papers", headers=_headers(), json=body, timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def get_paper(paper_id: str) -> dict:
    """Open one paper: title, venue, citation count, full abstract, the
    outline of its indexed sections (with how many chunks of prose, math and
    tables each holds), and its citation neighbours inside the corpus: what it cites
    and what cites it, most cited first.

    Use it to judge a paper before reading it, to find the section worth
    reading, or to walk to earlier and later work on the same idea.

    Args:
        paper_id: e.g. "2405.15793", "iacr:2025/1040", "eip:1559", "simd:0297".

    Returns:
        dict with id, title, year, venue, citation_count, abstract, url,
        outline (each {section_title, section_type, chunks, elements}),
        cites / cited_by (up to 25 each, with cites_in_corpus and
        cited_by_in_corpus totals) and twins.
    """
    try:
        resp = requests.get(f"{RESEARCH_API_BASE}/v1/paper/{_path_id(paper_id)}", headers=_headers(), timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def read_paper_section(
    paper_id: str,
    section_title: Optional[str] = None,
    section_type: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 8000,
) -> dict:
    """Read the text of one section of a paper, in order, a page at a time.

    Args:
        paper_id: The paper, e.g. "1904.05234".
        section_title: Exact title from get_paper's outline, e.g. "Limitations".
        section_type: Instead of a title, every section of one kind:
            "introduction", "method", "experiments", "analysis",
            "limitations", "related_work", "conclusion", "appendix".
        offset: Character offset to continue from (next_offset of the
            previous call).
        max_chars: Page size, 500-20000, default 8000 (about 2000 tokens).

    Returns:
        dict with "text", "offset", "next_offset" (null at the end),
        "total_chars" and "url". Quote from it with attribution; do not
        reassemble whole papers.
    """
    params = {"offset": offset, "max_chars": max_chars}
    if section_title:
        params["title"] = section_title
    if section_type:
        params["section_type"] = section_type
    try:
        resp = requests.get(f"{RESEARCH_API_BASE}/v1/paper/{_path_id(paper_id)}/section", headers=_headers(),
                            params=params, timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def count_papers(
    query: Optional[str] = None,
    layer: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> dict:
    """Count papers instead of listing them: how much the corpus holds on a
    subject, per year, per layer, in which venues and under which technical
    terms. Use it to size a topic ("how many papers on account abstraction"),
    to see whether it is growing (by_year over the last years), to learn who
    publishes it (top_venues) and what sits next to it (top_terms: "what
    goes with X"), and to pick the year window before find_papers.

    Args:
        query: Optional subject in plain words. With it the counts cover the
            200 papers most relevant to it (a neighbourhood, so a small
            topic shows fewer than 200 and a big one is capped); without
            it, every paper in the layer and year window.
        layer: Optional: "web3", "ai-agents", "llm-slm" or "builder-tech".
        year_from / year_to: Publication year window.

    Returns:
        dict with total, by_year (ascending), by_layer, top_venues (15),
        top_terms (25, each {term, papers}), and with a query top_papers
        (5 cards). "sampled" appears if terms were counted over a sample.
        Copies of one paper count once.
        With a query also "scope" (as in find_papers): in_scope=false means
        the counts describe the nearest neighbours of a subject the index
        does not cover, so a large total there is not evidence of coverage.
        "counted_over" says what the counts were taken over.
    """
    body = {}
    if query:
        body["query"] = query
    if layer:
        body["layer"] = layer
    if year_from is not None:
        body["year_from"] = year_from
    if year_to is not None:
        body["year_to"] = year_to
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/facets", headers=_headers(), json=body, timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def similar_papers(paper_id: str, limit: int = 10, layer: Optional[str] = None) -> dict:
    """Papers closest in meaning to one paper, by title and abstract. Use it
    for "is there anything like this paper", "what else should I read after
    this one", and to find neighbours that share no references or wording
    with it (find_papers and the citation lists in get_paper miss those).

    Args:
        paper_id: e.g. "2310.06770", "iacr:2025/1040".
        limit: 1-30, default 10.
        layer: Optional: restrict neighbours to "web3", "ai-agents",
            "llm-slm" or "builder-tech".

    Returns:
        dict with "papers": cards as in find_papers plus "similarity"
        (cosine, higher is closer), most similar first. The paper itself
        and its copies are excluded. Error if the paper is not in the
        paper-level index.
    """
    params = {"limit": limit}
    if layer:
        params["layer"] = layer
    try:
        resp = requests.get(f"{RESEARCH_API_BASE}/v1/paper/{_path_id(paper_id)}/similar", headers=_headers(),
                            params=params, timeout=30)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def get_code_or_math_spec(
    arxiv_id: str,
    target_elements: Optional[str] = None,
    include_prose: bool = False,
    max_chars: int = 12000,
) -> dict:
    """Fetch the technical "spec" of a paper: its pseudocode, equations,
    listings and benchmark tables as raw LaTeX objects, NOT the abstract or
    narrative text. Use this to get implementable detail (exact algorithm
    steps, formulas, parameters) for writing code from a paper.

    Args:
        arxiv_id: Paper identifier, e.g. "2401.12345" for arXiv or
            "iacr:2025/1040" for an IACR ePrint paper.
        target_elements: Comma-separated subset of "algorithm,equation,
            code,table" (default: all four). Narrow it to keep context
            small, e.g. "algorithm" for pseudocode only.
        include_prose: Also return method-section prose. Off by default,
            since prose is what usually blows up the context budget.
        max_chars: Hard cap on returned characters (default 12000, roughly
            3000 tokens). The response reports "returned", "available" and
            "truncated" so you can ask for more deliberately.

    Returns:
        dict with "arxiv_id", "title", "sections" (each with element_type,
        section_type, section_title, text), plus "returned", "available",
        "chars" and "truncated". Returns an "error" key if the paper is not
        in the database or has no structured elements.
    """
    params = {"include_prose": str(bool(include_prose)).lower(), "max_chars": max_chars}
    if target_elements:
        params["target_elements"] = target_elements
    try:
        resp = requests.get(
            f"{RESEARCH_API_BASE}/v1/paper/{_path_id(arxiv_id)}/spec",
            headers=_headers(), params=params, timeout=30,
        )
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")

    return _api_result(resp)


@mcp.tool()
def compare_methods(
    arxiv_id_a: str,
    arxiv_id_b: str,
    extract_tables_only: bool = False,
    max_chars: int = 12000,
) -> dict:
    """Fetch the Results/Benchmarks (and Limitations) sections of two
    papers side by side, for comparing their reported performance and
    tradeoffs. The comparison/analysis itself is left to the calling
    model — this tool only retrieves the raw sections.

    Args:
        arxiv_id_a: id of the first paper, e.g. "2401.12345" (arXiv) or
            "iacr:2025/1040" (IACR ePrint).
        arxiv_id_b: id of the second paper.
        extract_tables_only: Return only table elements, which is where
            latency, memory and accuracy numbers usually live. Much
            cheaper in context than full results prose.
        max_chars: Hard cap per paper (default 12000, roughly 3000 tokens).

    Returns:
        dict with "a" and "b", each {arxiv_id, title, results_chunks,
        available, chars}. Returns an "error" key if either paper is not
        in the database.
    """
    try:
        resp = requests.get(
            f"{RESEARCH_API_BASE}/v1/compare",
            headers=_headers(),
            params={
                "a": arxiv_id_a,
                "b": arxiv_id_b,
                "extract_tables_only": str(bool(extract_tables_only)).lower(),
                "max_chars": max_chars,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")

    return _api_result(resp)


@mcp.tool()
def get_verdict_message(
    claim: str,
    paper_id: str,
    verdict: str,
    evidence_sha256: Optional[str] = None,
) -> dict:
    """Prepare the exact message to sign for a verifiable, on-chain verdict.

    Step one of filing a wallet-backed verdict; no chain transaction is
    submitted by this preparation call. The API may resolve or register a
    local claim node during preparation. It returns
    the canonical UTF-8 string this claim/paper/verdict combination hashes
    to. Sign it with the ed25519 secret key of a Solana wallet you control --
    the same primitive any Solana wallet already uses -- then pass the
    base58 signature and your base58 public key to record_signed_verdict.
    That call sends both to attestor, which recomputes this exact message
    server-side, verifies your signature against it, and only then writes
    the verdict to Solana devnet as a Solana Attestation Service (SAS)
    attestation. Anyone can independently verify your signature against the
    attested on-chain data without trusting dtox at all.

    Args:
        claim: The claim, in your own words. Wordings that mean the same
            thing resolve to one claim_id, returned here and required by
            record_signed_verdict.
        paper_id: The paper you are ruling on, e.g. "2401.12345".
        verdict: "asserts", "does_not_assert" or "partial".
        evidence_sha256: Optional hex sha256 of the evidence text backing
            the verdict. Leave unset if you have none.

    Returns:
        dict with "message" (the exact string to sign), "claim_id",
        "claim_sha256", "paper_id", "verdict", "evidence_sha256" and
        "issued_at" (stamped by the server -- pass it through unchanged to
        record_signed_verdict, since your signature covers it).
    """
    try:
        payload = {"claim": claim, "paper_id": paper_id, "verdict": verdict}
        if evidence_sha256:
            payload["evidence_sha256"] = evidence_sha256
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/verdict/message", headers=_headers(),
                             json=payload, timeout=15)
    except requests.RequestException as e:
        _raise_tool_error(f"research api unavailable: {e}", error_type="transport")
    return _api_result(resp)


@mcp.tool()
def record_signed_verdict(
    claim: str,
    judgments: List[dict],
    layer: Optional[str] = None,
) -> dict:
    """File a verdict signed by your own Solana wallet -- verifiable on-chain.

    Always available on this public endpoint, unlike record_claim_judgment:
    your identity here is your wallet's ed25519 signature, not a shared API
    key, so an anonymous caller cannot use it to poison the registry under
    someone else's name. Get the message to sign from get_verdict_message
    first, sign it with the ed25519 secret key of a Solana wallet, and pass
    the result here. attestor verifies the signature and writes a Solana
    Attestation Service (SAS) attestation on devnet before anything lands in
    the registry: a bad signature is rejected for that one item (status 400
    in its result entry) while the rest of the batch is still processed; if
    attestor itself cannot be reached the whole call fails and nothing is
    written.

    A verified wallet signature authenticates its signer, not independent
    reasoning. Agreement across wallets alone stays read_once (pending);
    confirmed_prior_art / ruled_out require known trusted model diversity.

    Args:
        claim: The exact claim text you called get_verdict_message with.
        judgments: One entry per paper, each
            {"id": "2401.12345", "verdict": "asserts", "reason": "...",
             "evidence_sha256": "<hex, optional>",
             "reviewer": "<your base58 Solana public key>",
             "signature": "<base58 ed25519 signature over the message
                             get_verdict_message returned>",
             "issued_at": "<the issued_at get_verdict_message returned>"}.
            verdict is "asserts", "does_not_assert" or "partial".
        layer: The layer the claim was audited in, if you used one.

    Returns:
        dict with per-item "results" (status 200 with attestation_pda /
        attestation_tx / explorer_url on success, or status 400/429 with an
        error otherwise), plus "papers" with each paper's confirmation
        status and an "onchain" list of its verified attestations
        ({reviewer, verdict, attestation, explorer_url}).
    """
    try:
        payload = {"claim": claim, "judgments": judgments}
        if layer:
            payload["layer"] = layer
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/adjudicate/signed", headers=_headers(),
                             json=payload, timeout=60)
    except requests.RequestException as e:
        _raise_tool_error(f"research api unavailable: {e}", error_type="transport")
    return _api_result(resp, reject_failed_batch=True)


def record_claim_judgment(claim: str, judgments: List[dict],
                          layer: Optional[str] = None,
                          judged_by_model: Optional[str] = None) -> dict:
    """File what you concluded after reading validate_project's candidates.

    Call this immediately after you have read the snippets and decided which
    papers actually assert the claim. This is not bookkeeping: a retrieval
    score cannot tell "does this" from "writes about this", so a candidate
    stays unproven until a reader rules on it. Your ruling is remembered and
    shown to whoever asks the same question next, which is the only thing that
    makes the index more precise over time.

    Judge honestly, including against your own interest: marking a paper as
    prior art when it merely shares vocabulary poisons the record for everyone,
    and so does clearing one because a clean result is convenient. Verdicts are
    settled only when independent readers agree, so a careless entry is visible
    as "contested" rather than silently accepted.

    Args:
        claim: The claim as passed to validate_project. Wordings that mean the
            same thing are filed against one claim node, so a verdict recorded
            here answers the same question asked in other words later.
        judgments: One entry per paper you read, as
            {"id": "2502.01068", "verdict": "asserts", "reason": "selects the
            reduction strategy per layer at inference", "score": 0.892,
            "band": "direct", "section_type": "method", "niche_score": 8}.
            verdict is "asserts", "does_not_assert" or "partial"; reason is one
            sentence from the text you read. Pass score, band, section_type and
            niche_score straight from the evidence item you are judging: they
            are what lets the bands be fitted to accepted verdicts later
            instead of to a percentile picked by hand.
        layer: The layer the claim was audited in, if you used one.
        judged_by_model: Optional self-declared attribution for the reading.
            This string alone is not trusted quorum provenance. Settlement
            requires known server-established model diversity; repeated
            readings by one known model are agreed_same_model, and wallet-only
            agreement without trusted model attribution is read_once (pending).

    Returns:
        dict with the stored count, the claim_id the verdicts were filed
        against, and per paper its status ("read_once", "agreed_same_model",
        "confirmed_prior_art", "ruled_out", "contested") with the number of
        recorded reader identities (not automatically independent readers).
    """
    if not REGISTRY_WRITES_ENABLED:
        _raise_tool_error({
            "error": "claim-registry writes are disabled on this public MCP endpoint",
            "next_step": "use an authenticated/private MCP deployment to submit judgments",
        }, status=403)
    try:
        payload = {"claim": claim, "judgments": judgments}
        if layer:
            payload["layer"] = layer
        if judged_by_model:
            payload["judged_by_model"] = judged_by_model
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/adjudicate", headers=_headers(),
                             json=payload, timeout=15)
        return _api_result(resp)
    except requests.RequestException as e:
        _raise_tool_error(f"research api unavailable: {e}", error_type="transport")


def link_claim_nodes(claim: str, same_as_claim_id: str, reason: str = "") -> dict:
    """Declare that two wordings of a claim ask the same question.

    validate_project returns "same_question_candidates": existing claim nodes
    whose wording sits close to yours. They are proposals, not matches. Cosine
    cannot make this call -- at the threshold that merged genuine rephrasings it
    also merged "speculative decoding" with "long-term memory for an agent" --
    so a reader has to decide, and that reader is you.

    Link only when the two sentences would be answered by the same papers. When
    they would not, leave them apart: a wrongly merged node hands the next
    caller a confident verdict about a question nobody judged.

    Args:
        claim: Your wording, exactly as passed to validate_project.
        same_as_claim_id: claim_id of the existing node, from
            same_question_candidates.
        reason: One sentence on why they are the same question.

    Returns:
        dict with the node the claim is now filed under and how many judgments
        that node carries.
    """
    if not REGISTRY_WRITES_ENABLED:
        _raise_tool_error({
            "error": "claim-registry writes are disabled on this public MCP endpoint",
            "next_step": "use an authenticated/private MCP deployment to link claims",
        }, status=403)
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/claims/link", headers=_headers(),
                             json={"claim": claim, "same_as_claim_id": same_as_claim_id,
                                   "reason": reason}, timeout=15)
        return _api_result(resp)
    except requests.RequestException as e:
        _raise_tool_error(f"research api unavailable: {e}", error_type="transport")


if REGISTRY_WRITES_ENABLED:
    mcp.tool()(record_claim_judgment)
    mcp.tool()(link_claim_nodes)


@mcp.tool()
def research_trends(
    layer: Optional[str] = None,
    year_from: int = 2023,
    year_to: Optional[int] = None,
    top: int = 20,
    max_share: float = 0.35,
    about: Optional[str] = None,
) -> dict:
    """Which techniques are gaining or losing ground in the literature, by year.

    Use this to answer "what is hot right now", "what replaced X", or to pick
    which approach to build on. Counts are per paper and normalised against
    the papers published each year, so a term does not look like it is growing
    just because the corpus grew.

    Args:
        layer: Optional topical layer: "llm-slm", "ai-agents" or "web3".
            Omit for all.
        year_from: First publication year of the window (default 2023).
        year_to: Last year of the window. Omit for "up to the latest".
        top: How many terms to return (1-100, default 20).
        about: Restrict the counts to papers on one subject, e.g. "KV cache
            compression for long context inference". "What replaced X" cannot be
            answered over the whole corpus, where every technique inside a
            subfield is a rounding error. The response then carries
            papers_in_topic.
        max_share: Terms appearing in more than this share of the corpus are
            reported separately under "excluded_as_vocabulary" instead of as
            trends (default 0.35). "language model" occurs in 89% of the
            LLM corpus: its share cannot move, so its growth number is
            normalisation noise crowding out real techniques. Raise towards
            1.0 to see everything.

    Returns:
        dict with "years", "papers_per_year", "trends" (each term with
        by_year counts, share_first_year, share_last_year, growth as a
        fraction, and an emerging flag), and "emerging": terms that were
        absent at the start of the window and present at the end.
    """
    params = {"year_from": year_from, "top": top, "max_share": max_share}
    if about:
        params["about"] = about
    if layer:
        params["layer"] = layer
    if year_to is not None:
        params["year_to"] = year_to
    try:
        resp = requests.get(f"{RESEARCH_API_BASE}/v1/trends", headers=_headers(),
                            params=params, timeout=90)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")

    return _api_result(resp)


@mcp.tool()
def validate_project(
    idea: str,
    claims: List[Union[str, dict]],
    layer: Optional[str] = None,
    evidence_per_claim: int = 4,
    deep: bool = False,
) -> dict:
    """Audit a project or research idea against the literature: prior art,
    known limitations, supporting math, and where the field is heading.

    Use this before building something, writing a grant application, or
    reviewing someone else's pitch. Break the idea into its distinct
    technical claims and pass them separately -- one claim per capability
    the project asserts -- because each is judged on its own evidence.

    Pass each claim with two or three rephrasings. Recall here is a similarity
    search, so a claim written in your words can miss a paper that says the same
    thing in its own: "entropy-based criterion for how aggressively to compress"
    returned nothing while "attention variance and information entropy to
    allocate cache budget" found the paper that does exactly that, published at
    a top venue. A false "nothing found" is the worst answer this tool can give,
    and you are the one who can prevent it.

    The public endpoint reads earlier claim judgments but is deliberately
    read-only. Registry mutations are exposed only by an authenticated private
    deployment. A claim whose candidates nobody has read yet says so in
    "settled".

    Two channels feed the answer. Retrieval depends on wording, so pass
    rephrasings. The citation graph does not: papers one hop from the best hits
    arrive under "graph_candidates" whatever words you chose. And the registry
    outranks both -- if a reader has already judged a paper against this claim,
    that verdict is returned even when today's phrasing buries the paper in
    weaker_matches.

    Scores are not comparable across layers and are not compared for you: the
    bands are calibrated inside each layer (measured on-topic hits land at
    0.90 in llm-slm and 0.81 in web3), so "direct" means the same percentile
    position everywhere. Read "corpus_coverage" before believing a quiet
    result: an empty answer over 800 indexed papers is evidence, over 3 it is
    an empty shelf. An idea the index knows nothing about is refused outright
    with in_scope=false rather than answered with "no match".

    YOU must settle the verdict, not the score. A "strong_candidates" result
    means those papers sit where the same idea usually sits by wording alone.
    Before you tell anyone an idea is taken, read each snippet and decide
    whether that paper actually asserts this exact claim; drop the ones that
    merely share vocabulary, and say which survived. Items under
    "context_mentions" come from related-work sections and describe OTHER
    papers' results, so they never prove what the citing paper does.

    Every verdict ships with the corpus size behind it. A claim with no
    match is a lead worth a patent search, NOT a novelty certificate: this
    index is curated and finite while the literature is not. It holds no
    patents at all, so it cannot speak to filing decisions. Do not report
    a "novelty score" to the user on the strength of an empty result.

    Args:
        idea: One-line description of the project.
        claims: The technical claims to audit, phrased as the paper that
            would describe them, e.g. "recursive compression of already
            compressed latent representations". 1-8 items. Either a plain
            string, or {"claim": "...", "phrasings": ["...", "..."]} with your
            own rephrasings; every phrasing is searched and the results are
            merged, with "found_via" telling you which wording surfaced each
            paper. Use the dict form unless you have a reason not to.
        layer: Optional scope: "llm-slm", "ai-agents" or "web3". Worth
            setting for blockchain work, which the LLM-heavy corpus would
            otherwise drown out.
        evidence_per_claim: Papers cited per claim (1-10, default 4).
        deep: False returns retrieval, registry and coverage quickly. True also
            expands the citation graph, reranks candidates and extracts
            limitations and supporting equations.

    Returns:
        dict with "headline", "corpus" (index size and coverage), "scope"
        (what the index does not hold, patents included), "start_here"
        (limitations of the papers actually cited), "verdict_summary", and
        per-claim reports: verdict, "settled" counts, evidence with real ids,
        score bands, citation counts, venues, "prior_reading" from earlier
        readers, known_limitations, supporting_math as raw LaTeX, plus
        "recall_note" whenever a claim found nothing strong. Cite only the ids
        it returns.
    """
    body = {"idea": idea, "claims": claims, "evidence_per_claim": evidence_per_claim,
            "depth": "full" if deep else "fast"}
    if layer:
        body["layer"] = layer
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/validate", headers=_headers(),
                             json=body, timeout=180)
    except requests.RequestException as e:
        _raise_tool_error(f"could not reach research API: {e}", error_type="transport")

    return _api_result(resp)


if __name__ == "__main__":
    import uvicorn
    from utf8_guard import Utf8Guard

    uvicorn.run(Utf8Guard(mcp.streamable_http_app()), host=MCP_HOST, port=MCP_PORT,
                log_level="info")
