"""MCP protocol compatibility tests for neutral and legacy paper ID names."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
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


class PaperIdAliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {"_path_id", "_resolve_paper_id", "_raise_tool_error", "_handle_error",
                 "_api_result", "get_code_or_math_spec", "compare_methods"}
        functions = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        cls.functions = {node.name: node for node in functions}
        for node in functions:
            node.decorator_list = []
        module = ast.Module(body=functions, type_ignores=[])
        cls.code = compile(ast.fix_missing_locations(module), str(path), "exec")

    def setUp(self):
        self.response = Mock(status_code=200, headers={})
        self.response.json.return_value = {"arxiv_id": "source-id", "title": "Fixture",
                                           "sections": [{"text": "x = y"}]}
        self.requests = SimpleNamespace(get=Mock(return_value=self.response), Response=object,
                                        RequestException=requests.RequestException)
        namespace = {"requests": self.requests, "json": json, "ToolError": ToolError,
                     "Optional": Optional, "quote": quote,
                     "RESEARCH_API_BASE": "http://internal.invalid",
                     "_headers": lambda: {"Content-Type": "application/json"}}
        exec(self.code, namespace)
        self.namespace = namespace
        self.server = SDKServer("offline-paper-id-aliases", log_level="CRITICAL")
        for name in ("get_code_or_math_spec", "compare_methods"):
            self.server.tool()(namespace[name])

    def protocol_call(self, name, arguments):
        if SDK_V1:
            request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                name=name, arguments=arguments))
            handler = self.server._mcp_server.request_handlers[types.CallToolRequest]
            return asyncio.run(handler(request)).root.model_dump(by_alias=True)
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(self.server._handle_call_tool(None, params)).model_dump(by_alias=True)

    def list_tools(self):
        if SDK_V1:
            request = types.ListToolsRequest(method="tools/list")
            handler = self.server._mcp_server.request_handlers[types.ListToolsRequest]
            return asyncio.run(handler(request)).root.tools
        return asyncio.run(self.server.list_tools())

    def payload(self, result):
        self.assertFalse(result["isError"])
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])

    def error_payload(self, result):
        self.assertTrue(result["isError"])
        text = "\n".join(item["text"] for item in result["content"]
                          if item.get("type") == "text")
        return json.loads(text[text.index("{"):])

    def test_catalog_has_both_optional_identifier_names(self):
        schema_attr = "inputSchema" if SDK_V1 else "input_schema"
        schemas = {tool.name: getattr(tool, schema_attr) for tool in self.list_tools()}
        spec = schemas["get_code_or_math_spec"]
        compare = schemas["compare_methods"]
        self.assertIn("paper_id", spec["properties"])
        self.assertIn("arxiv_id", spec["properties"])
        self.assertNotIn("paper_id", spec.get("required", []))
        self.assertNotIn("arxiv_id", spec.get("required", []))
        for field in ("paper_id_a", "paper_id_b", "arxiv_id_a", "arxiv_id_b"):
            self.assertIn(field, compare["properties"])
            self.assertNotIn(field, compare.get("required", []))

    def test_get_spec_accepts_legacy_keyword_and_new_neutral_keyword(self):
        legacy = self.protocol_call("get_code_or_math_spec", {"arxiv_id": "eip:1559"})
        self.assertEqual(self.payload(legacy)["arxiv_id"], "source-id")
        self.assertEqual(self.payload(legacy)["paper_id"], "eip:1559")
        self.assertEqual(self.requests.get.call_args.args[0],
                         "http://internal.invalid/v1/paper/eip:1559/spec")

        self.response.json.return_value = {"arxiv_id": "iacr:2019/953", "title": "Fixture",
                                           "sections": []}
        neutral = self.protocol_call("get_code_or_math_spec", {"paper_id": "iacr:2019/953"})
        self.assertEqual(self.payload(neutral)["paper_id"], "iacr:2019/953")
        self.assertEqual(self.requests.get.call_args.args[0],
                         "http://internal.invalid/v1/paper/iacr:2019/953/spec")

    def test_legacy_positional_get_spec_still_maps_arguments_in_order(self):
        self.namespace["get_code_or_math_spec"]("eip:1559", "equation", True, 3200)

        self.assertEqual(self.requests.get.call_args.args[0],
                         "http://internal.invalid/v1/paper/eip:1559/spec")
        self.assertEqual(self.requests.get.call_args.kwargs["params"], {
            "include_prose": "true", "max_chars": 3200, "target_elements": "equation"})

    def test_spec_conflict_and_missing_id_are_protocol_errors_without_http(self):
        for arguments in (
            {"paper_id": "eip:1559", "arxiv_id": "iacr:2019/953"},
            {},
        ):
            with self.subTest(arguments=arguments):
                self.requests.get.reset_mock()
                result = self.protocol_call("get_code_or_math_spec", arguments)
                self.assertEqual(self.error_payload(result)["status"], 400)
                self.requests.get.assert_not_called()

    def test_non_string_identifier_is_a_managed_400_before_http(self):
        with self.assertRaises(ToolError) as caught:
            self.namespace["get_code_or_math_spec"](12345)

        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["status"], 400)
        self.assertEqual(payload["error_type"], "validation")
        self.requests.get.assert_not_called()

    def test_compare_accepts_legacy_and_neutral_pair_and_adds_aliases(self):
        self.response.json.return_value = {
            "a": {"arxiv_id": "legacy-a", "title": "A"},
            "b": {"arxiv_id": "legacy-b", "title": "B"},
        }
        legacy = self.protocol_call("compare_methods", {
            "arxiv_id_a": "eip:1559", "arxiv_id_b": "iacr:2019/953"})
        payload = self.payload(legacy)
        self.assertEqual(payload["a"]["paper_id"], "eip:1559")
        self.assertEqual(payload["b"]["paper_id"], "iacr:2019/953")
        self.assertEqual(self.requests.get.call_args.kwargs["params"]["a"], "eip:1559")
        self.assertEqual(self.requests.get.call_args.kwargs["params"]["b"], "iacr:2019/953")

        self.response.json.return_value = {"a": {}, "b": {}}
        neutral = self.protocol_call("compare_methods", {
            "paper_id_a": "eip:1559", "paper_id_b": "iacr:2019/953"})
        self.assertEqual(self.payload(neutral), {
            "a": {"paper_id": "eip:1559"}, "b": {"paper_id": "iacr:2019/953"}})

    def test_legacy_positional_compare_and_mixed_alias_pair_remain_valid(self):
        self.namespace["compare_methods"]("eip:1559", "iacr:2019/953", True, 4000)
        self.assertEqual(self.requests.get.call_args.kwargs["params"], {
            "a": "eip:1559", "b": "iacr:2019/953",
            "extract_tables_only": "true", "max_chars": 4000})

        result = self.protocol_call("compare_methods", {
            "paper_id_a": "eip:1559", "arxiv_id_b": "iacr:2019/953"})
        self.assertFalse(result["isError"])

    def test_compare_conflict_or_missing_side_fails_before_http(self):
        for arguments in (
            {"paper_id_a": "eip:1559", "arxiv_id_a": "simd:0096", "paper_id_b": "eip:7702"},
            {"paper_id_a": "eip:1559"},
        ):
            with self.subTest(arguments=arguments):
                self.requests.get.reset_mock()
                result = self.protocol_call("compare_methods", arguments)
                self.assertEqual(self.error_payload(result)["status"], 400)
                self.requests.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
