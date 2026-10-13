import unittest
from io import StringIO
import sqlite3
from unittest.mock import patch

from api import main
from api.source_attribution import _catalog


def _hit(paper_id, score, layer):
    return {"id": paper_id, "score": score, "payload": {
        "arxiv_id": paper_id, "title": f"Paper {paper_id}", "text": "method fragment",
        "section_type": "method", "section_title": "Method", "layers": [layer],
    }}


class DiscoveryHydrationOnceTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()

    def _soft_search(self, discovery):
        ids = {
            "ai-agents": ("2401.00001", "2401.00002"),
            "llm-slm": ("2401.00003", "2401.00004"),
        }
        hits = {
            layer: [_hit(primary, 0.94, layer), _hit(candidate, 0.60, layer)]
            for layer, (primary, candidate) in ids.items()
        }
        hier_meta = {"paper_scores": {}, "global_channel": "used", "global_extra": []}
        observed = {}
        body = main.SearchBody(query="agentic tool planning", layer="ai-agents", hybrid=True,
                               dedupe=True, diagnose=False, auto_relax=False, limit=2)
        real_merge = main._merge_soft_layers
        real_candidate_merge = main.merge_source_candidates

        def hier(_vector, _filter, _must, inner_body, *_args, **_kwargs):
            return hits[inner_body.layer], [], [], dict(hier_meta)

        def merge_soft(*args):
            results = real_merge(*args)
            observed["primary_before"] = [row["arxiv_id"] for row in results]
            observed["primary_rank_before"] = [
                (row["arxiv_id"], row.get("score"), row.get("rank_score"), row.get("fusion_score"))
                for row in results]
            return results

        def merge_candidates(*args):
            candidates = real_candidate_merge(*args)
            observed["candidates_before"] = [row["arxiv_id"] for row in candidates]
            observed["candidate_rank_before"] = [
                (row["arxiv_id"], row.get("score"), row.get("candidate_channel"), row.get("fusion_score"))
                for row in candidates]
            return candidates

        metadata = {
            paper_id: {"title": f"Paper {paper_id}", "source_url": f"https://papers.example/{paper_id}",
                       "fulltext_source": "latex"}
            for pair in ids.values() for paper_id in pair
        }
        with (patch.object(main, "SOURCE_DISCOVERY", discovery),
              patch.object(main, "SOFT_LAYERS", True),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "PAPER_LEXICAL", False),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_hier_run", side_effect=hier),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_merge_soft_layers", side_effect=merge_soft),
              patch.object(main, "merge_source_candidates", side_effect=merge_candidates),
              patch.object(main, "_evidence_rows", return_value=metadata) as rows_read,
              patch.object(main, "_lookup_note", return_value=None),
              patch.object(main, "_inject_exact", side_effect=lambda result, *_args: result)):
            payload = main._run_search(body)

        observed["metadata_reads"] = rows_read.call_args_list
        observed["expected_ids"] = ids
        return payload, observed

    def test_discovery_soft_layer_hydrates_union_once_after_fusion(self):
        payload, observed = self._soft_search(discovery=True)
        before = observed["primary_before"]
        after = [row["arxiv_id"] for row in payload["results"]]
        self.assertEqual(after, before)
        self.assertEqual(
            [(row["arxiv_id"], row.get("score"), row.get("rank_score"), row.get("fusion_score"))
             for row in payload["results"]], observed["primary_rank_before"])
        candidates_before = observed["candidates_before"]
        candidates_after = [row["arxiv_id"] for row in payload["source_candidates"]]
        self.assertEqual(candidates_after, candidates_before)
        self.assertEqual(
            [(row["arxiv_id"], row.get("score"), row.get("candidate_channel"), row.get("fusion_score"))
             for row in payload["source_candidates"]], observed["candidate_rank_before"])

        self.assertEqual(len(observed["metadata_reads"]), 1)
        requested = observed["metadata_reads"][0].args[0]
        response_ids = after + candidates_after
        self.assertEqual(set(requested), set(response_ids))
        self.assertEqual(len(requested), len(set(requested)))
        for row in [*payload["results"], *payload["source_candidates"]]:
            self.assertTrue(row["fulltext"])
            self.assertEqual(row["evidence"]["source_url"], row["url"])

    def test_discovery_off_keeps_inner_hydration_behavior(self):
        payload, observed = self._soft_search(discovery=False)
        self.assertEqual(len(observed["metadata_reads"]), 3)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]],
                         observed["primary_before"])
        self.assertTrue(all(row["fulltext"] for row in payload["results"]))

    def test_non_soft_top_false_search_still_hydrates_when_discovery_is_on(self):
        body = main.SearchBody(query="ordinary direct internal query", layer="web3", hybrid=True,
                               dedupe=True, diagnose=False, auto_relax=False, limit=1)
        row = {"title": "Paper 2401.00009", "source_url": "https://papers.example/2401.00009",
               "fulltext_source": "latex"}
        extra = {"paper_scores": {}, "global_channel": "used", "global_extra": []}
        with (patch.object(main, "SOURCE_DISCOVERY", True),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "PAPER_LEXICAL", False),
              patch.object(main, "embed_query", return_value=[0.1]),
              patch.object(main, "_hier_run", return_value=([_hit("2401.00009", 0.94, "web3")], [], [], extra)),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_evidence_rows", return_value={"2401.00009": row}) as rows_read,
              patch.object(main, "_inject_exact", side_effect=lambda results, *_args: results),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body, _top=False)
        rows_read.assert_called_once_with(["2401.00009"])
        self.assertTrue(payload["results"][0]["fulltext"])

    def test_paper_rows_legacy_schema_fallback_keeps_metadata_and_invalidates_cached_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE papers (arxiv_id TEXT, title TEXT, year INTEGER, venue TEXT, "
            "citation_count INTEGER, influential INTEGER, layers TEXT, abstract TEXT, "
            "fulltext_source TEXT, source_url TEXT, status TEXT)")
        connection.execute(
            "INSERT INTO papers VALUES ('wp:legacy','Paper',2024,NULL,0,0,'web3','a','latex',NULL,'done')")
        statements = []
        connection.set_trace_callback(statements.append)
        with (patch.object(main, "_ro_conn", return_value=connection),
              patch.object(main, "_ro_conn_drop") as drop):
            rows = main._paper_rows(["wp:legacy"])
        self.assertIsNone(rows["wp:legacy"]["source_url"])
        self.assertEqual(rows["wp:legacy"]["fulltext_source"], "latex")
        self.assertIsNone(rows["wp:legacy"]["parser_mode"])
        self.assertIsNone(rows["wp:legacy"]["parse_status"])
        drop.assert_called_once_with(main.STATE_DB_PATH)
        self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements), 1)
        self.assertTrue(any("PRAGMA table_info(papers)" in sql for sql in statements))

    def test_catalog_malformed_root_and_entries_fail_closed(self):
        _catalog.cache_clear()
        self.addCleanup(_catalog.cache_clear)
        malformed = ("{}", '[{"id": "not-wp", "title": "bad", "url": null}]')
        for document in malformed:
            with self.subTest(document=document), \
                    patch("api.source_attribution.Path.open", return_value=StringIO(document)):
                _catalog.cache_clear()
                self.assertEqual(_catalog(), {})

    def test_catalog_invalid_utf8_fails_closed(self):
        _catalog.cache_clear()
        self.addCleanup(_catalog.cache_clear)
        with patch("api.source_attribution.Path.open", side_effect=UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte")):
            self.assertEqual(_catalog(), {})


if __name__ == "__main__":
    unittest.main()
