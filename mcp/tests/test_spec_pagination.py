"""Offline MCP forwarding and snapshot-bound spec pagination contract."""
import inspect
import json
import unittest

from api.evidence import EvidenceCursorError, bounded_evidence_page, provenance
from test_paper_id_aliases import PaperIdAliasTests


class SpecPaginationTests(PaperIdAliasTests):
    def test_cursor_is_additive_optional_string_and_defaults_stay_unchanged(self):
        signature = inspect.signature(self.namespace["get_code_or_math_spec"])
        self.assertEqual(list(signature.parameters), ["paper_id", "target_elements", "include_prose",
                                                      "max_chars", "arxiv_id", "cursor"])
        self.assertIsNone(signature.parameters["cursor"].default)
        self.assertEqual(signature.parameters["max_chars"].default, 12000)
        self.assertIs(signature.parameters["include_prose"].default, False)
        self.namespace["get_code_or_math_spec"]("generic:paper")
        self.assertNotIn("cursor", self.requests.get.call_args.kwargs["params"])

    def test_protocol_forwards_cursor_and_raw_hash_invariant(self):
        fragment = {"text": "α📚" * 650, "element_type": "equation"}
        row = {**fragment, "evidence": provenance("generic:paper", fragment,
               {"fulltext_source": "latex", "source_url": "https://example.test/paper"}, scope="indexed_chunk")}
        first = bounded_evidence_page([row], 500)
        self.response.json.return_value = {"arxiv_id": "generic:paper", **first}
        result = self.payload(self.protocol_call("get_code_or_math_spec", {"paper_id": "generic:paper", "max_chars": 500}))
        self.assertTrue(result["sections"][0]["clipped"])
        self.assertEqual(len(result["sections"][0]["text"]), 500)
        self.assertIsInstance(result["next_cursor"], str)
        second = bounded_evidence_page([row], 500, result["next_cursor"])
        self.response.json.return_value = {"arxiv_id": "generic:paper", **second}
        result = self.payload(self.protocol_call("get_code_or_math_spec", {"paper_id": "generic:paper",
                              "max_chars": 500, "cursor": result["next_cursor"]}))
        self.assertEqual(self.requests.get.call_args.kwargs["params"]["cursor"], first["next_cursor"])
        self.assertEqual(result["sections"][0]["text"], fragment["text"][500:1000])
        self.assertEqual(result["sections"][0]["evidence"]["fragment_hash"], row["evidence"]["fragment_hash"])
        self.assertEqual(result["sections"][0]["evidence"]["fragment_chars"], len(fragment["text"]))
        self.assertEqual(result["sections"][0]["evidence"]["returned_offset"], 500)

    def test_invalid_cursor_shape_is_a_managed_error_without_http(self):
        for cursor in (True, {}, "", " " * 2, "x" * 1025):
            self.requests.get.reset_mock()
            with self.subTest(cursor=type(cursor).__name__), self.assertRaises(Exception) as caught:
                self.namespace["get_code_or_math_spec"]("generic:paper", cursor=cursor)
            self.assertEqual(json.loads(str(caught.exception))["status"], 400)
            self.requests.get.assert_not_called()

    def test_invalid_and_changed_cursor_errors_have_distinct_statuses(self):
        fragment = {"text": "x" * 600, "element_type": "equation"}
        row = {**fragment, "evidence": provenance("generic:paper", fragment, scope="indexed_chunk")}
        cursor = bounded_evidence_page([row], 500)["next_cursor"]
        for invalid in ("{", "[]", "null", "x" * 1025, json.dumps({"snapshot_hash": "z" * 64})):
            with self.subTest(cursor=invalid[:8]), self.assertRaises(EvidenceCursorError) as caught:
                bounded_evidence_page([row], 500, invalid)
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(EvidenceCursorError) as caught:
            bounded_evidence_page([{**row, "text": "changed"}], 500, cursor)
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
