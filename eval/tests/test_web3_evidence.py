import ast
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

from eval import check_web3_evidence as bench


class Web3EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.row = bench.load_bench()[0]
        self.spec = {"arxiv_id": self.row["expected_id"], "sections": [
            {"element_type": "code", "section_type": "method",
             "section_title": "Specification", "text": "mock structural evidence"}]}

    def test_canonical_ids_and_tracked_chainlink(self):
        rows = bench.load_bench()
        self.assertEqual({r["expected_id"] for r in rows}, bench.KNOWN_IDS)
        source = Path(__file__).resolve().parents[2] / "pipeline" / "whitepapers.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        papers = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "WHITEPAPERS" for t in node.targets))
        self.assertIn("chainlink-v2", {p[0] for p in papers})
        self.assertNotIn("erc:4337", {r["expected_id"] for r in rows})
        self.assertTrue(all("element_id" not in json.dumps(row) for row in rows))

    def test_offline_default_never_requests_or_claims_live_pass(self):
        with patch.object(bench, "request_json") as request, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bench.main([]), 0)
        request.assert_not_called()
        self.assertIsNone(json.loads(out.getvalue())["known_expected_pass"])

    def test_network_requires_two_explicit_options(self):
        for args in (["--network"], ["--api", "http://localhost:8010"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bench.main(args)

    def test_origin_rejects_credentials_query_and_other_schemes(self):
        for value in ("file:///tmp", "http://user:pass@host", "http://host?a=b", "http://host/v1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bench.api_base(value)

    def test_rank_and_structure_and_only_read_endpoints(self):
        fetch = unittest.mock.Mock(side_effect=[
            {"results": [{"arxiv_id": "other"}, {"arxiv_id": self.row["expected_id"]}]}, self.spec])
        result = bench.run_case(self.row, "http://localhost", fetch=fetch)
        self.assertEqual(result["rank"], 2)
        self.assertTrue(result["known_expected_pass"])
        self.assertEqual(fetch.call_args_list[0].args[1:3], ("POST", "/v1/search"))
        self.assertEqual(fetch.call_args_list[1].args[1:3],
                         ("GET", "/v1/paper/eip%3A7702/spec?max_chars=12000"))
        self.assertEqual(set(result["timing_ms"]), {"search", "structure", "total"})

    def test_missing_wrong_paper_empty_or_prose_structure_fails(self):
        bad_specs = [{"arxiv_id": "wrong", "sections": self.spec["sections"]},
                     {"arxiv_id": self.row["expected_id"], "sections": []}]
        for change in ({"text": " "}, {"element_type": "prose"}):
            bad_specs.append({"arxiv_id": self.row["expected_id"],
                              "sections": [dict(self.spec["sections"][0], **change)]})
        for spec in bad_specs:
            with self.subTest(spec=spec):
                self.assertFalse(bench.structural_pass(spec, self.row))

    def test_partial_rank_and_http_errors_are_not_passes(self):
        hit = {"results": [{"arxiv_id": self.row["expected_id"]}], "partial": True}
        result = bench.run_case(self.row, "http://localhost",
                                fetch=unittest.mock.Mock(side_effect=[hit, self.spec]))
        self.assertTrue(result["partial"])
        self.assertFalse(result["known_expected_pass"])
        error = urllib.error.HTTPError("http://secret", 503, "secret body", None, None)
        result = bench.run_case(self.row, "http://localhost",
                                fetch=unittest.mock.Mock(side_effect=[error, self.spec]))
        self.assertEqual(result["errors"], [{"phase": "search", "detail": "HTTP 503"}])
        self.assertNotIn("secret", json.dumps(result))

    def test_missing_result_and_malformed_response_fail(self):
        for data in ({"results": []}, {"results": "bad"}):
            result = bench.run_case(self.row, "http://localhost",
                                    fetch=unittest.mock.Mock(side_effect=[data, self.spec]))
            self.assertFalse(result["known_expected_pass"])

    def test_reported_baselines_preserve_miss_and_low_ranks(self):
        rows = bench.load_bench()
        measured = [row for row in rows if "baseline" in row]
        self.assertEqual([row["query"] for row in measured], [
            "EIP-7702", "account abstraction with a separate user operation mempool and bundlers",
            "blob carrying transactions for rollup data availability", "durable nonce transaction",
            "on-chain oracle validating external real-world event data", "verifiable delay function"])
        self.assertEqual([row["max_rank"] for row in measured], [3, 3, 10, 4, 3, 5])
        self.assertTrue(all(row["baseline"]["literal_query_confirmed"] is True for row in measured))
        self.assertEqual({row["baseline"]["source"] for row in measured},
                         {"lead live MCP before change,2026-10-02"})
        self.assertEqual(len(rows) - len(measured), 3)
        baseline = {r["expected_id"]: r["baseline"]["rank"] for r in rows if "baseline" in r}
        self.assertEqual(baseline, {"eip:4337": 1, "eip:4844": 9, "eip:7702": 1,
                                   "simd:0297": 3, "wp:chainlink-v2": None, "iacr:2018/601": 4})
        oracle = next(r for r in rows if r["qid"] == "oracle-chainlink-v2-descriptive")
        self.assertNotIn("structure", oracle)
        result = bench.run_case(oracle, "http://localhost",
                                fetch=unittest.mock.Mock(side_effect=[{"results": []}, self.spec]))
        self.assertFalse(result["known_expected_pass"])
        self.assertFalse(result["baseline_hit_at3"])
        self.assertEqual(result["known_extraction_gap"], "lead verified spec returned 0 elements")

    def test_mixed_valid_structure_and_context_truncation_not_retrieval_failure(self):
        self.spec["sections"].append(dict(self.spec["sections"][0], element_type="prose"))
        self.spec["truncated"] = True
        result = bench.run_case(self.row, "http://localhost", fetch=unittest.mock.Mock(side_effect=[
            {"results": [{"arxiv_id": self.row["expected_id"]}]}, self.spec]))
        self.assertTrue(result["known_expected_pass"])
        self.assertTrue(result["context_truncated"])
        self.assertFalse(result["partial"])

    def test_rank9_acceptable_but_top3_failure_remains_visible(self):
        row = next(r for r in bench.load_bench() if r["expected_id"] == "eip:4844")
        hits = [{"arxiv_id": "other"}] * 8 + [{"arxiv_id": row["expected_id"]}]
        result = bench.run_case(row, "http://localhost", fetch=unittest.mock.Mock(return_value={"results": hits}))
        self.assertTrue(result["known_expected_pass"])
        self.assertFalse(result["hit_at3"])
        self.assertEqual(result["rank_delta"], 0)

    def test_authenticated_network_uses_selected_env_without_printing_it(self):
        with (patch.dict(bench.os.environ, {"MOCK_DTOX_AUTH": "mock-test-key"}),
              patch.object(bench, "request_json", side_effect=[
                  {"results": [{"arxiv_id": self.row["expected_id"]}]}, self.spec]) as request,
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(bench.main(["--network", "--api", "http://localhost", "--limit", "1",
                                         "--api-key-env", "MOCK_DTOX_AUTH"]), 0)
        self.assertEqual(request.call_args.kwargs["api_key"], "mock-test-key")
        self.assertNotIn("mock-test-key", out.getvalue())

    def test_no_signed_or_write_endpoint(self):
        with self.assertRaises(ValueError):
            bench.request_json("http://localhost", "POST", "/v1/verdict/message", {})
        self.assertIsNone(bench.NoRedirect().redirect_request(None, None, 302, "", {}, "http://other"))


if __name__ == "__main__":
    unittest.main()
