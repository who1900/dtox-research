"""Bounded, sanitized acceptance probe for public read-only MCP profiles.

Status measures protocol/schema and evidence correctness, not corpus completeness.
Explicit character/chunk/source bounds and unavailable indexed elements are
reported separately in completeness and do not by themselves fail correctness.
Time exhaustion, retrieval errors, missing/invalid provenance and unexplained partials fail closed,
even when budget clipping coexists. Legacy partial/truncated flags are retained.
Completeness describes reported indexed evidence only, never complete parsing or
proof that an element is absent from the original source. No extra reads are made.
"""

import argparse
import json
import time
from urllib.parse import urlparse

import requests

try:
    from ops.release_readonly_probe import _decode_response, _flags, _tool_payload
except ModuleNotFoundError:
    from release_readonly_probe import _decode_response, _flags, _tool_payload


MAX_HTTP_REQUESTS = 12
MAX_TOOL_CALLS = 9
TOTAL_TIMEOUT = 120
REQUEST_TIMEOUT = (3, 12)
EXPECTED_TOOLS = {
    "search_research_paper", "get_research_bundle", "find_papers", "get_paper",
    "read_paper_section", "count_papers", "similar_papers", "get_code_or_math_spec",
    "compare_methods", "get_verdict_message", "record_signed_verdict",
    "research_trends", "validate_project",
}

# Same technical question, materially different wording; these are probes, not gold labels.
PARAPHRASES = (
    "How can autonomous language-model agents retrieve external research and cite evidence while reducing hallucinations?",
    "What methods ground LLM agent answers in retrieved documents and preserve traceable citations?",
    "How do research assistants built with large language models select sources and attribute claims reliably?",
)
NEGATIVE_QUERY = "How do real-estate agents search property listings and match homes to buyers?"
DISCOVERY_QUERY = "AI agents grounded research retrieval citation attribution"


BUDGET_REASONS = {"character_budget", "chunk_limit", "source_limit"}
AVAILABILITY_REASONS = {"abstract_only", "metadata_missing", "no_indexed_chunks",
                        "source_resolution_incomplete", "not_found_in_retrieved_chunks",
                        "expected_unavailable"}
FAILURE_REASONS = {"retrieval_failed", "invalid_provenance", "provenance_missing",
                   "search_partial", "unexplained_partial", "time_budget", "empty_budget_page"}
QUALITY_REASONS = BUDGET_REASONS | AVAILABILITY_REASONS | FAILURE_REASONS


def _valid_evidence_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(
            ord(char) < 32 or char.isspace() for char in value):
        return False
    try:
        parsed = urlparse(value)
        return bool(parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                    and parsed.username is None and parsed.password is None
                    and (parsed.port is None or parsed.port >= 0))
    except (ValueError, UnicodeError):
        return False


def _quality_reasons(value, tool_name, resolution=False):
    """Use bounded labels only; never copy error text or arbitrary reason values."""
    reasons = set()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                reasons.update(_quality_reasons(item, tool_name))
        return reasons
    if not isinstance(value, dict):
        return reasons
    supplied = value.get("partial_reasons")
    if isinstance(supplied, list):
        reasons.update(reason for reason in supplied if isinstance(reason, str) and reason in QUALITY_REASONS)
        if any(not isinstance(reason, str) or reason not in QUALITY_REASONS for reason in supplied):
            reasons.add("unexplained_partial")
    elif "partial_reasons" in value:
        reasons.add("unexplained_partial")
    if (value.get("retrieval_error") and not (
            value.get("time_budget_exhausted") is True and "time_budget" in reasons)) or (
            value.get("retrieval_warning") or value.get("errors")):
        reasons.add("retrieval_failed")
    if value.get("provenance_missing") is True:
        reasons.add("provenance_missing")
    if value.get("provenance_reason") == "source_url_invalid":
        reasons.add("invalid_provenance")
    if any(value.get(key) not in (None, "") and not _valid_evidence_url(value[key])
           for key in ("source_url", "url")):
        reasons.add("invalid_provenance")
    if value.get("search_partial") is True:
        reasons.add("search_partial")
    if value.get("time_budget_exhausted") is True:
        reasons.add("time_budget")
    if value.get("chunk_limit_reached") is True or (resolution and value.get("chunk_limit")):
        reasons.add("chunk_limit")
    if any(value.get(key) is True for key in ("truncated", "is_truncated", "clipped")):
        reasons.add("character_budget")
    if isinstance(value.get("text"), str) and isinstance(value.get("total_chars"), int):
        offset = value.get("offset", 0)
        if isinstance(offset, int) and offset + len(value["text"]) < value["total_chars"]:
            reasons.add("character_budget")
    missing = value.get("missing")
    if isinstance(missing, dict):
        reasons.update(reason for reason in missing.values() if isinstance(reason, str) and reason in QUALITY_REASONS)
    if value.get("fulltext") is False:
        reasons.add("abstract_only")
    if "fulltext" in value and value["fulltext"] is None:
        reasons.add("metadata_missing")
    if tool_name == "get_code_or_math_spec" and value.get("available") == 0:
        reasons.add("not_found_in_retrieved_chunks")
    if tool_name == "get_code_or_math_spec" and type(value.get("available")) is int and (
            value["available"] > 0 and value.get("sections") == [] and value.get("truncated") is True):
        reasons.add("empty_budget_page")
    for key, child in value.items():
        if isinstance(child, (dict, list)) and key not in {"partial_reasons", "missing", "errors"}:
            reasons.update(_quality_reasons(child, tool_name, resolution=key in {"source_resolution", "resolution"}))
    if value.get("partial") is True:
        if resolution and not value.get("errors") and not value.get("time_budget_exhausted"):
            if not reasons:
                reasons.add("source_resolution_incomplete")
        elif tool_name in {"search_research_paper", "find_papers", "similar_papers"}:
            reasons.add("search_partial")
        elif not reasons:
            reasons.add("unexplained_partial")
    return reasons


def _completeness(reasons):
    status = ("incomplete" if reasons & FAILURE_REASONS else
              "bounded" if reasons & BUDGET_REASONS else
              "unavailable" if reasons & AVAILABILITY_REASONS else "complete")
    return {"status": status, "reasons": sorted(reasons)}


def _cards(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("papers", "results", "related"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _paper_summaries(value, limit=8):
    """Extract bounded card metadata from nested bundle evidence, never text."""
    rows, seen, stack = [], set(), [value]
    while stack and len(rows) < limit:
        item = stack.pop()
        if isinstance(item, dict):
            paper_id = item.get("id") or item.get("paper_id") or item.get("arxiv_id")
            if paper_id and (item.get("title") or item.get("source_url_origin") or "fulltext" in item):
                key = str(paper_id)
                if key not in seen:
                    seen.add(key)
                    fulltext = item.get("fulltext")
                    fulltext_state = ("unknown" if fulltext is None else
                        "present" if fulltext is True else "absent" if fulltext is False else
                        "abstract_only" if isinstance(fulltext, str) and fulltext.lower() in {"abstract", "abstract-only"} else
                        "fulltext" if isinstance(fulltext, str) and fulltext.lower() == "fulltext" else "unrecognized")
                    rows.append({"id": key, "title": item.get("title"),
                        "source": item.get("source"), "source_url_present": bool(item.get("url") or item.get("source_url")),
                        "source_url_origin": item.get("source_url_origin"),
                        "source_url_status": item.get("source_url_status"),
                        "provenance_missing": item.get("provenance_missing"),
                        "fulltext_state": fulltext_state})
            stack.extend(v for v in reversed(list(item.values())) if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return rows


def run_probe(endpoint, profile="reads", post=requests.post):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be HTTPS without query or fragment")
    started = time.monotonic()
    if profile not in {"reads", "semantic"}:
        raise ValueError("profile must be reads or semantic")
    report = {"profile": profile, "status": "fail", "http_request_cap": MAX_HTTP_REQUESTS,
              "tool_call_cap": MAX_TOOL_CALLS, "http_requests_attempted": 0,
              "http_requests_completed": 0, "tool_calls_attempted": 0,
              "inventory_count": None, "inventory_matches_13": False,
              "partial": False, "truncated": False, "budget_exhausted": False,
              "elapsed_ms": 0, "steps": [], "errors": [],
              "runtime_tool_coverage": [], "tool_errors": 0,
              "scope_audit": {"validate_project": "api/main.py validate route inspected: claim registry paths use mode=ro and SELECT only; no claim_nodes/claim_judgments mutation in handler. Not in these bounded profile tool sets.",
                              "get_verdict_message": "mcp/server.py and api/main.py proxy to attestor /message; attestor/src/server.ts builds a message and timestamp only, with no DB/nonce persistence. Not in the requested nine-read profile."},
              "interpretation": "Paraphrase overlap is descriptive only; without gold labels it is not recall, accuracy, or relevance proof."}
    session = None
    quality_reasons = set()

    def send(name, message, is_tool=False, read_timeout=None):
        nonlocal session
        if report["http_requests_attempted"] >= MAX_HTTP_REQUESTS or (
                time.monotonic() - started >= TOTAL_TIMEOUT):
            report["budget_exhausted"] = True
            report["errors"].append({"step": name, "kind": "request_budget"})
            return None
        report["http_requests_attempted"] += 1
        if is_tool:
            report["tool_calls_attempted"] += 1
        elapsed = time.monotonic() - started
        remaining = max(0.2, TOTAL_TIMEOUT - elapsed)
        connect_timeout = min(REQUEST_TIMEOUT[0], max(0.1, remaining / 2))
        timeout = (connect_timeout, min(read_timeout or REQUEST_TIMEOUT[1],
                   max(0.1, remaining - connect_timeout)))
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if session:
            headers["Mcp-Session-Id"] = session
        mark = time.monotonic()
        try:
            response = post(endpoint, json=message, headers=headers,
                            timeout=timeout, allow_redirects=False)
            report["http_requests_completed"] += 1
            session = response.headers.get("Mcp-Session-Id") or session
            report["steps"].append({"name": name, "http_status": response.status_code,
                                    "latency_ms": round((time.monotonic() - mark) * 1000, 1)})
            if not 200 <= response.status_code < 300:
                report["errors"].append({"step": name, "kind": "http_status"})
                return None
            decoded = _decode_response(response)
            if name == "notifications/initialized" and response.status_code in (200, 202, 204):
                return decoded
            if not isinstance(decoded, dict):
                report["errors"].append({"step": name, "kind": "malformed_json_rpc"})
                return None
            return decoded
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            report["steps"].append({"name": name,
                                    "latency_ms": round((time.monotonic() - mark) * 1000, 1)})
            report["errors"].append({"step": name, "kind": "transport_or_decode"})
            return None

    def tool(name, args, read_timeout=None):
        request_id = len(report["steps"]) + 1
        answer = send(name, {"jsonrpc": "2.0", "id": request_id,
                             "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, True, read_timeout)
        if not isinstance(answer, dict) or answer.get("id") != request_id:
            report["errors"].append({"step": name, "kind": "invalid_json_rpc"})
            return None
        result = answer.get("result")
        if not isinstance(result, dict) or type(result.get("isError")) is not bool:
            report["errors"].append({"step": name, "kind": "malformed_tool_result"})
            return None
        partial, truncated = _flags(result)
        report["partial"] |= partial
        report["truncated"] |= truncated
        data = _tool_payload(result)
        report["runtime_tool_coverage"].append(name)
        step = report["steps"][-1]
        step["tool_status"] = "error" if result["isError"] else "ok"
        step["partial"] = partial
        step["reported_truncated"] = truncated
        if result["isError"]:
            status = data.get("status") if isinstance(data, dict) else None
            allowed_missing = (name in {"read_paper_section", "get_code_or_math_spec"}
                               and status == 404)
            step["tool_status"] = "expected_unavailable" if allowed_missing else "error"
            if not allowed_missing:
                report["tool_errors"] += 1
                report["errors"].append({"step": name, "kind": "tool_error"})
            else:
                quality_reasons.add("expected_unavailable")
                step["completeness"] = _completeness({"expected_unavailable"})
            return {"_unavailable_status": status} if allowed_missing else data
        if not isinstance(data, dict) or data.get("error"):
            report["errors"].append({"step": name, "kind": "malformed_payload"})
            return None
        schemas = {"find_papers": ("papers", list), "get_paper": ("id", str),
            "count_papers": ("total", int), "similar_papers": ("papers", list),
            "read_paper_section": ("text", str), "get_code_or_math_spec": ("sections", list),
            "compare_methods": ("a", dict), "get_research_bundle": ("papers", list),
            "research_trends": ("trends", list), "search_research_paper": ("results", list)}
        field, expected = schemas[name]
        if field not in data or not isinstance(data[field], expected) or (
                expected is str and not data[field].strip()) or (
                expected is int and isinstance(data[field], bool)) or (
                name == "compare_methods" and not isinstance(data.get("b"), dict)):
            report["errors"].append({"step": name, "kind": "malformed_payload"})
            return None
        reasons = _quality_reasons(data, name)
        if partial and not reasons:
            reasons.add("unexplained_partial")
        quality_reasons.update(reasons)
        step["completeness"] = _completeness(reasons)
        if name in {"read_paper_section", "get_code_or_math_spec"}:
            step["continuation_available"] = (data.get("next_offset") is not None or
                                               isinstance(data.get("next_cursor"), str) and bool(data["next_cursor"]))
        for reason in sorted(reasons & FAILURE_REASONS):
            report["errors"].append({"step": name, "kind": reason})
        return data

    initialized = send("initialize", {"jsonrpc": "2.0", "id": 1,
        "method": "initialize", "params": {"protocolVersion": "2025-03-26",
        "capabilities": {}, "clientInfo": {"name": "dtox-expanded-readonly", "version": "1"}}})
    if not isinstance(initialized, dict) or initialized.get("id") != 1:
        report["errors"].append({"step": "initialize", "kind": "invalid_json_rpc"})
    if not report["errors"]:
        send("notifications/initialized", {"jsonrpc": "2.0",
             "method": "notifications/initialized"})
    if not report["errors"]:
        inventory = send("tools/list", {"jsonrpc": "2.0", "id": 2,
                         "method": "tools/list", "params": {}})
        tools = inventory.get("result", {}).get("tools", []) if isinstance(inventory, dict) else []
        if not isinstance(tools, list):
            tools = []
        names = {x.get("name") for x in tools if isinstance(x, dict)}
        report["inventory_count"] = len(names)
        report["inventory_matches_13"] = len(names) == 13 and names == EXPECTED_TOOLS
        if not isinstance(inventory, dict) or inventory.get("id") != 2 or not report["inventory_matches_13"]:
            report["errors"].append({"step": "tools/list", "kind": "inventory_mismatch"})

    if profile == "reads" and not report["errors"]:
        found = tool("find_papers", {"query": DISCOVERY_QUERY, "layer": "ai-agents", "limit": 2})
        found_cards = _cards(found)
        papers = [p for p in found_cards if isinstance(p, dict) and (p.get("id") or p.get("arxiv_id"))]
        report["discovery"] = _paper_summaries(found_cards)
        if not papers:
            report["errors"].append({"step": "find_papers", "kind": "empty_or_invalid_discovery"})
        else:
            paper_id = papers[0].get("id") or papers[0].get("arxiv_id")
            paper = tool("get_paper", {"paper_id": paper_id}) or {}
            report["paper_read"] = {"id": paper_id, "title": paper.get("title"),
                "source": paper.get("source"), "source_url_present": bool(paper.get("url")),
                "source_url_origin": paper.get("source_url_origin"),
                "source_url_status": paper.get("source_url_status"),
                "provenance_missing": paper.get("provenance_missing"),
                "fulltext_state": "unknown" if paper.get("fulltext") is None else "present" if paper.get("fulltext") is True else "absent" if paper.get("fulltext") is False else "unrecognized",
                "outline_count": len(paper.get("outline", [])) if isinstance(paper.get("outline"), list) else None}
            counts = tool("count_papers", {"query": "decentralized consensus cryptographic verification", "layer": "web3"}) or {}
            report["count"] = {"total": counts.get("total"), "by_year": counts.get("by_year"),
                "sampled": counts.get("sampled"), "counted_over": counts.get("counted_over"),
                "scope": {k: counts.get("scope", {}).get(k) for k in ("in_scope", "confidence")}
                    if isinstance(counts.get("scope"), dict) else None}
            similar = tool("similar_papers", {"paper_id": paper_id, "limit": 2, "layer": "ai-agents"}) or {}
            report["similar"] = _paper_summaries(similar)
            outline = paper.get("outline", []) if isinstance(paper.get("outline"), list) else []
            section = next((x for x in outline if isinstance(x, dict) and x.get("section_title")), None)
            section_args = {"paper_id": paper_id, "max_chars": 1000}
            if section:
                section_args["section_title"] = section["section_title"]
            else:
                section_args["section_type"] = "conclusion"
            section_result = tool("read_paper_section", section_args) or {}
            report["section_read"] = ({"available": False, "status": section_result.get("_unavailable_status")}
                if isinstance(section_result, dict) and "_unavailable_status" in section_result else {
                    "available": isinstance(section_result.get("text"), str),
                    "returned_chars": len(section_result.get("text", "")) if isinstance(section_result, dict) and isinstance(section_result.get("text"), str) else None,
                    "total_chars": section_result.get("total_chars") if isinstance(section_result, dict) else None,
                    "source_url_present": bool(section_result.get("url")) if isinstance(section_result, dict) else False})
            spec = tool("get_code_or_math_spec", {"arxiv_id": paper_id,
                "target_elements": "algorithm,equation", "include_prose": False, "max_chars": 1000})
            spec = spec or {}
            report["spec_read"] = ({"available": False, "status": spec.get("_unavailable_status"), "expected_empty_elements": True}
                if isinstance(spec, dict) and "_unavailable_status" in spec else {
                    "available": isinstance(spec.get("sections"), list), "section_count": len(spec.get("sections", [])) if isinstance(spec.get("sections"), list) else None,
                    "returned_elements": spec.get("returned"), "available_elements": spec.get("available"),
                    "returned_chars": spec.get("chars"),
                    "reported_truncated": spec.get("truncated", False),
                    "next_cursor_present": isinstance(spec.get("next_cursor"), str) and bool(spec["next_cursor"]),
                    "source_url_present": bool(spec.get("url")),
                    "source_url_origin": spec.get("source_url_origin"),
                    "source_url_status": spec.get("source_url_status"),
                    "provenance_missing": spec.get("provenance_missing")})
            bundle = tool("get_research_bundle", {"query": "retrieval augmented language models grounded in cited source evidence",
                "layer": "llm-slm", "limit": 2, "max_chars": 1000, "strict": True}, read_timeout=8)
            bundle = bundle or {}
            bundle_cards = _paper_summaries(bundle)
            report["bundle"] = {"paper_count": len(bundle_cards), "papers": bundle_cards,
                "missing": bundle.get("missing"), "budgets": bundle.get("budgets")}
            distinct = [p["id"] for p in bundle_cards if p.get("id")]
            compare_ids = (distinct + [distinct[0] if distinct else paper_id])[:2]
            if len(compare_ids) < 2:
                compare_ids.append(compare_ids[0])
            comparison = tool("compare_methods", {"arxiv_id_a": compare_ids[0],
                "arxiv_id_b": compare_ids[1], "extract_tables_only": True, "max_chars": 1000})
            comparison = comparison or {}
            report["compare"] = {"ids": compare_ids, "distinct_pair": compare_ids[0] != compare_ids[1],
                "a_available": comparison.get("a", {}).get("available") if isinstance(comparison.get("a"), dict) else None,
                "b_available": comparison.get("b", {}).get("available") if isinstance(comparison.get("b"), dict) else None}
            trends = tool("research_trends", {"layer": "web3", "year_from": 2023, "top": 5}) or {}
            report["trends"] = {"year_count": len(trends.get("years", [])) if isinstance(trends.get("years"), list) else None,
                "paper_years": trends.get("papers_per_year"), "trend_count": len(trends.get("trends", [])) if isinstance(trends.get("trends"), list) else None}
    elif profile == "semantic" and not report["errors"]:
        semantic_queries = [(PARAPHRASES[0], "ai-agents", "paraphrase"),
            (PARAPHRASES[1], "ai-agents", "paraphrase"),
            (PARAPHRASES[2], "ai-agents", "paraphrase"),
            (NEGATIVE_QUERY, "ai-agents", "negative_scope_only"),
            ("How do language models ground generated answers in retrieved passages?", "llm-slm", "distinct_llm_question"),
            ("How do decentralized consensus protocols verify validator messages cryptographically?", "web3", "distinct_web3_question")]
        report["semantic_probes"] = []
        for query, layer, kind in semantic_queries:
            result = tool("search_research_paper", {"query": query, "layer": layer,
                "limit": 2, "strict": True})
            result = result or {}
            rows = _cards(result)
            report["semantic_probes"].append({"kind": kind, "layer": layer,
                "count": result.get("count"), "scope": {k: result.get("scope", {}).get(k)
                    for k in ("in_scope", "confidence")} if isinstance(result.get("scope"), dict) else None,
                "papers": _paper_summaries(rows, limit=2)})
        ids_by_phrase = [set(p.get("id") for p in probe["papers"] if p.get("id"))
                         for probe in report["semantic_probes"][:3]]
        report["paraphrase_overlap"] = {"top_ids": [sorted(ids) for ids in ids_by_phrase],
            "pairwise_shared_ids": [len(ids_by_phrase[a] & ids_by_phrase[b])
                for a, b in ((0, 1), (0, 2), (1, 2))]}

    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    if report["elapsed_ms"] >= TOTAL_TIMEOUT * 1000:
        report["budget_exhausted"] = True
        report["errors"].append({"step": "total", "kind": "time_budget"})
    report["error_count"] = len(report["errors"])
    report["correctness"] = "fail" if report["errors"] or report["budget_exhausted"] else "pass"
    report["completeness"] = _completeness(quality_reasons)
    if report["correctness"] == "fail":
        report["completeness"]["status"] = "incomplete"
    if report["correctness"] == "pass":
        report["status"] = "pass"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://read.whoim.space/mcp")
    parser.add_argument("--profile", choices=("reads", "semantic"), required=True)
    args = parser.parse_args()
    try:
        report = run_probe(args.endpoint, profile=args.profile)
    except ValueError as error:
        report = {"status": "fail", "errors": [{"step": "configuration", "kind": str(error)}],
                  "error_count": 1, "http_requests_attempted": 0, "tool_calls_attempted": 0}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
