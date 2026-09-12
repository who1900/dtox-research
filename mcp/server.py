"""
dtox research MCP server.

Exposes 3 tools over MCP (streamable-http transport) that proxy to the
internal dtox-research-api (127.0.0.1:8010), which serves semantic search
over a full-text database of curated AI/LLM/Web3 arxiv papers.

The server holds its own server-side X-API-Key for the internal API;
MCP clients never see it.
"""

import json
import os
from pathlib import Path
from typing import List, Optional, Union

import requests
from mcp.server.fastmcp import FastMCP
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

mcp = FastMCP(
    "dtox-research",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    transport_security=transport_security,
)


def _headers():
    return {"X-API-Key": RESEARCH_API_KEY, "Content-Type": "application/json"}


def _handle_error(resp: requests.Response) -> Optional[dict]:
    """Return a structured error dict for non-2xx responses, else None."""
    if resp.status_code == 401:
        return {"error": "internal auth error contacting research API (401)"}
    if resp.status_code == 404:
        try:
            detail = resp.json().get("detail", "not found")
        except Exception:
            detail = "not found"
        # the API detail already says what was not found; prefixing it again
        # produced "not found: paper not found"
        return {"error": detail}
    if resp.status_code == 429:
        return {"error": "rate limit exceeded on research API, try again shortly"}
    if resp.status_code == 400:
        try:
            detail = resp.json().get("detail", "bad request")
        except Exception:
            detail = "bad request"
        return {"error": f"bad request: {detail}"}
    if not resp.ok:
        return {"error": f"research API error (status {resp.status_code})"}
    return None


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
) -> dict:
    """Semantic search over a curated full-text database of high-quality
    AI/LLM and Web3 research papers (arxiv), indexed by section (abstract,
    method, architecture, results, limitations, etc), not just abstracts.

    Args:
        query: Natural language search query (e.g. "graph neural network
            for molecule generation", "sybil resistance in prediction
            markets").
        layer: Optional filter restricting results to one topical layer.
            One of: "llm-slm" (LLMs / small language models), "web3"
            (blockchain/crypto), "ai-agents" (agentic systems). Omit to
            search across all layers.
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
        terms: Optional list of exact technical terms the paper must
            mention, e.g. ["kv cache"], ["mev", "rollup"], ["durable
            nonce"]. Matches an indexed vocabulary of concrete techniques
            and protocols, so it is far more precise than semantics alone.
        min_score: Relevance floor (default 0.79). Cosine scores run high
            on this index, so off-topic queries otherwise return
            confident-looking noise around 0.75; below the floor the
            database reports no results instead. Lower it only to see
            weak matches deliberately.
        year_from: Earliest publication year to accept. In these fields a
            2019 result can be actively misleading, so pin recency when the
            question is about current practice: year_from=2025 for "what do
            people do now", left open for foundational work.
        year_to: Latest publication year to accept. Useful to ask what was
            known before a given paper appeared.
        dedupe: One hit per paper (default). Set False when you want several
            chunks of the same paper, e.g. to read a method across sections.
        limit: Max number of results to return (1-50, default 8).

    Returns:
        dict with "results": a list of matched chunks, each containing title,
        arxiv_url, section_type, section_title, text (excerpt), score, venue,
        citation_count, "fulltext" (false for IACR records, which are indexed
        by abstract only, so section and element filters cannot reach inside
        them) and "niche_score" (how strongly the paper belongs to its layer;
        1 means a single passing mention, which is how a graph-database paper
        once became top evidence for a blockchain claim). Also "count",
        "filters_applied", and on an empty result "why_empty" naming the
        filter to relax.
    """
    body = {"query": query, "limit": limit}
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
        return {"error": f"could not reach research API: {e}"}

    err = _handle_error(resp)
    if err:
        return err
    return resp.json()


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
            f"{RESEARCH_API_BASE}/v1/paper/{arxiv_id}/spec",
            headers=_headers(), params=params, timeout=30,
        )
    except requests.RequestException as e:
        return {"error": f"could not reach research API: {e}"}

    err = _handle_error(resp)
    if err:
        return err
    return resp.json()


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
        return {"error": f"could not reach research API: {e}"}

    err = _handle_error(resp)
    if err:
        return err
    return resp.json()


@mcp.tool()
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
        judged_by_model: Who did the reading, e.g. "claude-opus-5" or a human's
            name. Agreement only counts as settled when it comes from readers
            that can actually differ: two runs of one model repeating each
            other is correlation, not confirmation, and is reported as
            agreed_same_model instead.

    Returns:
        dict with the stored count, the claim_id the verdicts were filed
        against, and per paper its status ("read_once", "agreed_same_model",
        "confirmed_prior_art", "ruled_out", "contested") with the number of
        independent readers.
    """
    if not REGISTRY_WRITES_ENABLED:
        return {
            "error": "claim-registry writes are disabled on this public MCP endpoint",
            "next_step": "use an authenticated/private MCP deployment to submit judgments",
        }
    try:
        payload = {"claim": claim, "judgments": judgments}
        if layer:
            payload["layer"] = layer
        if judged_by_model:
            payload["judged_by_model"] = judged_by_model
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/adjudicate", headers=_headers(),
                             json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"research api unavailable: {e}"}


@mcp.tool()
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
        return {
            "error": "claim-registry writes are disabled on this public MCP endpoint",
            "next_step": "use an authenticated/private MCP deployment to link claims",
        }
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/claims/link", headers=_headers(),
                             json={"claim": claim, "same_as_claim_id": same_as_claim_id,
                                   "reason": reason}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"research api unavailable: {e}"}


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
        return {"error": f"could not reach research API: {e}"}

    err = _handle_error(resp)
    if err:
        return err
    return resp.json()


@mcp.tool()
def validate_project(
    idea: str,
    claims: List[Union[str, dict]],
    layer: Optional[str] = None,
    evidence_per_claim: int = 4,
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

    This tool is half of a loop. It finds candidates; you decide; you file the
    decision with record_claim_judgment so the next caller starts from a
    reading instead of a cosine score. A claim whose candidates nobody has
    read yet says so in "settled".

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
    body = {"idea": idea, "claims": claims, "evidence_per_claim": evidence_per_claim}
    if layer:
        body["layer"] = layer
    try:
        resp = requests.post(f"{RESEARCH_API_BASE}/v1/validate", headers=_headers(),
                             json=body, timeout=180)
    except requests.RequestException as e:
        return {"error": f"could not reach research API: {e}"}

    err = _handle_error(resp)
    if err:
        return err
    return resp.json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
