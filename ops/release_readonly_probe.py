"""Bounded, read-only public MCP readiness probe; emits one sanitized JSON report."""

import argparse
import json
import time
from urllib.parse import urlparse

import requests


EXPECTED_TOOLS = {
    "search_research_paper", "get_research_bundle", "find_papers", "get_paper",
    "read_paper_section", "count_papers", "similar_papers", "get_code_or_math_spec",
    "compare_methods", "get_verdict_message", "record_signed_verdict",
    "research_trends", "validate_project",
}
MAX_HTTP_CALLS = 6
REQUEST_TIMEOUT = (3, 9)
MISSING_PAPER_ID = "dtox-readiness-gate-missing-id-20261005"


def _decode_response(response):
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/event-stream" not in content_type:
        raw = response.text
        return response.json() if raw else None
    raw = response.content.decode("utf-8")
    for event in raw.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                           if line.startswith("data:"))
        if data:
            try:
                return json.loads(data)
            except ValueError:
                continue
    return None


def _flags(value):
    """Find explicit partial/truncation markers without retaining response content."""
    partial = truncated = False
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            partial |= current.get("partial") is True or current.get("search_partial") is True
            truncated |= current.get("truncated") is True or current.get("is_truncated") is True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and current.startswith(("{", "[")):
            try:
                stack.append(json.loads(current))
            except ValueError:
                pass
    return partial, truncated


def _tool_payload(result):
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if not isinstance(text, str):
                continue
            decoder = json.JSONDecoder()
            start = text.find("{")
            while start >= 0:
                try:
                    payload, _ = decoder.raw_decode(text, start)
                except ValueError:
                    start = text.find("{", start + 1)
                    continue
                if isinstance(payload, dict):
                    return payload
                start = text.find("{", start + 1)
    return None


def _is_expected_missing_error(result):
    if not isinstance(result, dict) or result.get("isError") is not True:
        return False
    payload = _tool_payload(result)
    return isinstance(payload, dict) and payload.get("status") == 404


def run_probe(endpoint, post=requests.post):
    """Run initialize, initialized, tools/list and one safe expected-404 read."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be an HTTPS URL without query or fragment")

    started = time.monotonic()
    report = {
        "status": "fail", "calls_attempted": 0, "calls_completed": 0,
        "http_call_cap": MAX_HTTP_CALLS, "errors": [], "error_count": 0,
        "tool_error_count": 0, "partial": False,
        "truncated": False, "latency_ms": None, "steps": [],
        "inventory_count": None, "inventory_matches_13": False,
        "missing_id_is_error": False, "find_papers_ok": False,
        "get_code_or_math_spec_ok": False,
    }
    session_id = None

    def send(label, payload, notification=False):
        nonlocal session_id
        if report["calls_attempted"] >= MAX_HTTP_CALLS:
            report["errors"].append({"step": label, "kind": "call_cap"})
            return None
        report["calls_attempted"] += 1
        step_started = time.monotonic()
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        try:
            response = post(endpoint, json=payload, headers=headers,
                            timeout=REQUEST_TIMEOUT, allow_redirects=False)
            session_id = response.headers.get("Mcp-Session-Id") or session_id
            report["calls_completed"] += 1
            item = {"name": label, "http_status": response.status_code,
                    "latency_ms": round((time.monotonic() - step_started) * 1000, 1)}
            report["steps"].append(item)
            if notification:
                if response.status_code not in (200, 202, 204):
                    report["errors"].append({"step": label, "kind": "http_status"})
                return response
            if not 200 <= response.status_code < 300:
                report["errors"].append({"step": label, "kind": "http_status"})
                return None
            return _decode_response(response)
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            report["steps"].append({"name": label, "latency_ms": round(
                (time.monotonic() - step_started) * 1000, 1)})
            report["errors"].append({"step": label, "kind": "transport_or_decode"})
            return None

    initialized = send("initialize", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "dtox-readiness-probe", "version": "1"}},
    })
    if initialized and initialized.get("id") == 1 and isinstance(initialized.get("result"), dict):
        report["partial"], report["truncated"] = _flags(initialized)
    elif not report["errors"]:
        report["errors"].append({"step": "initialize", "kind": "invalid_json_rpc"})

    if not report["errors"]:
        send("notifications/initialized", {
            "jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)

    if not report["errors"]:
        listed = send("tools/list", {"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}})
        if isinstance(listed, dict):
            result = listed.get("result")
            tools = result.get("tools") if isinstance(result, dict) else None
            names = {tool.get("name") for tool in tools or [] if isinstance(tool, dict)}
            report["inventory_count"] = len(names)
            report["inventory_matches_13"] = len(names) == 13 and names == EXPECTED_TOOLS
            partial, truncated = _flags(listed)
            report["partial"] |= partial
            report["truncated"] |= truncated
            if listed.get("id") != 2 or not report["inventory_matches_13"]:
                report["errors"].append({"step": "tools/list", "kind": "inventory_mismatch"})
        elif not report["errors"]:
            report["errors"].append({"step": "tools/list", "kind": "inventory_mismatch"})

    if not report["errors"]:
        called = send("get_paper_missing_id", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_paper", "arguments": {"paper_id": MISSING_PAPER_ID}},
        })
        result = called.get("result") if isinstance(called, dict) else None
        report["missing_id_is_error"] = _is_expected_missing_error(result)
        report["tool_error_count"] = int(
            isinstance(result, dict) and result.get("isError") is True)
        partial, truncated = _flags(called)
        report["partial"] |= partial
        report["truncated"] |= truncated
        if isinstance(called, dict) and (called.get("id") != 3 or not report["missing_id_is_error"]):
            report["errors"].append({"step": "get_paper_missing_id", "kind": "expected_is_error_missing"})

        positive_calls = (
            ("find_papers", 4, {"query": "durable nonce", "layer": "web3", "limit": 2}),
            ("get_code_or_math_spec", 5, {"arxiv_id": "2101.05511",
                                           "target_elements": "equation,algorithm",
                                           "include_prose": False, "max_chars": 2500}),
        )
        for name, request_id, arguments in positive_calls:
            response = send(name, {"jsonrpc": "2.0", "id": request_id,
                                   "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}})
            tool_result = response.get("result") if isinstance(response, dict) else None
            tool_error = isinstance(tool_result, dict) and tool_result.get("isError") is True
            report["tool_error_count"] += int(tool_error)
            payload = _tool_payload(tool_result)
            partial, truncated = _flags(tool_result)
            report["partial"] |= partial
            report["truncated"] |= truncated
            if name == "find_papers":
                papers = payload.get("papers") if isinstance(payload, dict) else None
                valid_paper = (isinstance(papers, list) and bool(papers) and
                               isinstance(papers[0], dict) and
                               all(isinstance(papers[0].get(field), str) and papers[0][field].strip()
                                   for field in ("id", "title", "source", "url")))
                ok = (isinstance(tool_result, dict) and tool_result.get("isError") is False
                      and isinstance(payload, dict) and not payload.get("error") and valid_paper)
                report["find_papers_ok"] = bool(ok)
            else:
                sections = payload.get("sections") if isinstance(payload, dict) else None
                valid_spec = (isinstance(sections, list) and bool(sections) and
                              all(isinstance(item, dict) and isinstance(item.get("text"), str)
                                  and item["text"].strip() for item in sections))
                ok = (isinstance(tool_result, dict) and tool_result.get("isError") is False
                      and isinstance(payload, dict) and not payload.get("error")
                      and isinstance(payload.get("arxiv_id"), str)
                      and bool(payload["arxiv_id"].strip())
                      and isinstance(payload.get("title"), str)
                      and bool(payload["title"].strip()) and valid_spec)
                report["get_code_or_math_spec_ok"] = bool(ok)
            if isinstance(response, dict) and (response.get("id") != request_id or not ok):
                report["errors"].append({"step": name, "kind": "positive_read_invalid"})

    report["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    report["error_count"] = len(report["errors"])
    if not report["errors"] and report["calls_attempted"] <= MAX_HTTP_CALLS:
        report["status"] = "pass"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://read.whoim.space/mcp")
    args = parser.parse_args()
    try:
        report = run_probe(args.endpoint)
    except ValueError as error:
        report = {"status": "fail", "errors": [{"step": "configuration", "kind": str(error)}],
                  "calls_attempted": 0, "calls_completed": 0, "http_call_cap": MAX_HTTP_CALLS,
                  "error_count": 1, "tool_error_count": 0,
                  "partial": False, "truncated": False, "latency_ms": 0, "steps": []}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
