"""Bounded, read-only MCP audit probe for the frozen search/API candidate."""

import argparse
import hashlib
import json
import re
import time
from urllib.parse import urlparse

import requests


MAX_HTTP_CALLS = 10
REQUEST_TIMEOUT_SECONDS = 20
PROBE_REPORT_VERSION = 2
EXPECTED_TOOLS = {
    "search_research_paper", "get_research_bundle", "find_papers", "get_paper",
    "read_paper_section", "count_papers", "similar_papers", "get_code_or_math_spec",
    "compare_methods", "get_verdict_message", "record_signed_verdict",
    "research_trends", "validate_project",
}
WEB3_STABLE_EXPECTED_IDS = [
    "iacr:2020/1206", "1902.05164", "2508.14284", "2301.13785",
    "wp:flashbots-mev", "oa:W4414495031", "2504.03936", "2409.06213",
    "oa:W7166513198", "oa:W7214212801",
]
MEMORY_QUERY = (
    "saving computer memory when processing very long texts by throwing away "
    "unimportant previous parts"
)
SNAP_TITLE_PATTERN = re.compile(r"\bSnapKV\b", re.IGNORECASE)
SNAP_LIMITATION_PHRASES = ("cannot extend a model", "prompt inference")


def _decode_response(response):
    raw = response.text
    if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
        return response.json() if raw else None
    for event in raw.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                           if line.startswith("data:"))
        if data:
            try:
                return json.loads(data)
            except ValueError:
                continue
    return None


def _tool_payload(result):
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text", "")
        if not isinstance(text, str):
            continue
        decoder = json.JSONDecoder()
        offset = text.find("{")
        while offset >= 0:
            try:
                value, _ = decoder.raw_decode(text, offset)
            except ValueError:
                offset = text.find("{", offset + 1)
                continue
            if isinstance(value, dict):
                return value
            offset = text.find("{", offset + 1)
    return None


def _section_text_hash(payload):
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, list) or not sections:
        return None, 0
    first = sections[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None, 0
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text)


def _error_detail(payload):
    if not isinstance(payload, dict):
        return {}
    for key in ("detail", "error"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def run_probe(endpoint, snap_paper_id, post=requests.post):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be HTTPS without query or fragment")
    snap_paper_id = (snap_paper_id or "").strip()
    if not snap_paper_id:
        raise ValueError("snap_paper_id is required to keep the exact-section check within the call cap")

    report = {
        "report_version": PROBE_REPORT_VERSION,
        "status": "fail", "calls_attempted": 0, "calls_completed": 0,
        "http_call_cap": MAX_HTTP_CALLS, "steps": [], "errors": [],
        "error_count": 0, "inventory_count": None, "inventory_matches_13": False,
        "eip4337_first_code_text_hash_equal": False, "eip4337_code_chars": None,
        "eip4337_partial_allowed": True, "conflict_is_400": False,
        "missing_section_404_has_outline": False, "snap_discussions_title_matches": False,
        "snap_discussions_limitation_phrases_match": False,
        "memory_query_primary_count": None, "memory_query_unverified_leads": None,
        "web3_top10_matches_frozen_ids": False,
    }
    session_id = None
    fatal = False

    def send(label, payload, notification=False):
        nonlocal session_id, fatal
        if report["calls_attempted"] >= MAX_HTTP_CALLS:
            report["errors"].append({"step": label, "kind": "call_cap"})
            fatal = True
            return None
        report["calls_attempted"] += 1
        started = time.monotonic()
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        try:
            response = post(endpoint, json=payload, headers=headers,
                            timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            session_id = response.headers.get("Mcp-Session-Id") or session_id
            report["calls_completed"] += 1
            step = {"name": label, "http_status": response.status_code,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1)}
            report["steps"].append(step)
            if notification:
                if response.status_code not in (200, 202, 204):
                    report["errors"].append({"step": label, "kind": "http_status"})
                    fatal = True
                return response
            if not 200 <= response.status_code < 300:
                report["errors"].append({"step": label, "kind": "http_status"})
                fatal = True
                return None
            return _decode_response(response)
        except (requests.RequestException, ValueError):
            report["steps"].append({"name": label, "latency_ms": round(
                (time.monotonic() - started) * 1000, 1)})
            report["errors"].append({"step": label, "kind": "transport_or_decode"})
            fatal = True
            return None

    def call_tool(name, request_id, arguments, label=None):
        step_name = label or name
        reply = send(step_name, {"jsonrpc": "2.0", "id": request_id,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
        if not isinstance(reply, dict) or reply.get("id") != request_id:
            report["errors"].append({"step": step_name, "kind": "invalid_json_rpc"})
            fatal = True
            return None
        return reply.get("result")

    initialized = send("initialize", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "dtox-audit-fix-probe", "version": "1"}},
    })
    if not (isinstance(initialized, dict) and initialized.get("id") == 1
            and isinstance(initialized.get("result"), dict)):
        report["errors"].append({"step": "initialize", "kind": "invalid_json_rpc"})
        fatal = True

    if not fatal:
        send("notifications/initialized", {
            "jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)
    if not fatal:
        listed = send("tools/list", {"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}})
        result = listed.get("result") if isinstance(listed, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        names = {tool.get("name") for tool in tools or [] if isinstance(tool, dict)}
        report["inventory_count"] = len(names)
        report["inventory_matches_13"] = names == EXPECTED_TOOLS
        if not isinstance(listed, dict) or listed.get("id") != 2 or not report["inventory_matches_13"]:
            report["errors"].append({"step": "tools/list", "kind": "inventory_mismatch"})
            if not isinstance(listed, dict) or listed.get("id") != 2:
                fatal = True

    specs = []
    if not fatal:
        for request_id, args in (
            (3, {"paper_id": "eip:4337", "target_elements": "code",
                 "include_prose": False, "max_chars": 1500}),
            (4, {"arxiv_id": "eip:4337", "target_elements": "code",
                 "include_prose": False, "max_chars": 1500}),
        ):
            result = call_tool("get_code_or_math_spec", request_id, args)
            if fatal:
                break
            payload = _tool_payload(result)
            digest, chars = _section_text_hash(payload)
            if not (isinstance(result, dict) and result.get("isError") is False
                    and isinstance(payload, dict) and not payload.get("error")
                    and payload.get("arxiv_id") == "eip:4337" and digest and chars <= 1500):
                report["errors"].append({"step": "get_code_or_math_spec",
                                         "kind": "missing_first_code"})
            specs.append((digest, chars, payload))
        if len(specs) == 2 and specs[0][0] and specs[1][0]:
            report["eip4337_first_code_text_hash_equal"] = specs[0][0] == specs[1][0]
            report["eip4337_code_chars"] = max(specs[0][1], specs[1][1])
            if not report["eip4337_first_code_text_hash_equal"]:
                report["errors"].append({"step": "eip4337_aliases", "kind": "text_hash_mismatch"})

    if not fatal:
        conflict = call_tool("get_code_or_math_spec", 5, {
            "paper_id": "eip:4337", "arxiv_id": "eip:1559", "max_chars": 1500})
        conflict_payload = _tool_payload(conflict)
        report["conflict_is_400"] = (
            isinstance(conflict, dict) and conflict.get("isError") is True
            and isinstance(conflict_payload, dict) and conflict_payload.get("status") == 400
            and conflict_payload.get("error_type") == "validation")
        if not report["conflict_is_400"]:
            report["errors"].append({"step": "alias_conflict", "kind": "expected_validation_400"})

    if not fatal:
        missing = call_tool("read_paper_section", 6, {
            "paper_id": "eip:4337", "section_title": "__audit_probe_missing_section__",
            "max_chars": 1500})
        missing_payload = _tool_payload(missing)
        detail = _error_detail(missing_payload)
        report["missing_section_404_has_outline"] = (
            isinstance(missing, dict) and missing.get("isError") is True
            and isinstance(missing_payload, dict) and missing_payload.get("status") == 404
            and isinstance(detail.get("available_sections"), list)
            and bool(detail["available_sections"]))
        if not report["missing_section_404_has_outline"]:
            report["errors"].append({"step": "missing_section", "kind": "expected_404_outline"})

    if not fatal:
        snap = call_tool("read_paper_section", 7, {
            "paper_id": snap_paper_id, "section_title": "Discussions", "max_chars": 1500})
        snap_payload = _tool_payload(snap)
        title = snap_payload.get("title", "") if isinstance(snap_payload, dict) else ""
        text = snap_payload.get("text", "") if isinstance(snap_payload, dict) else ""
        report["snap_discussions_title_matches"] = (
            isinstance(snap, dict) and snap.get("isError") is False
            and isinstance(title, str) and bool(SNAP_TITLE_PATTERN.search(title))
            and isinstance(snap_payload, dict)
            and snap_payload.get("section_title") == "Discussions")
        report["snap_discussions_limitation_phrases_match"] = (
            isinstance(text, str) and all(phrase in text.casefold()
                                          for phrase in SNAP_LIMITATION_PHRASES))
        if (not report["snap_discussions_title_matches"]
                or not report["snap_discussions_limitation_phrases_match"]):
            report["errors"].append({"step": "snap_discussions", "kind": "title_or_limitation_missing"})

    if not fatal:
        memory = call_tool("search_research_paper", 8, {
            "query": MEMORY_QUERY, "layer": "llm-slm", "limit": 10, "strict": False},
            label="memory_query")
        payload = _tool_payload(memory)
        results = payload.get("results") if isinstance(payload, dict) else None
        candidates = payload.get("source_candidates") if isinstance(payload, dict) else None
        report["memory_query_primary_count"] = payload.get("count") if isinstance(payload, dict) else None
        report["memory_query_unverified_leads"] = len(candidates) if isinstance(candidates, list) else 0
        valid_leads = (isinstance(candidates, list) and 1 <= len(candidates) <= 3
                       and all(isinstance(row, dict)
                               and row.get("retrieval_role") == "source_candidate"
                               and row.get("verification_required") is True
                               for row in candidates))
        if not (isinstance(memory, dict) and memory.get("isError") is False
                and isinstance(payload, dict) and isinstance(results, list)
                and payload.get("count") == 0 and not results and valid_leads
                and payload.get("partial") is not True):
            report["errors"].append({"step": "memory_query", "kind": "expected_empty_primary_leads"})

    if not fatal:
        web3 = call_tool("search_research_paper", 9, {
            "query": "commit-reveal scheme for frontrunning and MEV protection in decentralized exchanges",
            "layer": "web3", "limit": 10, "strict": False}, label="web3_top10")
        payload = _tool_payload(web3)
        results = payload.get("results") if isinstance(payload, dict) else None
        ids = [row.get("arxiv_id") for row in results or [] if isinstance(row, dict)]
        report["web3_top10_matches_frozen_ids"] = (
            isinstance(web3, dict) and web3.get("isError") is False
            and isinstance(results, list) and payload.get("partial") is not True
            and ids == WEB3_STABLE_EXPECTED_IDS)
        if not report["web3_top10_matches_frozen_ids"]:
            report["errors"].append({"step": "web3_top10", "kind": "frozen_order_mismatch"})

    report["error_count"] = len(report["errors"])
    if report["calls_attempted"] > MAX_HTTP_CALLS:
        report["errors"].append({"step": "probe", "kind": "call_cap_exceeded"})
        report["error_count"] = len(report["errors"])
    if not report["errors"]:
        report["status"] = "pass"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://read.whoim.space/mcp")
    parser.add_argument("--snap-paper-id", required=True,
                        help="Known paper ID whose exact Discussions section is to be read")
    args = parser.parse_args()
    try:
        report = run_probe(args.endpoint, args.snap_paper_id)
    except ValueError as error:
        report = {"status": "fail", "calls_attempted": 0, "calls_completed": 0,
                  "http_call_cap": MAX_HTTP_CALLS, "error_count": 1,
                  "errors": [{"step": "configuration", "kind": str(error)}], "steps": []}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
