import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Union
import unittest
from unittest.mock import Mock
from urllib.parse import quote

import requests
from mcp import types

try:
    from mcp.server.fastmcp import FastMCP as SDKServer
    from mcp.server.fastmcp.exceptions import ToolError
    SDK_V1 = True
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as SDKServer
    from mcp.server.mcpserver.exceptions import ToolError
    SDK_V1 = False


class MCPAuditErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "server.py"
        cls.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        cls.instructions = next(node.value.value for node in cls.tree.body
                                if isinstance(node, ast.Assign) and any(
                                    isinstance(target, ast.Name) and target.id == "INSTRUCTIONS"
                                    for target in node.targets))
        cls.functions = [node for node in cls.tree.body if isinstance(node, ast.FunctionDef)
                         and node.name != "_headers"]
        cls.docstrings = {node.name: ast.get_docstring(node) for node in cls.functions}
        for node in cls.functions:
            node.decorator_list = []
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=cls.functions, type_ignores=[])),
                           str(path), "exec")

    def setUp(self):
        self.response = Mock(status_code=200, ok=True, headers={})
        self.response.json.return_value = {"title": "Proof – 東京", "message": "Exact UTF-8: café"}
        self.requests = SimpleNamespace(get=Mock(return_value=self.response),
                                        post=Mock(return_value=self.response), Response=object,
                                        RequestException=requests.RequestException)
        self.namespace = {"requests": self.requests, "json": json, "ToolError": ToolError,
                          "Optional": Optional, "List": List, "Union": Union, "quote": quote,
                          "RESEARCH_API_BASE": "http://internal.invalid",
                          "REGISTRY_WRITES_ENABLED": True,
                          "_headers": lambda: {"Content-Type": "application/json"}}
        exec(self.code, self.namespace)
        self.server = SDKServer("offline-regression", log_level="CRITICAL")
        for name in ("get_paper", "search_research_paper", "get_verdict_message", "record_signed_verdict"):
            self.server.tool()(self.namespace[name])

    def protocol_call(self, name, arguments):
        if SDK_V1:
            request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                name=name, arguments=arguments))
            handler = self.server._mcp_server.request_handlers[types.CallToolRequest]
            result = asyncio.run(handler(request))
            return result.root.model_dump(by_alias=True)
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        result = asyncio.run(self.server._handle_call_tool(None, params))
        return result.model_dump(by_alias=True)

    def error_payload(self, result):
        self.assertTrue(result["isError"])
        text = "\n".join(c["text"] for c in result["content"] if c["type"] == "text")
        return json.loads(text[text.index("{"):])

    def success_payload(self, result):
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        if result.get("structuredContent") is not None:
            self.assertEqual(result["structuredContent"], payload)
        return payload

    def test_success_is_not_protocol_error_and_preserves_utf8(self):
        result = self.protocol_call("get_paper", {"paper_id": "iacr:2025/1040"})
        self.assertEqual(self.success_payload(result), self.response.json.return_value)
        self.assertEqual(self.requests.get.call_args.args[0],
                         "http://internal.invalid/v1/paper/iacr:2025/1040")

    def test_http_errors_are_real_protocol_errors_with_status_and_detail(self):
        for status in (400, 401, 404, 429, 422, 503):
            with self.subTest(status=status):
                self.response.status_code, self.response.ok = status, False
                self.response.json.return_value = {"detail": "Diagnostic – 東京", "request_id": "req-1"}
                payload = self.error_payload(self.protocol_call("get_paper", {"paper_id": "missing"}))
                self.assertEqual(payload["status"], status)
                self.assertEqual(payload["detail"], "Diagnostic – 東京")
                self.assertEqual(payload["request_id"], "req-1")
                self.assertTrue(payload["error"])

    def test_rate_limit_diagnostics_survive(self):
        self.response.status_code, self.response.ok = 429, False
        self.response.json.return_value = {"detail": "Too many requests", "retry_after": 12}
        self.response.headers = {"Retry-After": "12", "X-RateLimit-Remaining": "0"}
        payload = self.error_payload(self.protocol_call("get_paper", {"paper_id": "missing"}))
        self.assertEqual(payload["retry_after"], 12)
        self.assertEqual(payload["rate_limit_headers"], self.response.headers)

    def test_transport_failure_is_real_protocol_error(self):
        self.requests.get.side_effect = requests.ConnectionError("offline transport failure")
        payload = self.error_payload(self.protocol_call("get_paper", {"paper_id": "missing"}))
        self.assertIn("offline transport failure", payload["error"])
        self.assertIsNone(payload["status"])
        self.assertEqual(payload["error_type"], "transport")

    def test_json_error_in_successful_http_response_is_not_tool_success(self):
        self.response.json.return_value = {"error": "No structured elements", "paper_id": "paper:1"}
        payload = self.error_payload(self.protocol_call("get_paper", {"paper_id": "paper:1"}))
        self.assertEqual(payload, {"error": "No structured elements", "paper_id": "paper:1", "status": 200})

    def test_signed_endpoints_preserve_payloads_and_success(self):
        arguments = {"claim": "café", "paper_id": "eip:7702", "verdict": "partial",
                     "evidence_sha256": "a" * 64}
        result = self.protocol_call("get_verdict_message", arguments)
        self.assertEqual(self.success_payload(result), self.response.json.return_value)
        self.assertEqual(self.requests.post.call_args.kwargs["json"], arguments)
        self.assertTrue(self.requests.post.call_args.args[0].endswith("/v1/verdict/message"))
        signed = {"claim": "café", "judgments": [{"signature": "fixture", "issued_at": "fixture"}],
                  "layer": "web3"}
        self.response.json.return_value = {"results": [{"status": 200, "attestation_tx": "fixture"}]}
        result = self.protocol_call("record_signed_verdict", signed)
        self.assertEqual(self.success_payload(result), self.response.json.return_value)
        self.assertEqual(self.requests.post.call_args.kwargs["json"], signed)
        self.assertTrue(self.requests.post.call_args.args[0].endswith("/v1/adjudicate/signed"))

    def test_fully_rejected_signed_batch_is_protocol_error_at_http_200(self):
        self.response.json.return_value = {
            "results": [{"status": 400, "error": "invalid signature"},
                        {"status": 429, "error": "rate limited", "retry_after": 12}],
            "papers": [], "onchain": []}
        result = self.protocol_call("record_signed_verdict", {"claim": "fixture", "judgments": []})
        payload = self.error_payload(result)
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["results"], self.response.json.return_value["results"])
        self.assertEqual(payload["papers"], [])
        self.assertEqual(payload["onchain"], [])
        self.assertIn("all signed verdict judgments were rejected", payload["error"])

    def test_partially_successful_signed_batch_keeps_all_item_statuses(self):
        self.response.json.return_value = {
            "results": [{"status": 200, "attestation_tx": "fixture"},
                        {"status": 400, "error": "invalid signature"}],
            "onchain": [{"attestation": "fixture"}]}
        result = self.protocol_call("record_signed_verdict", {"claim": "fixture", "judgments": []})
        self.assertEqual(self.success_payload(result), self.response.json.return_value)

    def test_empty_signed_batch_is_not_assumed_to_be_fully_rejected(self):
        self.response.json.return_value = {"results": []}
        result = self.protocol_call("record_signed_verdict", {"claim": "fixture", "judgments": []})
        self.assertEqual(self.success_payload(result), {"results": []})

    def test_prepare_description_does_not_claim_no_local_writes(self):
        doc = self.docstrings["get_verdict_message"].lower()
        self.assertIn("prepare", doc)
        self.assertIn("no chain", doc)
        self.assertNotIn("writes nothing", doc)

    def test_instructions_do_not_treat_large_coverage_as_absence_or_novelty(self):
        instructions = " ".join(self.instructions.split())
        self.assertIn("Empty results reflect index coverage and retrieval limits, never establish novelty; "
                      "inspect evidence and scope.", instructions)
        self.assertNotIn("over a large corpus_coverage is evidence", instructions)

    def test_every_request_handler_raises_on_transport_failure(self):
        self.requests.get.side_effect = self.requests.post.side_effect = requests.Timeout("offline")
        import inspect
        for node in self.functions:
            if not any(isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
                       and child.value.id == "requests" and child.attr in ("get", "post")
                       for child in ast.walk(node)):
                continue
            function = self.namespace[node.name]
            arguments = {name: ([] if name in ("claims", "judgments") else "fixture")
                         for name, parameter in inspect.signature(function).parameters.items()
                         if parameter.default is inspect.Parameter.empty}
            with self.subTest(tool=node.name), self.assertRaises(ToolError):
                function(**arguments)


if __name__ == "__main__":
    unittest.main()
