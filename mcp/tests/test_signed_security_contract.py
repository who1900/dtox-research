"""Offline signed proxies: no SDK startup, database, wallets or network calls."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
import unittest
from unittest.mock import Mock


class ToolError(Exception):
    pass


class RequestException(Exception):
    pass


class SignedSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"get_verdict_message", "record_signed_verdict", "_api_result", "_handle_error", "_raise_tool_error"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in functions:
            node.decorator_list = []
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), str(source), "exec")
        cls.batch_cases = json.loads(source.with_name("tests").joinpath("signed_batch_cases.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.response = Mock(status_code=200, headers={})
        self.response.json.return_value = {"results": [{"status": 200}]}
        self.requests = SimpleNamespace(post=Mock(return_value=self.response), Response=object,
                                        RequestException=RequestException)
        self.namespace = {"requests": self.requests, "json": json, "Optional": Optional, "List": List,
                          "ToolError": ToolError, "RESEARCH_API_BASE": "http://offline.invalid",
                          "_headers": lambda: {"Content-Type": "application/json"}}
        exec(self.code, self.namespace)

    def test_signed_fields_and_foreign_identity_hints_are_not_rewritten_by_proxy(self):
        judgment = {"id": "iacr:2026/321", "verdict": "partial", "reviewer": "fixture-public-key",
                    "signature": "fixture-signature", "issued_at": "2026-10-06T12:00:00+05:00",
                    "evidence_sha256": "a" * 64, "reason": "unsigned rationale"}
        body = {"claim": "Safety — 東京", "judgments": [judgment], "layer": "web3"}
        self.namespace["record_signed_verdict"](**body)
        self.requests.post.assert_called_once_with("http://offline.invalid/v1/adjudicate/signed",
            headers={"Content-Type": "application/json"}, json=body, timeout=60)

    def test_prepare_preserves_claim_paper_verdict_and_evidence_hash(self):
        body = {"claim": "Safety — 東京", "paper_id": "eip:7702", "verdict": "does_not_assert",
                "evidence_sha256": "a" * 64}
        self.namespace["get_verdict_message"](**body)
        self.requests.post.assert_called_once_with("http://offline.invalid/v1/verdict/message",
            headers={"Content-Type": "application/json"}, json=body, timeout=15)

    def test_signed_transport_failure_never_autoretries(self):
        self.requests.post.side_effect = RequestException("offline timeout")
        with self.assertRaises(ToolError) as caught:
            self.namespace["record_signed_verdict"]("fixture claim", [{"id": "paper:1"}])
        self.assertEqual(self.requests.post.call_count, 1)
        self.assertEqual(json.loads(str(caught.exception))["error_type"], "transport")

    def test_all_rejected_batch_keeps_every_status_and_diagnostic(self):
        body = {"results": [{"status": 409, "error": "replay conflict"},
                            {"status": 429, "retry_after": 12}], "onchain": []}
        self.response.json.return_value = body
        with self.assertRaises(ToolError) as caught:
            self.namespace["record_signed_verdict"]("fixture claim", [{"id": "paper:1"}])
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["results"], body["results"])
        self.assertEqual(payload["onchain"], [])

    def test_shared_signed_outcome_contract_preserves_every_item(self):
        for case in self.batch_cases:
            with self.subTest(case=case["name"]):
                body = case["payload"]
                self.response.json.return_value = body
                if case["outcome"] == "accepted":
                    self.assertEqual(self.namespace["record_signed_verdict"](
                        "fixture claim", [{"id": "paper:1"}]), body)
                else:
                    with self.assertRaises(ToolError) as caught:
                        self.namespace["record_signed_verdict"]("fixture claim", [{"id": "paper:1"}])
                    result = json.loads(str(caught.exception))
                    self.assertIn("malformed" if case["outcome"] == "malformed" else "all signed", result["error"])
                    if isinstance(body, dict):
                        for key, value in body.items():
                            self.assertEqual(result[key], value)
                    else:
                        self.assertEqual(result["upstream_result"], body)

    def test_unsigned_read_response_shape_is_unchanged(self):
        for body in ([], {"results": []}, {"papers": {}}):
            self.response.json.return_value = body
            self.assertEqual(self.namespace["_api_result"](self.response), body)


if __name__ == "__main__":
    unittest.main()
