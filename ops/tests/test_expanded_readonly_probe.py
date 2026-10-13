"""Offline transport and contract tests for expanded_readonly_probe."""

import json
import unittest

from requests import Timeout

from ops.expanded_readonly_probe import EXPECTED_TOOLS, MAX_HTTP_REQUESTS, run_probe


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, body=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.text = body if body is not None else json.dumps(payload) if payload is not None else ""
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, *, inventory=None, empty=False, fail_tool=None,
                 raise_at=None, marker=None, malformed_tool=None):
        self.calls = []
        self.inventory = inventory if inventory is not None else EXPECTED_TOOLS
        self.empty = empty
        self.fail_tool = fail_tool
        self.raise_at = raise_at
        self.marker = marker
        self.malformed_tool = malformed_tool

    def __call__(self, url, *, json, headers, timeout, allow_redirects):
        self.calls.append((json, headers, timeout, allow_redirects))
        index = len(self.calls)
        if index == self.raise_at:
            raise Timeout("do not expose transport detail")
        if index == 1:
            payload = {"jsonrpc": "2.0", "id": json["id"],
                       "result": {"protocolVersion": "2025-03-26"}}
        elif index == 2:
            return FakeResponse(202, None, {"Mcp-Session-Id": "private-session-id"})
        elif index == 3:
            payload = {"jsonrpc": "2.0", "id": json["id"],
                       "result": {"tools": [{"name": name} for name in self.inventory]}}
        else:
            params = json["params"]
            name = params["name"]
            args = params["arguments"]
            if name == self.fail_tool:
                result = {"isError": True, "content": [{"type": "text",
                    "text": "secret error body must not be serialized"}]}
            else:
                data = {"title": "malformed but not leaked"} if name == self.malformed_tool else self._result(name, args)
                result = {"isError": False, "structuredContent": data}
                if self.marker and name == self.marker[0]:
                    result["structuredContent"][self.marker[1]] = True
            payload = {"jsonrpc": "2.0", "id": json["id"], "result": result}
        return FakeResponse(payload=payload, headers={"Mcp-Session-Id": "private-session-id"})

    def _result(self, name, args):
        if name == "find_papers":
            papers = [] if self.empty else [{"id": "agent:42", "title": "Agent retrieval",
                "layers": ["ai-agents"], "source": "fixture", "url": "https://example.test/p/42",
                "fulltext": None}, {"id": "agent:43", "title": "Another agent paper",
                "source_url_origin": "stored", "source_url_status": "valid", "fulltext": "abstract-only"}]
            return {"papers": papers, "scope": {"in_scope": True}}
        if name == "search_research_paper":
            return {"results": [{"arxiv_id": "hit:1", "title": "Grounded retrieval",
                "url": "https://example.test/hit", "source_url_origin": "stored",
                "source_url_status": "valid", "provenance_missing": False,
                "fulltext": None}], "count": 1, "scope": {"in_scope": True, "confidence": "medium"}}
        if name == "count_papers":
            return {"total": 8, "by_year": [{"year": 2024, "count": 8}], "sampled": False,
                    "counted_over": 8, "scope": {"in_scope": True, "confidence": "high"},
                    "top_papers": [{"abstract": "must not leak"}]}
        if name == "get_paper":
            return {"id": args["paper_id"], "title": "Agent retrieval", "url": "https://example.test/p/42",
                    "outline": [{"section_title": "Method", "section_type": "method"}]}
        if name == "read_paper_section":
            return {"text": "bounded text", "total_chars": 11, "url": "https://example.test/p/42"}
        if name == "get_code_or_math_spec":
            return {"arxiv_id": args["arxiv_id"], "sections": [], "returned": 0,
                    "available": 0, "truncated": True}
        if name == "similar_papers":
            return {"papers": [{"id": "dynamic:99", "title": "Similar work",
                "url": "https://example.test/p/99", "fulltext": "abstract-only"}]}
        if name == "get_research_bundle":
            return {"papers": [{"id": "llm:51", "title": "LLM one", "source_url_origin": "acquired",
                "source_url_status": "valid", "provenance_missing": False, "fulltext": None},
                {"id": "llm:52", "title": "LLM two", "source_url_origin": "stored",
                 "source_url_status": "valid", "provenance_missing": False, "fulltext": "abstract-only"}],
                "missing": {"papers": False}, "budgets": {"paper_limit": 2}}
        if name == "compare_methods":
            return {"a": {"available": True}, "b": {"available": True}}
        if name == "research_trends":
            return {"years": [2023, 2024], "papers_per_year": [2, 3], "trends": []}
        return {}


class PayloadTransport(FakeTransport):
    def __init__(self, tool_name, payload):
        super().__init__()
        self.tool_name, self.payload = tool_name, payload

    def _result(self, name, args):
        return self.payload if name == self.tool_name else super()._result(name, args)


class ExpandedProbeQualityTests(unittest.TestCase):
    def probe(self, payload, name="get_research_bundle"):
        return run_probe("https://read.example/mcp", post=PayloadTransport(name, payload))

    def test_budget_limited_partial_bundle_passes_correctness_not_completeness(self):
        payload = {"papers": [{"id": "generic:paper", "title": "Paper", "partial": True,
                    "partial_reasons": ["character_budget"]}],
                   "partial": True, "partial_reasons": ["character_budget"],
                   "equations": [{"text": "x" * 1000, "clipped": True}],
                   "budgets": {"max_chars": 1000, "chars_returned": 1000}}
        report = self.probe(payload)
        self.assertTrue(report["partial"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["correctness"], "pass")
        self.assertEqual(report["completeness"]["status"], "bounded")
        step = next(step for step in report["steps"] if step["name"] == "get_research_bundle")
        self.assertEqual(step["completeness"]["reasons"], ["character_budget"])
        self.assertNotIn("x" * 1000, json.dumps(report))

    def test_legacy_budget_clipped_bundle_is_accepted_without_new_fields(self):
        report = self.probe({"papers": [{"id": "generic:paper", "title": "Paper", "partial": True,
                            "missing": {"algorithms": "not_found_in_retrieved_chunks"}}],
                            "partial": True, "equations": [{"text": "α", "clipped": True}]})
        self.assertEqual(report["status"], "pass")
        self.assertIn("not_found_in_retrieved_chunks", report["completeness"]["reasons"])

    def test_unavailable_wanted_type_is_not_failure_or_source_absence_proof(self):
        spec = {"sections": [], "available": 0, "returned": 0, "truncated": False,
                "source_resolution": {"partial": True, "errors": [], "time_budget_exhausted": False,
                                      "partial_reasons": ["not_found_in_retrieved_chunks"]}}
        report = self.probe(spec, "get_code_or_math_spec")
        self.assertEqual(report["status"], "pass")
        step = next(step for step in report["steps"] if step["name"] == "get_code_or_math_spec")
        self.assertEqual(step["completeness"], {"status": "unavailable", "reasons": ["not_found_in_retrieved_chunks"]})

    def test_legacy_spec_resolution_partial_without_error_stays_observed(self):
        report = self.probe({"sections": [], "available": 0, "returned": 0,
                            "source_resolution": {"partial": True, "errors": [], "time_budget_exhausted": False}},
                            "get_code_or_math_spec")
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["partial"])
        self.assertIn("source_resolution_incomplete", report["completeness"]["reasons"])

    def test_char_clipping_never_masks_retrieval_failure_or_time_exhaustion(self):
        for reason in ("retrieval_failed", "time_budget", "invalid_provenance", "provenance_missing"):
            with self.subTest(reason=reason):
                report = self.probe({"papers": [], "partial": True,
                                     "partial_reasons": ["character_budget", reason]})
                self.assertEqual(report["status"], "fail")
                self.assertEqual(report["correctness"], "fail")
                self.assertEqual(report["completeness"]["status"], "incomplete")
                self.assertIn({"step": "get_research_bundle", "kind": reason}, report["errors"])

    def test_legacy_resolution_transport_timeout_is_not_expected_budget(self):
        report = self.probe({"sections": [], "available": 1, "truncated": True,
                            "source_resolution": {"partial": True, "time_budget_exhausted": True,
                                "errors": [{"error": "timeout private detail"}]}}, "get_code_or_math_spec")
        self.assertEqual(report["status"], "fail")
        self.assertIn("retrieval_failed", report["completeness"]["reasons"])
        self.assertIn("time_budget", report["completeness"]["reasons"])
        self.assertNotIn("private detail", json.dumps(report))

    def test_empty_oversized_first_element_is_a_real_failure_not_green_clipping(self):
        report = self.probe({"sections": [], "available": 1, "returned": 0,
                            "chars": 0, "truncated": True}, "get_code_or_math_spec")
        self.assertEqual(report["status"], "fail")
        self.assertIn("empty_budget_page", report["completeness"]["reasons"])
        self.assertFalse(report["spec_read"]["next_cursor_present"])

    def test_continuation_is_reported_without_serializing_cursor(self):
        report = self.probe({"sections": [{"text": "partial equation", "clipped": True}],
                            "available": 1, "returned": 1, "chars": 16, "truncated": True,
                            "next_cursor": "v1:private_cursor_marker"}, "get_code_or_math_spec")
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["spec_read"]["next_cursor_present"])
        self.assertNotIn("private_cursor_marker", json.dumps(report))

    def test_unknown_partial_and_arbitrary_reason_fail_closed_without_leaking(self):
        for supplied in (None, ["secret-reason-marker"], "invalid-shape"):
            payload = {"papers": [], "partial": True}
            if supplied is not None:
                payload["partial_reasons"] = supplied
            report = self.probe(payload)
            self.assertEqual(report["status"], "fail")
            self.assertIn("unexplained_partial", report["completeness"]["reasons"])
            self.assertNotIn("secret-reason-marker", json.dumps(report))

    def test_invalid_url_fails_even_if_provenance_flag_claims_success(self):
        report = self.probe({"papers": [{"id": "generic:paper", "title": "Paper",
                            "url": "javascript:private_marker", "provenance_missing": False}]})
        self.assertEqual(report["status"], "fail")
        self.assertIn("invalid_provenance", report["completeness"]["reasons"])
        self.assertNotIn("private_marker", json.dumps(report))


class ExpandedReadonlyProbeTests(unittest.TestCase):
    def test_reads_profile_calls_exact_nine_read_tools_and_honors_actual_signatures(self):
        transport = FakeTransport()
        report = run_probe("https://read.example/mcp", profile="reads", post=transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["http_requests_attempted"], MAX_HTTP_REQUESTS)
        self.assertEqual(report["tool_calls_attempted"], 9)
        self.assertEqual(report["inventory_count"], 13)
        self.assertEqual(report["discovery"][0]["id"], "agent:42")
        self.assertEqual(report["paper_read"]["id"], "agent:42")
        self.assertEqual(set(report["runtime_tool_coverage"]), {"find_papers", "get_paper", "count_papers",
            "similar_papers", "read_paper_section", "get_code_or_math_spec", "compare_methods",
            "get_research_bundle", "research_trends"})
        tool_calls = [call[0]["params"] for call in transport.calls if call[0].get("method") == "tools/call"]
        by_name = {call["name"]: call["arguments"] for call in tool_calls}
        self.assertIn("arxiv_id", by_name["get_code_or_math_spec"])
        self.assertEqual(by_name["get_code_or_math_spec"]["max_chars"], 1000)
        self.assertIn("arxiv_id_a", by_name["compare_methods"])
        self.assertEqual(by_name["get_research_bundle"]["max_chars"], 1000)
        self.assertIn("top", by_name["research_trends"])
        bundle_call = next(call for call in transport.calls if call[0].get("params", {}).get("name") == "get_research_bundle")
        self.assertEqual(bundle_call[2][1], 8)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["spec_read"]["section_count"], 0)
        self.assertNotIn("must not leak", json.dumps(report))
        self.assertEqual(transport.calls[-1][3], False)
        self.assertNotIn("private-session-id", json.dumps(report))
        self.assertTrue(all(x[2][0] == 3 and x[2][1] <= 12 for x in transport.calls))

    def test_semantic_profile_has_three_same_layer_variants_plus_scope_and_cross_domain(self):
        transport = FakeTransport()
        report = run_probe("https://read.example/mcp", profile="semantic", post=transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["http_requests_attempted"], 9)
        probes = report["semantic_probes"]
        self.assertEqual(len(probes), 6)
        self.assertEqual([p["layer"] for p in probes[:3]], ["ai-agents"] * 3)
        self.assertEqual([p["kind"] for p in probes[:3]], ["paraphrase"] * 3)
        self.assertEqual(probes[3]["kind"], "negative_scope_only")
        self.assertEqual([p["layer"] for p in probes[4:]], ["llm-slm", "web3"])
        self.assertEqual(len(report["paraphrase_overlap"]["pairwise_shared_ids"]), 3)
        self.assertIn("not recall", report["interpretation"])

    def test_inventory_mismatch_fails_before_tool_calls(self):
        transport = FakeTransport(inventory=EXPECTED_TOOLS - {"count_papers"})
        report = run_probe("https://read.example/mcp", post=transport)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["inventory_count"], 12)
        self.assertEqual(len(transport.calls), 3)

    def test_empty_discovery_is_a_real_failure(self):
        report = run_probe("https://read.example/mcp", post=FakeTransport(empty=True))
        self.assertEqual(report["status"], "fail")
        self.assertIn({"step": "find_papers", "kind": "empty_or_invalid_discovery"}, report["errors"])

    def test_tool_error_and_timeout_are_sanitized_failures(self):
        failed = run_probe("https://read.example/mcp", profile="reads", post=FakeTransport(fail_tool="find_papers"))
        self.assertEqual(failed["status"], "fail")
        self.assertNotIn("secret error body", json.dumps(failed))
        timed = run_probe("https://read.example/mcp", profile="reads", post=FakeTransport(raise_at=1))
        self.assertEqual(timed["errors"][0]["kind"], "transport_or_decode")
        self.assertNotIn("transport detail", json.dumps(timed))
        self.assertEqual(timed["completeness"]["status"], "incomplete")

    def test_partial_and_truncated_are_distinguished_and_fail(self):
        partial = run_probe("https://read.example/mcp", profile="reads", post=FakeTransport(marker=("find_papers", "partial")))
        truncated = run_probe("https://read.example/mcp", profile="reads", post=FakeTransport(marker=("get_code_or_math_spec", "truncated")))
        self.assertTrue(partial["partial"])
        self.assertTrue(truncated["truncated"])
        self.assertEqual(partial["status"], "fail")
        self.assertEqual(truncated["status"], "pass")

    def test_malformed_payload_fails_closed_and_no_tool_count_is_falsely_claimed(self):
        report = run_probe("https://read.example/mcp", profile="reads",
                           post=FakeTransport(malformed_tool="get_paper"))
        self.assertEqual(report["status"], "fail")
        self.assertIn({"step": "get_paper", "kind": "malformed_payload"}, report["errors"])
        self.assertEqual(len(report["runtime_tool_coverage"]), 9)

    def test_rejects_non_https_without_transport(self):
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            run_probe("http://read.example/mcp", profile="reads", post=transport)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
