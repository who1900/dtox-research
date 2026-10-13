"""Offline checks for the public MCP tool catalog and signed-write boundary."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Union
import unittest
from unittest.mock import Mock

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


PUBLIC_TOOLS = {
    "search_research_paper", "get_research_bundle", "find_papers", "get_paper",
    "read_paper_section", "count_papers", "similar_papers", "get_code_or_math_spec",
    "compare_methods", "get_verdict_message", "record_signed_verdict",
    "research_trends", "validate_project",
}


class MCPToolSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        cls.public_tool_names = {
            node.name for node in functions
            if any(isinstance(decorator, ast.Call)
                   and isinstance(decorator.func, ast.Attribute)
                   and decorator.func.attr == "tool"
                   and isinstance(decorator.func.value, ast.Name)
                   and decorator.func.value.id == "mcp"
                   for decorator in node.decorator_list)
        }
        for node in functions:
            node.decorator_list = []
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                           str(path), "exec")

    def setUp(self):
        self.response = Mock(status_code=200, ok=True, headers={})
        self.response.json.return_value = {"results": []}
        self.requests = SimpleNamespace(get=Mock(return_value=self.response),
                                        post=Mock(return_value=self.response),
                                        Response=object, RequestException=requests.RequestException)
        self.namespace = {"requests": self.requests, "json": json, "ToolError": ToolError,
                          "Optional": Optional, "List": List, "Union": Union,
                          "RESEARCH_API_BASE": "http://internal.invalid",
                          "REGISTRY_WRITES_ENABLED": False,
                          "_headers": lambda: {"Content-Type": "application/json"}}
        exec(self.code, self.namespace)
        self.server = SDKServer("offline-public-surface", log_level="CRITICAL")
        for name in self.public_tool_names:
            self.server.tool()(self.namespace[name])

    def list_tools(self):
        if SDK_V1:
            request = types.ListToolsRequest(method="tools/list")
            handler = self.server._mcp_server.request_handlers[types.ListToolsRequest]
            result = asyncio.run(handler(request))
            return result.root.tools
        return asyncio.run(self.server.list_tools())

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

    def test_default_public_catalog_has_thirteen_valid_schemas_and_no_unsigned_writes(self):
        tools = self.list_tools()
        by_name = {tool.name: tool.model_dump(by_alias=True) for tool in tools}

        self.assertEqual(set(by_name), PUBLIC_TOOLS)
        self.assertEqual(len(by_name), 13)
        self.assertNotIn("record_claim_judgment", by_name)
        self.assertNotIn("link_claim_nodes", by_name)
        for name, tool in by_name.items():
            with self.subTest(tool=name):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema["properties"], dict)

    def test_malformed_signed_payload_is_protocol_error_without_proxy_request(self):
        result = self.protocol_call("record_signed_verdict", {
            "claim": "fixture claim", "judgments": "not-a-list",
        })

        self.assertTrue(result["isError"])
        self.requests.post.assert_not_called()
        self.requests.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
