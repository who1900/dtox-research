import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from api import main


def chunk(pid, score=0.8, layer="web3"):
    return {"score": score, "payload": {"arxiv_id": pid, "layers": [layer]}}


def evidence(pid="paper-a", section="method"):
    return {"arxiv_id": pid, "score": 0.6, "title": "Protocol evidence", "year": 2024,
            "layers": ["web3"], "section_type": section, "text": "Recorded evidence", "terms": []}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main, "embed_query", return_value=[1.0]))
        self.stack.enter_context(patch.object(main.requests, "get", side_effect=AssertionError("offline")))
        self.stack.enter_context(patch.object(main.requests, "post", side_effect=AssertionError("offline")))

    def test_twins_count_once_and_chunk_cap_is_not_a_paper_cap(self):
        twins = {"canonical": {f"copy-{n}": "paper-a" for n in range(4)}}
        details = {}
        hits = [chunk(f"copy-{n % 4}") for n in range(40)]
        with patch.object(main, "_twins", return_value=twins), \
             patch.object(main, "qdrant_search", return_value=hits):
            count, by_layer, _ = main._topic_coverage("zero knowledge", "web3", probe_limit=40, details=details)
        self.assertEqual((count, by_layer), (1, {"web3": 1}))
        self.assertTrue(details["by_layer"]["web3"]["is_lower_bound"])
        self.assertEqual(details["by_layer"]["web3"]["chunk_hits"], 40)
        self.assertEqual(details["by_layer"]["web3"]["canonical_papers"], 1)

    def test_layers_overlap_and_use_existing_floors_not_a_sum(self):
        details = {}
        def probe(vector, query_filter, limit, **kwargs):
            lay = query_filter["must"][0]["match"]["any"][0]
            return [chunk("paper-a", 0.8, lay), chunk("paper-b", 0.75, lay)]
        with patch.object(main, "_twins", return_value={"canonical": {}}), \
             patch.object(main, "qdrant_search", side_effect=probe):
            count, layers, _ = main._topic_coverage("protocol", probe_limit=40, details=details)
        self.assertEqual(count, 2)
        self.assertEqual(layers["web3"], 2)
        self.assertEqual(layers["llm-slm"], 1)
        self.assertFalse(any(p["is_lower_bound"] for p in details["by_layer"].values()))
        self.assertEqual(details["by_layer"]["web3"]["relevance_floor"], main.MIN_RELEVANCE_BY_LAYER["web3"])

    def test_strict_probe_still_requires_the_existing_evidence_band(self):
        with patch.object(main, "qdrant_search", return_value=[chunk("paper-a", 0.71)]), \
             patch.object(main, "_twins", return_value={"canonical": {}}):
            self.assertEqual(main._topic_coverage("protocol", "web3", strict=True)[0], 0)


class AuditReportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for name, value in (("JUDGMENTS_DB_PATH", os.path.join(temp, "judgments.db")),
                            ("_twins", {"canonical": {}}), ("_corpus_stats", {"papers_indexed": 100}),
                            ("_paper_facts", {}), ("trends", {}), ("_graph_coverage", {})):
            self.stack.enter_context(patch.object(main, name, value) if name == "JUDGMENTS_DB_PATH"
                                     else patch.object(main, name, return_value=value))
        self.stack.enter_context(patch.object(main, "auth_and_limit", return_value=("test", {})))
        self.stack.enter_context(patch.object(main, "embed_query", return_value=[1.0, 0.0]))
        self.stack.enter_context(patch.object(main, "prime_query_embeddings"))
        self.stack.enter_context(patch.object(main, "_hydrate_evidence", side_effect=lambda hits: hits))
        self.stack.enter_context(patch.object(main.requests, "get", side_effect=AssertionError("offline")))
        self.stack.enter_context(patch.object(main.requests, "post", side_effect=AssertionError("offline")))

    def record(self, claim, models=(None, None), verdict="asserts"):
        conn = main._judgments_conn()
        try:
            claim_id, _ = main._canonical_claim(conn, claim)
            for n, model in enumerate(models):
                conn.execute("INSERT INTO claim_judgments "
                             "(claim_norm, claim_text, claim_id, paper_id, verdict, judged_by, judged_by_model, judged_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (main._norm_claim(claim), claim, claim_id, "paper-a", verdict,
                              f"wallet:{n}", f"trusted-model:{model}" if model else "unspecified", "2026-10-02"))
            conn.commit()
            return claim_id
        finally:
            conn.close()

    def report(self, claim, retrieved=False, section="method", cap=False):
        def probe(vector, query_filter, limit, **kwargs):
            hits = [chunk(f"scope-{n}") for n in range(4)] if vector == [2.0, 0.0] else [chunk("paper-a", 0.71)]
            return hits * limit if cap and len(hits) == 1 else hits
        def embed(text, *args, **kwargs):
            return [2.0, 0.0] if text == "web3 protocol research" else [1.0, 0.0]
        with patch.object(main, "embed_query", side_effect=embed), \
             patch.object(main, "qdrant_search", side_effect=probe), \
             patch.object(main, "_run_search", return_value={"results": [evidence(section=section)] if retrieved else []}), \
             patch.object(main, "_score_specific_papers", return_value=[evidence(section=section)]):
            return main.validate_project(main.ValidateBody(idea="web3 protocol research", claims=[claim],
                                                           layer="web3", depth="fast"))

    def test_similar_wallet_record_is_hydrated_but_never_current_quorum(self):
        self.record("GRPO group policy optimisation")
        result = self.report("GRPO group relative policy optimisation")
        claim = result["claims"][0]
        self.assertEqual(claim["verdict"], "prior_art_reported_on_similar_claim")
        self.assertEqual(claim["settled"]["confirmed_prior_art"], 0)
        self.assertEqual(claim["settled"]["pending_quorum"], 0)
        self.assertEqual(claim["settled"]["reported_on_similar_claim"], 1)
        self.assertEqual(claim["registry"][0]["models"], [])
        self.assertFalse(claim["registry"][0]["retrieved_today"])
        self.assertTrue(claim["registry"][0]["hydrated_from_registry"])
        self.assertTrue(claim["verification_required"])
        self.assertEqual(claim["out_of_scope_signal"]["verdict"], "thin_retrieval_with_registry_evidence")
        self.assertIn("not zero indexed", claim["out_of_scope_signal"]["reading"])
        self.assertIn("1 papers reported on similar", result["headline"])
        self.assertIn("similar, unlinked claim", claim["evidence"][0]["found_via"])

    def test_linked_wallet_readings_are_current_pending_and_counted_once(self):
        target = self.record("canonical protocol claim")
        alias = "linked protocol claim"
        conn = main._judgments_conn()
        try:
            conn.execute("INSERT INTO claim_links VALUES (?,?,?,?,?)",
                         (main._norm_claim(alias), target, "reader", "same question", "2026-10-02"))
            conn.commit()
        finally:
            conn.close()
        result = self.report(alias, retrieved=True)
        claim = result["claims"][0]
        self.assertEqual(claim["verdict"], "prior_art_reported_pending_quorum")
        self.assertEqual(claim["settled"]["pending_quorum"], 1)
        self.assertEqual(claim["settled"]["reported_on_similar_claim"], 0)
        self.assertEqual(claim["matches"]["strong_read"], 1)
        self.assertTrue(claim["registry"][0]["retrieved_today"])
        self.assertFalse(claim["registry"][0]["hydrated_from_registry"])
        self.assertTrue(claim["verification_required"])
        self.assertIn("1 papers read against current", result["headline"])
        self.assertIn("0 settled", result["headline"])

    def test_trusted_current_quorum_is_not_downgraded_and_context_stays_pinned(self):
        self.record("current protocol claim", models=("model-a", "model-b"))
        result = self.report("current protocol claim", section="related_work")
        claim = result["claims"][0]
        self.assertEqual(claim["verdict"], "prior_art_confirmed")
        self.assertEqual(claim["settled"]["confirmed_prior_art"], 1)
        self.assertFalse(claim["verification_required"])
        self.assertEqual(len(claim["evidence"]), 1)
        self.assertTrue(claim["registry"][0]["evidence_returned"])

    def test_same_model_agreement_remains_pending_and_needs_verification(self):
        self.record("current protocol claim", models=("model-a", "model-a"))
        result = self.report("current protocol claim")
        claim = result["claims"][0]
        self.assertEqual(claim["settled"]["agreed_same_model"], 1)
        self.assertEqual(claim["settled"]["pending_quorum"], 1)
        self.assertTrue(claim["verification_required"])
        self.assertIn("1 papers read against current", result["headline"])
        self.assertIn("0 settled", result["headline"])

    def test_negative_partial_and_contested_records_are_not_reported_as_no_match(self):
        cases = [("negative current claim", "does_not_assert", (None, None), "candidates_read_pending_quorum"),
                 ("partial current claim", "partial", (None,), "candidates_read_pending_quorum"),
                 ("settled negative claim", "does_not_assert", ("model-a", "model-b"), "candidates_ruled_out")]
        for text, verdict, models, expected in cases:
            self.record(text, models=models, verdict=verdict)
            with self.subTest(verdict=verdict, expected=expected):
                claim = self.report(text)["claims"][0]
                self.assertEqual(claim["verdict"], expected)
                self.assertEqual(claim["verification_required"], expected != "candidates_ruled_out")
        self.record("contested current claim", models=(None,))
        conn = main._judgments_conn()
        try:
            conn.execute("INSERT INTO claim_judgments "
                         "(claim_norm, claim_text, claim_id, paper_id, verdict, judged_by, judged_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (main._norm_claim("contested current claim"), "contested current claim",
                          main._norm_claim("contested current claim"), "paper-a", "does_not_assert", "wallet:other", "2026-10-02"))
            conn.commit()
        finally:
            conn.close()
        claim = self.report("contested current claim")["claims"][0]
        self.assertEqual(claim["verdict"], "registry_readings_contested")
        self.assertEqual(claim["settled"]["contested"], 1)
        self.assertTrue(claim["verification_required"])

    def test_negative_similar_record_stays_separate_and_requires_claim_verification(self):
        self.record("old protocol claim", verdict="does_not_assert", models=("model-a", "model-b"))
        claim = self.report("new protocol claim")["claims"][0]
        self.assertEqual(claim["verdict"], "readings_reported_on_similar_claim")
        self.assertEqual(claim["settled"]["ruled_out"], 0)
        self.assertTrue(claim["verification_required"])

    def test_report_truncation_uses_chunk_hits_and_as_of_is_observational(self):
        result = self.report("zero knowledge protocol", cap=True)
        claim = result["claims"][0]
        self.assertEqual(claim["corpus_coverage"]["papers_about_this_claim"], 1)
        self.assertTrue(claim["corpus_coverage"]["is_lower_bound"])
        self.assertEqual(claim["corpus_coverage"]["probes"][0]["by_layer"]["web3"]["chunk_hits"],
                         main.FAST_COVERAGE_PROBE_LIMIT)
        self.assertEqual(result["corpus"]["as_of_kind"], "observed_timestamp")
        self.assertIn("not an immutable", result["corpus"]["note"])
        self.assertIn("not unique papers", claim["corpus_coverage"]["how_counted"])


class TrendTests(unittest.TestCase):
    def setUp(self):
        main.TRENDS_CACHE.data.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main, "auth_and_limit"))
        self.stack.enter_context(patch.object(main, "embed_query", return_value=[1.0]))
        self.stack.enter_context(patch.object(main, "_twins", return_value={"canonical": {}}))
        self.stack.enter_context(patch.object(main.requests, "get", side_effect=AssertionError("offline")))
        self.stack.enter_context(patch.object(main.requests, "post", side_effect=AssertionError("offline")))

    def test_zero_knowledge_dense_uses_existing_web3_band_and_bounded_queries(self):
        with patch.object(main, "qdrant_search", return_value=[chunk("zk-paper", 0.8)]) as dense, \
             patch.object(main, "_paper_bm25", return_value=[]) as lexical, \
             patch.object(main, "_load_term_years", return_value=({"proof system": {2024: 1}}, {2024: 1})) as years:
            result = main.trends(about="zero knowledge", layer="web3", year_from=2024, year_to=2026)
        self.assertEqual(result["years"], ["2024"])
        self.assertEqual(years.call_args.args[-1], ["zk-paper"])
        self.assertEqual(result["topic_coverage"]["thresholds_by_layer"]["web3"], main._bands_for("web3")[1])
        self.assertEqual(dense.call_count, 1)
        self.assertLessEqual(dense.call_args.args[2], 200)
        self.assertEqual(dense.call_args.args[1]["must"][1]["range"], {"gte": 2024, "lte": 2026})
        self.assertEqual(lexical.call_count, 1)
        self.assertEqual(lexical.call_args.kwargs["limit"], main.PAPER_BM25_CANDIDATES)

    def test_lexical_is_real_topic_match_not_an_or_term_false_positive(self):
        rows = {"zk-lex": {"title": "Zero-Knowledge Proofs", "layers": ["web3"]},
                "noise": {"title": "Knowledge graphs", "layers": ["web3"]}}
        with patch.object(main, "qdrant_search", return_value=[chunk("weak", 0.71)]), \
             patch.object(main, "_paper_bm25", return_value=list(rows)), \
             patch.object(main, "_paper_rows", return_value=rows), \
             patch.object(main, "_load_term_years", return_value=({}, {2025: 1})) as years:
            result = main.trends(about="zero knowledge", layer="web3")
        self.assertEqual(years.call_args.args[-1], ["zk-lex"])
        self.assertEqual(result["topic_coverage"]["by_layer"]["web3"]["lexical"], 1)
        self.assertEqual(result["status"], "bounded_sample")

    def test_empty_retrieval_rejected_candidates_and_missing_metadata_are_distinct(self):
        cases = [([], [], "no_candidates"), ([chunk("weak", 0.71)], [], "insufficient_coverage"),
                 ([chunk("valid", 0.8)], [], "insufficient_coverage")]
        for hits, lexical, status in cases:
            with self.subTest(status=status, hits=hits), \
                 patch.object(main, "qdrant_search", return_value=hits), \
                 patch.object(main, "_paper_bm25", return_value=lexical), \
                 patch.object(main, "_load_term_years", return_value=({}, {})):
                result = main.trends(about="zero knowledge", layer="web3")
            self.assertEqual(result["status"], status)
            self.assertIn("does not establish absence", result["note"])

    def test_dense_failure_preserves_lexical_sample_but_marks_incomplete_coverage(self):
        with patch.object(main, "qdrant_search", side_effect=main.HTTPException(503, "unavailable")), \
             patch.object(main, "_paper_bm25", return_value=["lexical"]), \
             patch.object(main, "_paper_rows", return_value={"lexical": {"title": "Zero knowledge"}}), \
             patch.object(main, "_load_term_years", return_value=({}, {2024: 1})):
            result = main.trends(about="zero knowledge", layer="web3")
        self.assertEqual(result["status"], "insufficient_coverage")
        self.assertTrue(result["topic_coverage"]["dense_unavailable"])
        self.assertEqual(result["years"], ["2024"])

    def test_layer_threshold_is_not_lowered_for_llm_and_caps_are_reported(self):
        details = {}
        with patch.object(main, "qdrant_search", return_value=[chunk("weak", 0.83, "llm-slm")] * 200), \
             patch.object(main, "_paper_bm25", return_value=[]):
            ids = main._papers_about("language models", "llm-slm", details=details)
        self.assertEqual(ids, [])
        self.assertEqual(details["status"], "insufficient_coverage")
        self.assertTrue(details["is_lower_bound"])

    def test_dense_and_lexical_twins_count_once_with_overlapping_channels(self):
        details = {}
        with patch.object(main, "qdrant_search", return_value=[chunk("paper-a")]), \
             patch.object(main, "_paper_bm25", return_value=["copy-a"]), \
             patch.object(main, "_paper_rows", return_value={"copy-a": {"title": "Zero knowledge"}}), \
             patch.object(main, "_twins", return_value={"canonical": {"copy-a": "paper-a"}}):
            ids = main._papers_about("zero knowledge", "web3", details=details)
        self.assertEqual(ids, ["paper-a"])
        self.assertEqual(details["by_layer"]["web3"], {"dense": 1, "lexical": 1, "canonical_papers": 1})


class RuntimePathTests(unittest.TestCase):
    def test_state_db_overrides_and_legacy_default(self):
        for values, expected in (({}, "/opt/dtox-research/state.db"),
                                 ({"DTOX_DATA_DIR": "data"}, os.path.join("data", "state.db")),
                                 ({"DTOX_DATA_DIR": "data", "STATE_DB_PATH": "custom.db"}, "custom.db")):
            with self.subTest(values=values), patch.object(main.os, "getenv", side_effect=values.get):
                self.assertEqual(main._state_db_path(), expected)

    def test_key_path_aliases_and_legacy_name_without_opening_files(self):
        cases = [({}, "/opt/dtox-research-api/keys.json"), ({"RESEARCH_KEYS_PATH": "legacy.json"}, "legacy.json"),
                 ({"KEYS_PATH": "keys.json", "RESEARCH_KEYS_PATH": "legacy.json"}, "keys.json"),
                 ({"API_KEYS_PATH": "api.json", "KEYS_PATH": "keys.json"}, "api.json")]
        for values, expected in cases:
            with self.subTest(values=values), patch.object(main.os, "getenv", side_effect=values.get):
                self.assertEqual(main._api_keys_path(), Path(expected))


if __name__ == "__main__":
    unittest.main()
