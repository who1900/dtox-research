"""Offline tests for the bounded public MCP readiness probe."""

import json
import unittest

import requests
from requests import Timeout

from ops.release_readonly_probe import EXPECTED_TOOLS, MAX_HTTP_CALLS, _decode_response, run_probe


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, body=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._payload = payload
        self.text = body if body is not None else json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, *, inventory=None, error=True, fail_at=None, raise_at=None,
                 markers=False, missing_payload=None, bad_positive=False):
        self.calls = []
        self.inventory = inventory if inventory is not None else EXPECTED_TOOLS
        self.error = error
        self.fail_at = fail_at
        self.raise_at = raise_at
        self.markers = markers
        self.missing_payload = missing_payload or {"status": 404, "error": "not found"}
        self.bad_positive = bad_positive

    def __call__(self, url, *, json, headers, timeout, allow_redirects):
        self.calls.append((json, headers, timeout, allow_redirects))
        index = len(self.calls)
        if index == self.raise_at:
            raise Timeout("transport detail must not be reported")
        if index == self.fail_at:
            return FakeResponse(503, {"detail": "do not persist body"})
        if index == 1:
            result = {"protocolVersion": "2025-03-26"}
            if self.markers:
                result.update({"partial": True, "truncated": True})
            return FakeResponse(200, {"jsonrpc": "2.0", "id": 1,
                                      "result": result},
                                {"Mcp-Session-Id": "fake-session"})
        if index == 2:
            return FakeResponse(202, None, {"Mcp-Session-Id": "fake-session"})
        if index == 3:
            tools = [{"name": name} for name in sorted(self.inventory)]
            event = "event: message\ndata: " + json_module_dumps(
                {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}) + "\n\n"
            return FakeResponse(200, headers={"Content-Type": "text/event-stream"}, body=event)
        if index == 4:
            missing_result = {"isError": self.error}
            if self.missing_payload.get("status") == 500:
                missing_result["structuredContent"] = self.missing_payload
            else:
                missing_result["content"] = [{"type": "text", "text":
                                               "Error executing tool get_paper: " +
                                               json_module_dumps(self.missing_payload)}]
            return FakeResponse(200, {"jsonrpc": "2.0", "id": 3, "result": missing_result})
        if index == 5:
            paper = {"id": "eip:4337", "title": "Account Abstraction",
                     "source": "eip", "url": "https://example.test/eip"}
            if self.bad_positive:
                paper.pop("url")
            return FakeResponse(200, {"jsonrpc": "2.0", "id": 4,
                                      "result": {"isError": False, "structuredContent": {
                                          "papers": [paper]}}})
        sections = [] if self.bad_positive else [{"text": "x = y"}]
        return FakeResponse(200, {"jsonrpc": "2.0", "id": 5,
                                  "result": {"isError": False, "structuredContent": {
                                      "arxiv_id": "2101.05511", "title": "Fixture Paper",
                                      "sections": sections, "truncated": False}}})


def json_module_dumps(value):
    return json.dumps(value)


class ReleaseReadonlyProbeTests(unittest.TestCase):
    def test_event_stream_decodes_utf8_bytes_not_requests_text_encoding(self):
        payload = {"jsonrpc": "2.0", "id": 7, "result": {
            "text": "модель Δ; формула λ=β; source hash sha256:abc123"}}
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/event-stream"
        response.encoding = "ISO-8859-1"
        response._content = ("event: message\ndata: " +
                             json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

        self.assertEqual(_decode_response(response), payload)

    def test_event_stream_rejects_invalid_utf8_and_json_mode_is_unchanged(self):
        malformed = requests.Response()
        malformed.status_code = 200
        malformed.headers["Content-Type"] = "text/event-stream"
        malformed._content = b"data: \xff\n\n"
        with self.assertRaises(UnicodeDecodeError):
            _decode_response(malformed)

        ordinary = FakeResponse(200, {"jsonrpc": "2.0", "id": 8, "result": {"ok": True}})
        self.assertEqual(_decode_response(ordinary), ordinary._payload)

    def test_happy_path_is_capped_read_only_and_does_not_expose_session_header(self):
        transport = FakeTransport()

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["calls_attempted"], 6)
        self.assertEqual(report["calls_completed"], 6)
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["tool_error_count"], 1)
        self.assertEqual(report["http_call_cap"], MAX_HTTP_CALLS)
        self.assertEqual(report["inventory_count"], 13)
        self.assertTrue(report["inventory_matches_13"])
        self.assertTrue(report["missing_id_is_error"])
        self.assertTrue(report["find_papers_ok"])
        self.assertTrue(report["get_code_or_math_spec_ok"])
        self.assertFalse(report["errors"])
        self.assertNotIn("fake-session", json.dumps(report))
        self.assertEqual(transport.calls[3][0]["params"]["name"], "get_paper")
        self.assertEqual(transport.calls[3][0]["params"]["arguments"]["paper_id"],
                         "dtox-readiness-gate-missing-id-20261005")
        self.assertIn(("Mcp-Session-Id", "fake-session"),
                      tuple(sorted(transport.calls[-1][1].items())))
        self.assertEqual(transport.calls[-1][3], False)
        called_tools = [call[0]["params"]["name"] for call in transport.calls
                        if call[0].get("method") == "tools/call"]
        self.assertEqual(called_tools, ["get_paper", "find_papers", "get_code_or_math_spec"])

    def test_inventory_mismatch_fails_closed_without_tool_calls(self):
        transport = FakeTransport(inventory=EXPECTED_TOOLS - {"count_papers"})

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["inventory_count"], 12)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(report["errors"][0]["step"], "tools/list")

    def test_missing_id_success_is_not_accepted(self):
        transport = FakeTransport(error=False)

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["missing_id_is_error"])
        self.assertEqual(report["errors"][0]["kind"], "expected_is_error_missing")
        self.assertEqual(report["tool_error_count"], 0)

    def test_tool_500_is_error_but_not_the_expected_missing_id_404(self):
        transport = FakeTransport(missing_payload={"status": 500, "error": "internal error"})

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertFalse(report["missing_id_is_error"])
        self.assertEqual(report["errors"][0]["kind"], "expected_is_error_missing")
        self.assertEqual(report["tool_error_count"], 1)
        self.assertFalse(report["status"] == "pass")

    def test_tool_timeout_error_is_not_the_expected_missing_id_404(self):
        transport = FakeTransport(missing_payload={"error": "timeout", "error_type": "transport"})

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertFalse(report["missing_id_is_error"])
        self.assertFalse(report["status"] == "pass")

    def test_positive_reads_require_nonempty_payload_and_provenance(self):
        transport = FakeTransport(bad_positive=True)

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertFalse(report["find_papers_ok"])
        self.assertFalse(report["get_code_or_math_spec_ok"])
        self.assertEqual(report["error_count"], 2)

    def test_http_failure_is_included_and_stops_following_calls(self):
        transport = FakeTransport(fail_at=1)

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["calls_attempted"], 1)
        self.assertEqual(report["calls_completed"], 1)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["errors"][0]["kind"], "http_status")
        self.assertNotIn("do not persist body", json.dumps(report))

    def test_explicit_partial_and_truncation_markers_are_distinct(self):
        transport = FakeTransport(markers=True)

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertTrue(report["partial"])
        self.assertTrue(report["truncated"])

    def test_transport_exception_counts_as_attempt_and_is_sanitized(self):
        transport = FakeTransport(raise_at=1)

        report = run_probe("https://read.example/mcp", post=transport)

        self.assertEqual(report["calls_attempted"], 1)
        self.assertEqual(report["calls_completed"], 0)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["errors"][0]["kind"], "transport_or_decode")
        self.assertNotIn("transport detail", json.dumps(report))

    def test_rejects_non_https_without_network(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_probe("http://read.example/mcp", post=transport)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
