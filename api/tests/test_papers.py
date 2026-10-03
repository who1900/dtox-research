import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import main


class PaperSectionTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        stack.enter_context(patch.object(main, "auth_and_limit"))
        stack.enter_context(patch.object(main.requests, "post", side_effect=AssertionError("live request")))
        self.text = "a" * 600 + "\n\n" + "b" * 400
        points = [{"payload": {"title": "Paper", "section_title": "Method",
                               "section_type": "method", "chunk_index": index, "text": text}}
                  for index, text in ((1, "b" * 400), (0, "a" * 600))]
        self.scroll = stack.enter_context(patch.object(main, "qdrant_scroll_by_arxiv", return_value=points))
        self.rows = stack.enter_context(patch.object(main, "_paper_rows", return_value={
            "2401.1": {"fulltext_source": "latex"}}))

    def test_negative_offset_is_422_before_retrieval(self):
        for offset in (-1, -500):
            with self.subTest(offset=offset):
                response = self.client.get("/v1/paper/2401.1/section",
                                           params={"title": "Method", "offset": offset})
                self.assertEqual(response.status_code, 422)
                self.assertTrue(any(error["loc"] == ["query", "offset"]
                                    for error in response.json()["detail"]))
        self.scroll.assert_not_called()
        self.rows.assert_not_called()

    def test_default_offset_matches_explicit_zero(self):
        params = {"title": "Method", "max_chars": 500}
        default = self.client.get("/v1/paper/2401.1/section", params=params)
        zero = self.client.get("/v1/paper/2401.1/section", params=dict(params, offset=0))
        self.assertEqual(default.status_code, 200)
        self.assertEqual(zero.status_code, 200)
        self.assertEqual(default.json(), zero.json())
        data = default.json()
        self.assertEqual(data["text"], self.text[:500])
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["next_offset"], 500)
        self.assertEqual(data["total_chars"], len(self.text))
        self.assertEqual(data["evidence"]["offset"], 0)

    def test_valid_offsets_preserve_paging_and_evidence(self):
        for offset, next_offset in ((500, 1000), (1000, None), (1002, None), (1500, None)):
            with self.subTest(offset=offset):
                response = self.client.get("/v1/paper/2401.1/section", params={
                    "section_type": "method", "offset": offset, "max_chars": 500})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["text"], self.text[offset:offset + 500])
                self.assertLessEqual(len(data["text"]), 500)
                self.assertEqual(data["offset"], offset)
                self.assertEqual(data["next_offset"], next_offset)
                self.assertEqual(data["total_chars"], len(self.text))
                self.assertEqual(data["evidence"]["offset"], offset)
                self.assertEqual(data["evidence"]["fragment_scope"], "returned_section_page")

    def test_direct_call_keeps_default_zero(self):
        data = main.paper_section("2401.1", title="Method", max_chars=500)
        self.assertEqual(data["text"], self.text[:500])
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["next_offset"], 500)


if __name__ == "__main__":
    unittest.main()
