"""Canonical document layers for bounded stage-B IDs; chunk facets stay exact."""
import copy
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from api import main
from api.evidence import provenance


def matches(condition, payload):
    if "key" in condition:
        value = payload.get(condition["key"])
        if "match" in condition:
            target = condition["match"]
            values = value if isinstance(value, list) else [value]
            if "any" in target:
                return any(item in target["any"] for item in values)
            return target["value"] in values
        return value is not None and all(
            value >= bound if op == "gte" else value <= bound
            for op, bound in condition["range"].items())
    return (all(matches(item, payload) for item in condition.get("must", []))
            and (not condition.get("should") or any(matches(item, payload) for item in condition["should"]))
            and not any(matches(item, payload) for item in condition.get("must_not", [])))


class GlobalMetadataFilterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.db"
        writer = sqlite3.connect(path)
        with writer:
            columns = main.PAPER_COLUMNS.split(", ")
            writer.execute("CREATE TABLE papers(" + ",".join(
                "arxiv_id TEXT PRIMARY KEY" if column == "arxiv_id" else f"{column} TEXT"
                for column in columns) + ",status TEXT)")
            writer.executemany("INSERT INTO papers(arxiv_id,layers,status) VALUES (?,?,?)", [
                ("canonical", "builder-tech,web3", "done"),
                ("negative", "web3", "done"),
                ("pending", "builder-tech", "chunked"),
                ("no-layers", None, "done"),
                ("outside", "builder-tech", "done"),
            ])
        writer.close()
        self.reader = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        self.addCleanup(self.reader.close)
        guard = patch.object(main, "_ro_conn", return_value=self.reader)
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.object(main, "HIER_GROUPED_SEARCH", False)
        guard.start()
        self.addCleanup(guard.stop)
        self.layer = {"key": "layers", "match": {"any": ["builder-tech"]}}
        self.facets = [
            {"key": "section_type", "match": {"value": "method"}},
            {"key": "element_type", "match": {"value": "prose"}},
            {"key": "year", "range": {"gte": 2019, "lte": 2026}},
            {"key": "terms", "match": {"any": ["fuzzer"]}},
        ]
        self.payload = {"arxiv_id": "canonical", "layers": ["web3"], "year": 2021,
                        "section_type": "method", "element_type": "prose", "terms": ["fuzzer"]}

    def stage_filter(self, ids, must=None):
        must = [self.layer, *self.facets] if must is None else must
        before = copy.deepcopy((must, ids))
        with patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])) as search:
            main._hier_stage_b_hits([0.1], must, ids, 120, 3)
        self.assertEqual((must, ids), before)
        if not search.called:
            return None
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.args[2:], (120, max(len(ids), 1), 3))
        self.assertEqual(search.call_args.kwargs, {"layers": None})
        return search.call_args.args[1]

    def test_stale_chunk_layer_admitted_from_done_document_metadata(self):
        statements = []
        self.reader.set_trace_callback(statements.append)
        filt = self.stage_filter(["canonical", "canonical"])
        self.assertTrue(matches(filt, self.payload))
        self.assertEqual(filt["must"], self.facets + [
            {"key": "arxiv_id", "match": {"any": ["canonical"]}}])
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        self.assertEqual(len(selects), 1)
        self.assertIn("WHERE arxiv_id IN ('canonical')", selects[0])
        self.assertNotIn("GROUP BY", selects[0])

    def test_mixed_branch_preserves_unknown_old_layer_and_rejects_known_negative(self):
        ids = ["canonical", "negative", "unknown", "pending", "no-layers"]
        filt = self.stage_filter(ids)
        self.assertTrue(matches(filt, self.payload))
        self.assertFalse(matches(filt, {**self.payload, "arxiv_id": "negative", "layers": ["builder-tech"]}))
        self.assertFalse(matches(filt, {**self.payload, "arxiv_id": "outside"}))
        for pid in ("unknown", "pending", "no-layers"):
            self.assertFalse(matches(filt, {**self.payload, "arxiv_id": pid}))
            self.assertTrue(matches(filt, {**self.payload, "arxiv_id": pid, "layers": ["builder-tech"]}))
        self.assertEqual(len(filt["should"]), 2)
        self.assertIn(self.layer, filt["should"][1]["must"])

    def test_section_element_year_terms_remain_chunk_constraints_in_both_branches(self):
        filt = self.stage_filter(["canonical", "unknown"])
        for pid, layers in (("canonical", ["web3"]), ("unknown", ["builder-tech"])):
            valid = {**self.payload, "arxiv_id": pid, "layers": layers}
            self.assertTrue(matches(filt, valid))
            for field, value in (("section_type", "introduction"), ("element_type", "table"),
                                 ("year", 2018), ("year", 2027), ("terms", ["unrelated"])):
                with self.subTest(paper=pid, field=field, value=value):
                    self.assertFalse(matches(filt, {**valid, field: value}))

    def test_known_canonical_negative_never_issues_qdrant_search(self):
        self.assertIsNone(self.stage_filter(["negative"]))

    def test_unknown_and_db_error_keep_exact_old_filter(self):
        expected = {"must": [self.layer, *self.facets,
                             {"key": "arxiv_id", "match": {"any": ["unknown"]}}]}
        self.assertEqual(self.stage_filter(["unknown"]), expected)
        for error in (sqlite3.DatabaseError("offline fixture"), RuntimeError("offline fixture")):
            with self.subTest(error=type(error).__name__), patch.object(main, "_ro_conn", side_effect=error):
                filt = self.stage_filter(["canonical"])
            self.assertEqual(filt["must"][:-1], [self.layer, *self.facets])
            self.assertFalse(matches(filt, self.payload))

    def test_mixed_shortlist_uses_one_metadata_batch_one_search_and_regular_hydration(self):
        points = [{"id": pid, "score": 0.9, "payload": {**self.payload, "arxiv_id": pid}}
                  for pid in ("canonical", "negative", "unknown", "outside")]
        points.append({"id": "unknown-matching", "score": 0.8,
                       "payload": {**self.payload, "arxiv_id": "unknown", "layers": ["builder-tech"]}})
        calls = []
        def post(url, *, json, timeout):
            calls.append((url, copy.deepcopy(json)))
            if url.endswith("/points/search"):
                result = [{"id": point["id"], "score": point["score"]} for point in points
                          if matches(json["filter"], point["payload"])][:json["limit"]]
            else:
                self.assertTrue(url.endswith("/points"))
                result = [point for point in points if point["id"] in json["ids"]]
            response = Mock()
            response.json.return_value = {"result": result}
            return response
        main.qdrant_result_cache.data.clear()
        self.addCleanup(main.qdrant_result_cache.data.clear)
        with patch.object(main.requests, "post", side_effect=post), \
                patch.object(main, "_paper_rows", wraps=main._paper_rows) as metadata:
            hits = main._hier_stage_b_hits([0.1], [self.layer, *self.facets],
                                          ["canonical", "negative", "unknown"], 120, 3)
        self.assertEqual([hit["id"] for hit in hits], ["canonical", "unknown-matching"])
        metadata.assert_called_once_with(["canonical", "negative", "unknown"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/points/search"))
        self.assertTrue(calls[1][0].endswith("/points"))
        self.assertFalse(calls[0][1]["with_payload"])
        self.assertEqual(calls[0][1]["limit"], 120)

    def test_no_layer_empty_shortlist_or_oversized_input_never_reads_metadata(self):
        with patch.object(main, "_paper_rows", side_effect=AssertionError("no metadata batch expected")):
            filt = self.stage_filter(["canonical"], self.facets)
            self.assertFalse(matches(filt, {**self.payload, "arxiv_id": "outside"}))
            self.assertIsNone(self.stage_filter([]))
            filt = self.stage_filter([f"fixture-{i}" for i in range(501)])
            self.assertIn(self.layer, filt["must"])

    def test_multiple_layer_conditions_remain_conjunctive(self):
        extra = {"key": "layers", "match": {"value": "web3"}}
        filt = self.stage_filter(["canonical", "unknown"], [self.layer, extra, *self.facets])
        self.assertTrue(matches(filt, self.payload))
        self.assertFalse(matches(filt, {**self.payload, "arxiv_id": "unknown", "layers": ["builder-tech"]}))
        self.assertTrue(matches(filt, {**self.payload, "arxiv_id": "unknown",
                                     "layers": ["builder-tech", "web3"]}))

    def test_unsupported_layer_condition_is_preserved_without_metadata_read(self):
        layer = {"key": "layers", "match": {"except": ["web3"]}}
        with patch.object(main, "_paper_rows", side_effect=AssertionError("unsupported canonical match")):
            filt = self.stage_filter(["canonical"], [layer, *self.facets])
        self.assertEqual(filt["must"][:-1], [layer, *self.facets])

    def test_grouped_setting_is_not_enabled_and_existing_path_uses_same_filter(self):
        self.assertFalse(main.HIER_GROUPED_SEARCH)
        with patch.object(main, "HIER_GROUPED_SEARCH", True), \
                patch.object(main, "grouped_hits", return_value=[]) as grouped:
            main._hier_stage_b_hits([0.1], [self.layer, *self.facets], ["canonical", "unknown"], 120, 3)
        filt = grouped.call_args.args[4]
        self.assertTrue(matches(filt, self.payload))
        self.assertFalse(matches(filt, {**self.payload, "arxiv_id": "unknown"}))

    def test_strict_floor_still_drops_low_score_hier_evidence(self):
        hit = {"id": "fixture-chunk", "score": 0.7,
               "payload": {**self.payload, "title": "Fixture", "text": "method body"}}
        main.search_cache.data.clear()
        self.addCleanup(main.search_cache.data.clear)
        with patch.object(main, "HIER_SEARCH", True), \
                patch.object(main, "PAPER_LEXICAL", False), \
                patch.object(main, "HIER_GRAPH", False), \
                patch.object(main, "embed_query", return_value=[0.1]), \
                patch.object(main, "_coarse_paper_search", return_value=[{"arxiv_id": "canonical", "paper_score": 0.9}]), \
                patch.object(main, "two_phase_dense_search", return_value=([hit], [], [], [])), \
                patch.object(main, "_paper_facts", return_value={}), \
                patch.object(main, "_bm25_candidates", return_value=[]), \
                patch.object(main, "_inject_exact", side_effect=lambda results, *args: results):
            result = main._run_search(main.SearchBody(query="strict canonical fixture", layer="builder-tech",
                                                     strict=True, min_score=0.85, hybrid=False,
                                                     diagnose=False, auto_relax=False, limit=5))
        self.assertEqual(result["results"], [])


class PaperSpecPaginationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main, "auth_and_limit"))
        self.stack.enter_context(patch.object(main, "_trusted_alias_ids", return_value=[]))
        self.stack.enter_context(patch.object(main, "_twins", return_value={}))
        self.stack.enter_context(patch("requests.sessions.Session.request",
                                       side_effect=AssertionError("network forbidden")))
        self.pid = "record:fixture"
        self.text = "α📚 method → проверка\n" * 30
        self.payload = {"arxiv_id": self.pid, "title": "Fixture", "element_type": "algorithm",
                        "section_type": "method", "section_title": "Method", "chunk_index": 0,
                        "text": self.text, "repos": ["https://example.test/repo"]}
        self.metadata = {"arxiv_id": self.pid, "fulltext_source": "latex", "parser_mode": "pylatexenc",
                         "source_url": "https://example.test/paper.tex", "parse_status": "ok"}

    def spec(self, payloads=None, metadata=None, **kwargs):
        payloads = [self.payload] if payloads is None else payloads
        rows = {self.pid: self.metadata} if metadata is None else metadata
        with patch.object(main, "qdrant_scroll_by_arxiv", return_value=[{"payload": row} for row in payloads]), \
                patch.object(main, "_paper_rows", return_value=rows) as fetch:
            result = main.paper_spec(self.pid, **kwargs)
        fetch.assert_called_once_with([self.pid])
        return result

    def test_four_formats_clip_first_element_full_hash_and_lossless_v1_resume(self):
        for acquisition, parser, extraction, latex in (
                ("latex", "pylatexenc", "latex_source", True),
                ("latex", "markdown", "markdown_source", False),
                ("oa-mirror-pdf", "markdown", "pdf_text", False),
                ("openalex-abstract", "markdown", "abstract_text", False)):
            with self.subTest(acquisition=acquisition, parser=parser):
                metadata = {self.pid: {**self.metadata, "fulltext_source": acquisition, "parser_mode": parser}}
                texts, cursor, pages = [], None, []
                while True:
                    page = self.spec(metadata=metadata, max_chars=137, cursor=cursor)
                    pages.append(page)
                    self.assertEqual(page["available"], 1)
                    self.assertEqual(page["returned"], 1)
                    self.assertLessEqual(page["chars"], 137)
                    section = page["sections"][0]
                    self.assertEqual(section["source_paper_id"], self.pid)
                    self.assertEqual(section["evidence"]["source_paper_id"], self.pid)
                    self.assertEqual(section["evidence"]["fragment_chars"], len(self.text))
                    self.assertEqual(section["evidence"]["returned_chars"], len(section["text"]))
                    self.assertEqual(section["evidence"]["fragment_hash"],
                                     provenance(self.pid, self.payload, metadata[self.pid], scope="indexed_chunk")["fragment_hash"])
                    self.assertEqual(section["evidence"]["extraction_type"], extraction)
                    self.assertEqual(section["evidence"]["exact_latex"], latex)
                    texts.append(section["text"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                    self.assertTrue(cursor.startswith("v1:"))
                    self.assertLessEqual(len(cursor), 1024)
                    self.assertLess(len(pages), 10)
                self.assertEqual("".join(texts), self.text)
                self.assertTrue(pages[0]["truncated"])
                self.assertFalse(pages[-1]["truncated"])
                self.assertEqual(pages[1]["sections"][0]["evidence"]["returned_offset"], 137)
                self.assertEqual(self.payload["text"], self.text)

    def test_order_counts_default_budget_and_empty_elements_are_compatible(self):
        payloads = [{**self.payload, "element_type": element, "text": element, "chunk_index": i}
                    for i, element in enumerate(("table", "code", "equation", "algorithm"))]
        result = self.spec(payloads=payloads)
        self.assertEqual(result["available"], 4)
        self.assertEqual(result["returned"], 4)
        self.assertEqual([row["element_type"] for row in result["sections"]],
                         ["algorithm", "equation", "code", "table"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["source_resolution"]["missing"], {})
        self.assertFalse(result["source_resolution"]["partial"])
        self.assertEqual(result["repos"], self.payload["repos"])
        empty = self.spec(payloads=[{**self.payload, "element_type": "prose"}])
        self.assertEqual((empty["available"], empty["returned"], empty["chars"]), (0, 0, 0))
        self.assertIn("include_prose=true", empty["note"])
        self.assertEqual(set(empty["source_resolution"]["missing"].values()), {"not_found_in_retrieved_chunks"})
        self.assertEqual(empty["source_resolution"]["source_wide_absence"], "unknown")
        self.assertIn("not_found_in_retrieved_chunks", empty["source_resolution"]["reasons"])
        included = self.spec(payloads=[{**self.payload, "element_type": "prose"}], include_prose=True)
        self.assertEqual(included["available"], 1)

    def test_nonpositive_budget_and_oversized_cursor_are_400_before_retrieval(self):
        for kwargs in ({"max_chars": 0}, {"max_chars": -1}, {"cursor": "v1:" + "x" * 1024}):
            with self.subTest(kwargs=kwargs), patch.object(main, "qdrant_scroll_by_arxiv") as scroll:
                with self.assertRaises(main.HTTPException) as error:
                    main.paper_spec(self.pid, **kwargs)
                self.assertEqual(error.exception.status_code, 400)
                scroll.assert_not_called()

    def test_invalid_cursor_is_400_stale_snapshot_is_409_and_internal_dict_resumes(self):
        page = self.spec(max_chars=137)
        resumed = self.spec(max_chars=137, cursor=page["continuation"])
        self.assertEqual(resumed["sections"][0]["text"], self.text[137:274])
        for cursor in ("invalid", "v1:not-base64!"):
            with self.assertRaises(main.HTTPException) as error:
                self.spec(max_chars=137, cursor=cursor)
            self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(main.HTTPException) as error:
            self.spec(payloads=[{**self.payload, "text": "changed"}], max_chars=137, cursor=page["next_cursor"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("snapshot changed", error.exception.detail)

    def test_alias_fragment_preserves_actual_source_identity_and_hash(self):
        alias = "record:alias"
        equation = {**self.payload, "arxiv_id": alias, "element_type": "equation", "text": "x = y"}
        rows = {self.pid: self.metadata, alias: {**self.metadata, "arxiv_id": alias}}
        with patch.object(main, "_trusted_alias_ids", return_value=[alias]), \
                patch.object(main, "qdrant_scroll_by_arxiv", side_effect=[
                    [{"payload": self.payload}], [{"payload": equation}]]), \
                patch.object(main, "_paper_rows", return_value=rows):
            result = main.paper_spec(self.pid, max_chars=1000)
        section = next(row for row in result["sections"] if row["element_type"] == "equation")
        self.assertEqual(section["source_paper_id"], alias)
        self.assertEqual(section["evidence"]["fragment_hash"],
                         provenance(alias, equation, rows[alias], scope="indexed_chunk")["fragment_hash"])

    def test_metadata_failure_never_certifies_fulltext(self):
        result = self.spec(metadata={}, max_chars=137)
        self.assertIsNone(result["fulltext"])
        self.assertIsNone(result["sections"][0]["evidence"]["fulltext"])
        self.assertEqual(result["sections"][0]["evidence"]["extraction_type"], "unknown")

    def test_http_route_helper_pagination_and_cursor_error_statuses(self):
        client = TestClient(main.app)
        self.addCleanup(client.close)
        route = f"/v1/paper/{self.pid}/spec"
        with patch.object(main, "qdrant_scroll_by_arxiv", return_value=[{"payload": self.payload}]), \
                patch.object(main, "_paper_rows", return_value={self.pid: self.metadata}):
            first = client.get(route, params={"max_chars": 137})
            self.assertEqual(first.status_code, 200)
            page = first.json()
            self.assertEqual(page["sections"][0]["text"], self.text[:137])
            self.assertEqual(page["available"], 1)
            self.assertGreater(page["returned"], 0)
            second = client.get(route, params={"max_chars": 137, "cursor": page["next_cursor"]})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["sections"][0]["text"], self.text[137:274])
            self.assertEqual(second.json()["sections"][0]["evidence"]["fragment_hash"],
                             page["sections"][0]["evidence"]["fragment_hash"])
            for params in ({"max_chars": 0}, {"cursor": "invalid"}, {"cursor": "v1:" + "x" * 1024}):
                self.assertEqual(client.get(route, params=params).status_code, 400)
            with patch.object(main, "qdrant_scroll_by_arxiv", return_value=[{"payload": {**self.payload, "text": "new"}}]):
                stale = client.get(route, params={"max_chars": 137, "cursor": page["next_cursor"]})
            self.assertEqual(stale.status_code, 409)
            self.assertIn("snapshot changed", stale.json()["detail"])

    def test_http_route_resolution_errors_and_time_budget_propagate_with_text(self):
        client = TestClient(main.app)
        self.addCleanup(client.close)
        route = f"/v1/paper/{self.pid}/spec"
        for error, clocks, reason in (
                (TimeoutError("fixture timeout"), None, "retrieval_failed"),
                (None, [0, 0, main.SPEC_SECONDS + 1], "time_budget")):
            with self.subTest(reason=reason), ExitStack() as stack:
                stack.enter_context(patch.object(main, "_trusted_alias_ids", return_value=["record:alias"]))
                stack.enter_context(patch.object(main, "_paper_rows", return_value={self.pid: self.metadata}))
                if clocks:
                    clock = Mock(wraps=main.time)
                    clock.monotonic.side_effect = clocks
                    stack.enter_context(patch.object(main, "time", clock))
                stack.enter_context(patch.object(main, "qdrant_scroll_by_arxiv", side_effect=[
                    [{"payload": self.payload}], error] if error else [[{"payload": self.payload}]]))
                result = client.get(route, params={"max_chars": 137})
            self.assertEqual(result.status_code, 200)
            page = result.json()
            self.assertGreater(page["returned"], 0)
            self.assertIn(reason, page["partial_reasons"])
            self.assertIn(reason, page["source_resolution"]["partial_reasons"])
            self.assertEqual(page["source_resolution"]["reasons"], page["source_resolution"]["partial_reasons"])
            self.assertTrue(page["source_resolution"]["partial"])
            self.assertEqual(page["source_resolution"]["source_wide_absence"], "unknown")

    def test_chunk_limit_is_partial_even_when_all_requested_types_are_present(self):
        payloads = [{**self.payload, "text": "x", "chunk_index": i} for i in range(1000)]
        page = self.spec(payloads=payloads, target_elements="algorithm", max_chars=2000)
        self.assertFalse(page["truncated"])
        self.assertEqual(page["source_resolution"]["missing"], {})
        self.assertTrue(page["source_resolution"]["partial"])
        self.assertEqual(page["source_resolution"]["partial_reasons"], ["chunk_limit"])
        self.assertEqual(page["partial_reasons"], ["chunk_limit"])


if __name__ == "__main__":
    unittest.main()
