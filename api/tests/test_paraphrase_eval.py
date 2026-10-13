import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval import paraphrase_eval as pe


def fixture():
    return {"groups": [{"id": "g", "layer": "web3", "mechanism": "m",
                        "targets": [{"canonical_id": "target", "aliases": ["alias"]}],
                        "hard_negatives": [{"id": "negative", "reason": "near match"}],
                        "phrases": [{"id": "p1", "query": "one"}, {"id": "p2", "query": "two"}]}]}


def record(pid, results, **extra):
    return {"group_id": "g", "phrase_id": pid, "results": results,
            "error": None, "partial": False, **extra}


class ParaphraseEvalTests(unittest.TestCase):
    def test_alias_and_paper_dedupe_mrr_rank_three_and_ten(self):
        rows = ([{"arxiv_id": "d1"}, {"arxiv_id": "alias"},
                 {"arxiv_id": "target"}] + [{"arxiv_id": f"x{i}"} for i in range(6)] +
                [{"arxiv_id": "target"}])
        result = pe.score(fixture(), [record("p1", rows), record("p2", [{"arxiv_id": f"z{i}"} for i in range(9)] + [{"arxiv_id": "target"}])])
        self.assertEqual(result["phrases"][0]["first_target_rank"], 2)
        self.assertEqual(result["phrases"][0]["MRR@10"], 0.5)
        self.assertEqual(result["phrases"][1]["first_target_rank"], 10)
        self.assertTrue(result["groups"][0]["all_phrasings_hit@10"])

    def test_rank_three_hit(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "a"}, {"arxiv_id": "b"}, {"arxiv_id": "target"}]), record("p2", [{"arxiv_id": "target"}])])
        self.assertTrue(got["phrases"][0]["hit@3"])

    def test_errors_and_partial_are_invalid_not_pass(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "target"}], error="failed"), record("p2", [{"arxiv_id": "target"}], partial=True)])
        self.assertEqual(got["operational_invalid_count"], 2)
        self.assertFalse(got["all_groups_pass"])
        self.assertIsNone(got["phrases"][0]["hit@10"])

    def test_negative_above_target_flagged_but_lower_not_flagged(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "negative"}, {"arxiv_id": "target"}]), record("p2", [{"arxiv_id": "target"}, {"arxiv_id": "negative"}])])
        self.assertEqual(got["negative_flag_count"], 1)
        self.assertEqual(got["negative_flags"][0]["phrase_id"], "p1")

    def test_negative_with_missing_target_flagged_only_within_ten(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "negative"}]), record("p2", [{"arxiv_id": f"x{i}"} for i in range(10)] + [{"arxiv_id": "negative"}])])
        self.assertEqual(got["negative_flag_count"], 1)

    def test_negative_after_top_ten_not_flagged_against_target_rank_twelve(self):
        rows = [{"arxiv_id": f"x{i}"} for i in range(10)] + [{"arxiv_id": "negative"}, {"arxiv_id": "target"}]
        got = pe.score(fixture(), [record("p1", rows), record("p2", [{"arxiv_id": "target"}])])
        self.assertEqual(got["negative_flag_count"], 0)

    def test_missing_and_duplicate_records_are_invalid(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "target"}]),
                                   record("p1", [{"arxiv_id": "target"}])])
        self.assertEqual(got["operational_invalid_count"], 2)
        self.assertFalse(got["all_groups_pass"])

    def test_agreement_identifies_valid_phrasings_only(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "target"}]),
                                   record("p2", None, error="timeout")])
        self.assertIn("target_agreement_valid_phrasings_only", got["groups"][0])

    def test_miss_has_null_worst_rank_and_agreement_known_target_sets(self):
        got = pe.score(fixture(), [record("p1", [{"arxiv_id": "target"}]), record("p2", [{"arxiv_id": "other"}])])
        self.assertIsNone(got["groups"][0]["worst_rank"])
        self.assertIsNone(got["groups"][0]["rank_spread"])
        self.assertFalse(got["all_groups_pass"])

    def test_query_cap_and_budget(self):
        too_many = fixture()
        too_many["groups"][0]["phrases"] = [{"id": str(i), "query": "q"} for i in range(25)]
        with self.assertRaisesRegex(ValueError, "maximum"):
            pe.run(too_many, "http://unused", budget_seconds=1)
        with self.assertRaisesRegex(ValueError, "budget"):
            pe.run(fixture(), "http://unused", budget_seconds=181)

    def test_api_request_uses_default_search_contract_without_strict_only_overrides(self):
        item = {"group_id": "g", "phrase_id": "p", "query": "q", "layer": "web3"}
        with patch.object(pe, "_http_json", return_value={"results": [], "partial": False}) as call:
            pe._request("http://localhost/v1/search", None, item, 3, strict=False)
        self.assertEqual(call.call_args.args[1], {"query": "q", "layer": "web3", "limit": 10,
                                                   "hybrid": True, "strict": False,
                                                   "compact": True})

    def test_strict_disables_relaxation_for_api_and_is_forwarded_to_mcp(self):
        item = {"group_id": "g", "phrase_id": "p", "query": "q", "layer": "web3"}
        with patch.object(pe, "_http_json", return_value={"results": [], "partial": False}) as call:
            pe._request("http://localhost/v1/search", None, item, 3, strict=True)
        self.assertFalse(call.call_args.args[1].get("auto_relax", True))
        pe._mcp_state.initialized = True
        pe._mcp_state.endpoint = "https://example.invalid/mcp"
        with patch.object(pe, "_http_json", return_value={"result": {"structuredContent": {"results": []}}}) as call:
            pe._mcp_call("https://example.invalid/mcp", item, 3, strict=False)
        self.assertFalse(call.call_args.args[1]["params"]["arguments"]["strict"])

    def test_mcp_prefers_structured_content_and_rejects_tool_error(self):
        pe._mcp_state.initialized = True
        pe._mcp_state.endpoint = "https://example.invalid/mcp"
        item = {"query": "q", "layer": "web3"}
        payload = {"results": [{"arxiv_id": "x"}], "channels": ["dense"], "provenance": {"source": "mcp"}}
        with patch.object(pe, "_http_json", return_value={"result": {"structuredContent": payload}}):
            self.assertEqual(pe._mcp_call("https://example.invalid/mcp", item, 3), payload)
        with patch.object(pe, "_http_json", return_value={"result": {"isError": True}}):
            with self.assertRaisesRegex(ValueError, "tool call failed"):
                pe._mcp_call("https://example.invalid/mcp", item, 3)

    def test_mcp_text_json_fallback_and_jsonrpc_error(self):
        pe._mcp_state.initialized = True
        pe._mcp_state.endpoint = "https://example.invalid/mcp"
        item = {"query": "q", "layer": "web3"}
        payload = {"results": []}
        with patch.object(pe, "_http_json", return_value={"result": {"content": [{"type": "text", "text": __import__("json").dumps(payload)}]}}):
            self.assertEqual(pe._mcp_call("https://example.invalid/mcp", item, 3), payload)
        with patch.object(pe, "_http_json", return_value={"error": {"code": -1}}):
            with self.assertRaisesRegex(ValueError, "JSON-RPC error"):
                pe._mcp_call("https://example.invalid/mcp", item, 3)


if __name__ == "__main__":
    unittest.main()
