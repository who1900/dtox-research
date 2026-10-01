"""Offline schema/proxy tests: inspect AST, avoiding FastMCP version coupling."""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
import unittest
from unittest.mock import Mock


class RequestException(Exception):
    pass


class MCPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {"search_research_paper", "get_research_bundle", "_handle_error"}
        cls.functions = {node.name: node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name in names}
        cls.decorators = {name: list(node.decorator_list) for name, node in cls.functions.items()}
        for node in cls.functions.values():
            node.decorator_list = []
        module = ast.Module(body=list(cls.functions.values()), type_ignores=[])
        cls.code = compile(ast.fix_missing_locations(module), str(path), "exec")

    def setUp(self):
        self.requests = SimpleNamespace(post=Mock(), Response=object,
                                        RequestException=RequestException)
        self.namespace = {"requests": self.requests, "Optional": Optional, "List": List,
                          "RESEARCH_API_BASE": "http://internal.invalid",
                          "_headers": lambda: {"Content-Type": "application/json"}}
        exec(self.code, self.namespace)
        self.response = Mock(status_code=200, ok=True)
        self.response.json.return_value = {"evidence": [{"id": "paper:1"}]}
        self.requests.post.return_value = self.response

    def call(self, name, **kwargs):
        return self.namespace[name](**kwargs)

    def test_both_tools_are_publicly_decorated(self):
        for name in ("search_research_paper", "get_research_bundle"):
            with self.subTest(tool=name):
                self.assertEqual(len(self.decorators[name]), 1)
                decorator = self.decorators[name][0]
                self.assertIsInstance(decorator, ast.Call)
                self.assertIsInstance(decorator.func, ast.Attribute)
                self.assertEqual(decorator.func.attr, "tool")
                self.assertIsInstance(decorator.func.value, ast.Name)
                self.assertEqual(decorator.func.value.id, "mcp")
                self.assertEqual(decorator.args, [])
                self.assertEqual(decorator.keywords, [])
                node = self.functions[name]
                self.assertTrue(ast.get_docstring(node))

    def test_search_schema_preserves_defaults_and_adds_optional_strict(self):
        signature = inspect.signature(self.namespace["search_research_paper"])
        self.assertEqual(signature.parameters["query"].annotation, str)
        self.assertEqual(signature.parameters["limit"].default, 8)
        self.assertIs(signature.parameters["dedupe"].default, True)
        self.assertEqual(signature.parameters["strict"].annotation, bool)
        self.assertIs(signature.parameters["strict"].default, False)
        self.assertEqual(signature.return_annotation, dict)

    def test_bundle_schema(self):
        signature = inspect.signature(self.namespace["get_research_bundle"])
        self.assertEqual(list(signature.parameters), ["query", "layer", "limit", "max_chars", "strict"])
        self.assertIs(signature.parameters["query"].default, inspect.Parameter.empty)
        self.assertEqual(signature.parameters["query"].annotation, str)
        self.assertEqual(signature.parameters["layer"].annotation, Optional[str])
        self.assertIsNone(signature.parameters["layer"].default)
        for name, default in (("limit", 3), ("max_chars", 12000)):
            self.assertEqual(signature.parameters[name].annotation, int)
            self.assertEqual(signature.parameters[name].default, default)
        self.assertEqual(signature.parameters["strict"].annotation, bool)
        self.assertIs(signature.parameters["strict"].default, True)
        self.assertEqual(signature.return_annotation, dict)

    def test_default_search_preserves_api_auto_relax_default(self):
        self.call("search_research_paper", query="consensus")
        self.requests.post.assert_called_once_with(
            "http://internal.invalid/v1/search", headers={"Content-Type": "application/json"},
            json={"query": "consensus", "limit": 8, "compact": True, "strict": False}, timeout=15)

    def test_strict_search_forwards_filters_and_disables_auto_relax(self):
        self.call("search_research_paper", query="consensus", layer="web3", strict=True,
                  terms=["consensus"], section_type="method", element_type="algorithm",
                  year_from=2020, year_to=2026, min_score=0.0, dedupe=False, limit=4)
        body = self.requests.post.call_args.kwargs["json"]
        self.assertEqual(body, {"query": "consensus", "layer": "web3", "strict": True,
                                "auto_relax": False, "terms": ["consensus"],
                                "section_type": "method", "element_type": "algorithm",
                                "year_from": 2020, "year_to": 2026, "min_score": 0.0,
                                "dedupe": False, "limit": 4, "compact": True})

    def test_default_bundle_posts_once_with_bounded_timeout(self):
        result = self.call("get_research_bundle", query="consensus")
        self.assertEqual(result, self.response.json.return_value)
        self.requests.post.assert_called_once_with(
            "http://internal.invalid/v1/research/bundle",
            headers={"Content-Type": "application/json"},
            json={"query": "consensus", "limit": 3, "max_chars": 12000, "strict": True},
            timeout=30)

    def test_bundle_forwards_overrides_and_non_strict(self):
        self.call("get_research_bundle", query="agents", layer="ai-agents",
                  limit=2, max_chars=3000, strict=False)
        self.assertEqual(self.requests.post.call_args.kwargs["json"],
                         {"query": "agents", "layer": "ai-agents", "limit": 2,
                          "max_chars": 3000, "strict": False})

    def test_read_tools_share_http_error_handling(self):
        cases = ((400, {"detail": "invalid query"}, "bad request: invalid query"),
                 (401, {}, "internal auth error contacting research API (401)"),
                 (404, {"detail": "paper not found"}, "paper not found"),
                 (429, {}, "rate limit exceeded on research API, try again shortly"),
                 (422, {}, "research API error (status 422)"),
                 (503, {}, "research API error (status 503)"))
        for tool in ("search_research_paper", "get_research_bundle"):
            for status, payload, expected in cases:
                with self.subTest(tool=tool, status=status):
                    self.response.status_code, self.response.ok = status, False
                    self.response.json.return_value = payload
                    self.assertEqual(self.call(tool, query="consensus"), {"error": expected})

    def test_read_tools_report_network_errors(self):
        self.requests.post.side_effect = RequestException("timed out")
        for tool in ("search_research_paper", "get_research_bundle"):
            with self.subTest(tool=tool):
                self.assertEqual(self.call(tool, query="consensus"),
                                 {"error": "could not reach research API: timed out"})

    def test_read_tools_preserve_error_detail_fallbacks(self):
        for tool in ("search_research_paper", "get_research_bundle"):
            for status, expected in ((400, "bad request: bad request"), (404, "not found")):
                for malformed in (False, True):
                    with self.subTest(tool=tool, status=status, malformed=malformed):
                        self.response.status_code, self.response.ok = status, False
                        self.response.json.return_value = {}
                        self.response.json.side_effect = ValueError("not JSON") if malformed else None
                        self.assertEqual(self.call(tool, query="consensus"), {"error": expected})

    def test_bundle_has_no_registry_or_chain_calls(self):
        node = self.functions["get_research_bundle"]
        paths = [value.value for value in ast.walk(node)
                 if isinstance(value, ast.Constant) and isinstance(value.value, str)
                 and value.value.startswith("/v1/")]
        self.assertEqual(paths, ["/v1/research/bundle"])
        posts = [value for value in ast.walk(node) if isinstance(value, ast.Call)
                 and isinstance(value.func, ast.Attribute) and value.func.attr == "post"]
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
