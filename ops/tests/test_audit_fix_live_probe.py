"""Offline unit stubs for the candidate audit probe; no network is used."""

import json
import unittest

from ops.audit_fix_live_probe import (
    EXPECTED_TOOLS, MAX_HTTP_CALLS, WEB3_STABLE_EXPECTED_IDS, run_probe,
)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, *, conflict_status=400, aliases_equal=True):
        self.calls = []
        self.conflict_status = conflict_status
        self.aliases_equal = aliases_equal

    def __call__(self, url, *, json, headers, timeout, allow_redirects):
        self.calls.append((json, headers, timeout, allow_redirects))
        index = len(self.calls)
        if index == 1:
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
            return FakeResponse(200, payload, {"Mcp-Session-Id": "session"})
        if index == 2:
            return FakeResponse(202, headers={"Mcp-Session-Id": "session"})
        if index == 3:
            return FakeResponse(200, {"jsonrpc": "2.0", "id": 2, "result": {
                "tools": [{"name": name} for name in sorted(EXPECTED_TOOLS)]}})
        if index in (4, 5):
            suffix = "same" if self.aliases_equal or index == 4 else "different"
            spec = {"arxiv_id": "eip:4337", "title": "ERC-4337", "sections": [
                {"section_title": "Code", "element_type": "code",
                 "text": "bounded code " + suffix}],
                "truncated": True}
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
                "isError": False, "structuredContent": spec}})
        if index == 6:
            conflict = {"status": self.conflict_status, "error_type": "validation",
                        "error": "paper_id conflicts with arxiv_id"}
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": json_module_dumps(conflict)}]}})
        if index == 7:
            missing = {"status": 404, "detail": {"available_sections": [
                {"section_title": "Security Considerations"}]}}
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": json_module_dumps(missing)}]}})
        if index == 8:
            snap = {"id": "snap:test", "title": "SnapKV: test paper",
                    "section_title": "Discussions",
                    "text": "This method cannot extend a model for prompt inference."}
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
                "isError": False, "structuredContent": snap}})
        if index == 9:
            memory = {"count": 0, "results": [], "partial": False,
                      "source_candidates": [{"retrieval_role": "source_candidate",
                                             "verification_required": True}]}
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
                "isError": False, "structuredContent": memory}})
        ids = WEB3_STABLE_EXPECTED_IDS
        web3 = {"count": 10, "partial": False,
                "results": [{"arxiv_id": paper_id} for paper_id in ids]}
        return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": {
            "isError": False, "structuredContent": web3}})


def json_module_dumps(value):
    return json.dumps(value)


class AuditFixLiveProbeTests(unittest.TestCase):
    def test_offline_happy_path_obeys_cap_and_read_only_calls(self):
        transport = FakeTransport()
        report = run_probe("https://mcp.example/mcp", "snap:test", post=transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["report_version"], 2)
        self.assertEqual(report["calls_attempted"], MAX_HTTP_CALLS)
        self.assertTrue(report["eip4337_first_code_text_hash_equal"])
        self.assertTrue(all(call[0]["params"]["arguments"]["target_elements"] == "code"
                            and call[0]["params"]["arguments"]["max_chars"] == 1500
                            for call in transport.calls[3:5]))
        self.assertTrue(report["conflict_is_400"])
        self.assertTrue(report["missing_section_404_has_outline"])
        self.assertTrue(report["snap_discussions_title_matches"])
        self.assertTrue(report["snap_discussions_limitation_phrases_match"])
        self.assertEqual(report["memory_query_primary_count"], 0)
        self.assertTrue(report["web3_top10_matches_frozen_ids"])
        self.assertTrue(all(call[2] == 20 and call[3] is False for call in transport.calls))
        self.assertTrue(all(call[0]["method"] != "tools/call" or
                            call[0]["params"]["name"] not in {
                                "record_signed_verdict", "get_verdict_message"}
                            for call in transport.calls))

    def test_conflict_status_and_hash_mismatch_fail_closed(self):
        conflict = run_probe("https://mcp.example/mcp", "snap:test",
                              post=FakeTransport(conflict_status=None))
        self.assertEqual(conflict["errors"][0]["kind"], "expected_validation_400")
        mismatch = run_probe("https://mcp.example/mcp", "snap:test",
                             post=FakeTransport(aliases_equal=False))
        self.assertIn("text_hash_mismatch", [item["kind"] for item in mismatch["errors"]])
        self.assertEqual(mismatch["calls_attempted"], MAX_HTTP_CALLS)
        self.assertIn("memory_query", [step["name"] for step in mismatch["steps"]])
        self.assertIn("web3_top10", [step["name"] for step in mismatch["steps"]])

    def test_snap_exact_phrase_mismatch_does_not_skip_later_search_checks(self):
        class WrongSnap(FakeTransport):
            def __call__(self, url, *, json, headers, timeout, allow_redirects):
                response = super().__call__(url, json=json, headers=headers,
                                            timeout=timeout, allow_redirects=allow_redirects)
                if len(self.calls) == 8:
                    payload = response.json()
                    payload["result"]["structuredContent"]["text"] = "Generic limitations only."
                    return FakeResponse(response.status_code, payload, response.headers)
                return response

        transport = WrongSnap()
        report = run_probe("https://mcp.example/mcp", "snap:test", post=transport)
        self.assertFalse(report["snap_discussions_limitation_phrases_match"])
        self.assertEqual(report["calls_attempted"], MAX_HTTP_CALLS)
        self.assertIn("memory_query", [step["name"] for step in report["steps"]])
        self.assertIn("web3_top10", [step["name"] for step in report["steps"]])

    def test_configuration_rejects_missing_snap_id_before_http(self):
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            run_probe("https://mcp.example/mcp", "", post=transport)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
