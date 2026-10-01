import sqlite3
import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from fastapi import HTTPException

from api import main
from api.evidence import ABSTRACT_SOURCES, FULLTEXT_SOURCES, completeness, provenance


class EvidenceIntegrityTests(unittest.TestCase):
    def test_actual_acquisition_not_prefix(self):
        for source in ABSTRACT_SOURCES:
            self.assertEqual(completeness(source), (False, "abstract_only"))
        for source in FULLTEXT_SOURCES:
            self.assertEqual(completeness(source), (True, "fulltext_acquired"))
        self.assertIsNone(completeness("new-source")[0])
        results = [{"arxiv_id": "oa:1", "text": "abstract"},
                   {"arxiv_id": "iacr:2", "text": "body"}]
        rows = {"oa:1": {"fulltext_source": "openalex-abstract"},
                "iacr:2": {"fulltext_source": "oa-mirror-pdf", "source_url": "https://example.test/p.pdf"}}
        with patch.object(main, "_paper_rows", return_value=rows) as fetch:
            out = main._hydrate_evidence(results)
        fetch.assert_called_once_with(["oa:1", "iacr:2"])
        self.assertFalse(out[0]["fulltext"])
        self.assertTrue(out[1]["fulltext"])
        self.assertEqual(out[1]["evidence"]["extraction_type"], "pdf_text")
        self.assertFalse(out[1]["evidence"]["exact_latex"])
        self.assertNotIn("fulltext", results[0])

    def test_fragment_hash_and_unknown_status(self):
        fragment = {"text": "x=1", "section_title": "Method"}
        a = provenance("acl:x", fragment)
        self.assertEqual(a, provenance("acl:x", dict(fragment)))
        self.assertNotEqual(a["fragment_hash"], provenance("acl:x", {"text": "x=2"})["fragment_hash"])
        self.assertEqual(a["completeness"], "metadata_missing")
        self.assertIsNone(a["fulltext"])

    def test_github_legacy_latex_acquisition_is_markdown(self):
        metadata = {"fulltext_source": "latex"}
        gh = provenance("gh:project:readme", {"text": "# Method"}, metadata)
        tex = provenance("2401.00001", {"text": "x=1"}, metadata)
        self.assertEqual(gh["extraction_type"], "markdown_source")
        self.assertFalse(gh["exact_latex"])
        self.assertTrue(tex["exact_latex"])
        self.assertEqual(tex["extraction_type"], "latex_source")

    def test_pmlr_hal_actual_acquisition_and_pdf_parse_unknown(self):
        for pid in ("pmlr:v1/paper", "hal:123"):
            for acquisition, expected in (("openalex-oa-pdf", True), ("openalex-abstract", False)):
                item = provenance(pid, {"text": "body"}, {"fulltext_source": acquisition})
                self.assertIs(item["fulltext"], expected)
                self.assertFalse(item["exact_latex"])
                self.assertEqual(item["parse_completeness"], "unknown")

    def test_search_hash_covers_full_indexed_chunk_not_snippet(self):
        payload = {"arxiv_id": "1", "text": "x" * 700, "section_title": "Method"}
        result = main._result_from_hit({"score": 0.9, "payload": payload})
        hydrated = main._hydrate_evidence([result], {"1": {"fulltext_source": "latex"}})[0]
        evidence = hydrated["evidence"]
        self.assertEqual(len(hydrated["text"]), 500)
        self.assertEqual(evidence["fragment_chars"], 700)
        self.assertEqual(evidence["returned_chars"], 500)
        self.assertEqual(evidence["fragment_scope"], "indexed_chunk")
        self.assertEqual(evidence["fragment_hash"], provenance("1", payload)["fragment_hash"])

    def test_acl_and_license_labels(self):
        self.assertEqual(main.source_of("acl:2024.acl-long.1"), "acl")
        self.assertEqual(main.source_url("acl:2024.acl-long.1"), "https://aclanthology.org/2024.acl-long.1/")
        self.assertIn("ACL", main.license_of("acl:x")["terms"])
        self.assertNotIn("Only title", main.license_of("iacr:x")["terms"])

    def test_paper_rows_batched_and_old_tuple_compatible(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [
            ("1", "t", 2024, None, 2, 0, "web3", "a", "abstract-fallback", "https://example.test"),
            ("2", "t2", 2025, None, 0, 0, "llm-slm", "a2")]
        with patch.object(main, "_ro_conn", return_value=conn):
            rows = main._paper_rows(["1", "2", "1"])
        conn.execute.assert_called_once()
        self.assertEqual(rows["1"]["fulltext_source"], "abstract-fallback")
        self.assertIsNone(rows["2"]["fulltext_source"])

    def test_legacy_state_schema_preserves_rows_via_pragma(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE papers (arxiv_id TEXT, title TEXT, year INTEGER, venue TEXT,"
                           "citation_count INTEGER, influential INTEGER, layers TEXT, abstract TEXT, status TEXT);"
                           "INSERT INTO papers VALUES ('1','title',2024,NULL,1,0,'web3','a','done');")
        with patch.object(main, "_ro_conn", return_value=conn), patch.object(main, "_ro_conn_drop"):
            rows = main._paper_rows(["1"])
        self.assertEqual(rows["1"]["title"], "title")
        self.assertIsNone(rows["1"]["fulltext_source"])
        self.assertIsNone(rows["1"]["source_url"])

    def test_audit_rejects_scored_partial_search(self):
        with self.assertRaises(HTTPException) as error:
            main._scored_results_for_audit({"partial": True, "results": [{"score": 0.99}]})
        self.assertEqual(error.exception.status_code, 503)

    def test_hier_partial_survives_mix_and_rrf(self):
        hit = {"payload": {"arxiv_id": "1", "text": "body"}, "score": 0.9}
        for fusion in ("mix", "rrf"):
            with (patch.object(main, "two_phase_dense_search", return_value=([hit], [], [], ["web3"])),
                  patch.object(main, "_coarse_paper_search", return_value=[{"arxiv_id": "1", "paper_score": 0.9}]),
                  patch.object(main, "_hier_stage_b_hits", return_value=[hit]),
                  patch.object(main, "PAPER_LEXICAL", False), patch.object(main, "HIER_GRAPH", False),
                  patch.object(main, "HIER_FUSION", fusion)):
                out = main._hier_run([0.1], None, [], main.SearchBody(query="q"), 1, 4, 1, None)
            self.assertEqual(out[2], ["web3"])

    def test_coarse_fallback_is_partial_even_if_global_succeeded(self):
        with (patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])),
              patch.object(main, "_coarse_paper_search", side_effect=RuntimeError("coarse unavailable"))):
            out = main._hier_run([0.1], None, [], main.SearchBody(query="q"), 1, 4, 1, None)
        self.assertEqual(out[2], ["coarse_fallback"])
        self.assertIsNone(out[3])

    def test_graph_failure_is_partial_but_disabled_graph_is_not(self):
        hit = {"payload": {"arxiv_id": "1", "text": "body"}, "score": 0.9}
        for enabled in (True, False):
            with (patch.object(main, "two_phase_dense_search", return_value=([hit], [], [], [])),
                  patch.object(main, "_coarse_paper_search", return_value=[{"arxiv_id": "1", "paper_score": 0.9}]),
                  patch.object(main, "_hier_stage_b_hits", return_value=[hit]),
                  patch.object(main, "_ro_conn", side_effect=RuntimeError("boom")),
                  patch.object(main, "PAPER_LEXICAL", False), patch.object(main, "HIER_GRAPH", enabled),
                  patch.object(main, "HIER_FUSION", "rrf")):
                out = main._hier_run([0.1], None, [], main.SearchBody(query="q"), 1, 4, 1, None)
            self.assertEqual(out[2], ["graph"] if enabled else [])

    def test_registry_policy_is_delegated(self):
        helper = Mock(return_value="agreed_same_model")
        module = types.ModuleType("api.registry_policy")
        module.judgment_status = helper
        wallets = ["wallet:new1", "wallet:new2"]
        with patch.dict(sys.modules, {"api.registry_policy": module}):
            self.assertEqual(main._judgment_status({"asserts": 2}, wallets, []), "agreed_same_model")
        helper.assert_called_once_with({"asserts": 2}, readers=wallets, models=[], quorum=main.CONFIRMATION_QUORUM)

    def test_real_registry_policy_cannot_settle_two_new_wallets(self):
        self.assertEqual(main._judgment_status({"asserts": 2}, ["wallet:a", "wallet:b"], []), "read_once")

    def test_manual_links_resolve_source_alias_without_inserts(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE claim_nodes (claim_norm TEXT, claim_id TEXT);"
                           "CREATE TABLE claim_links (from_claim TEXT, to_claim TEXT, linked_at TEXT);"
                           "INSERT INTO claim_nodes VALUES ('known alias','target');"
                           "INSERT INTO claim_links VALUES ('sourcealias','target','2026-10-01');"
                           "INSERT INTO claim_links VALUES ('target','final','2026-10-02');")
        queries = []
        conn.set_trace_callback(queries.append)
        for claim in ("sourcealias", "known alias"):
            self.assertEqual(main._read_claim_id(conn, claim), "final")
            self.assertEqual(main._canonical_claim(conn, claim), ("final", False))
        self.assertTrue(all(q.lstrip().upper().startswith("SELECT") for q in queries))

    def test_partial_reading_cannot_add_model_diversity(self):
        decisive = [{"paper_id": "p", "verdict": "asserts", "reason": "", "judged_by": reader,
                     "judged_by_model": "trusted-model:modelA", "attestation_pda": None} for reader in ("key:a", "key:b")]
        partial = dict(decisive[0], verdict="partial", judged_by="key:c", judged_by_model="trusted-model:modelB")
        for records in (decisive + [partial], decisive + [dict(partial, judged_by_model="unspecified")]):
            conn = Mock()
            conn.execute.return_value.fetchall.return_value = records
            with (patch.object(main, "_judgments_read_conn", side_effect=[conn, None]),
                  patch.object(main, "_read_claim_id", return_value="claim")):
                _, registry = main._registry_for_claim("claim")
            self.assertEqual(registry["p"]["status"], "agreed_same_model")
            self.assertEqual(registry["p"]["distinct_models"], ["modelA"])
            with (patch.object(main, "_judgments_read_conn", return_value=conn),
                  patch.object(main, "_read_claim_id", return_value="claim")):
                prior = main._prior_readings("claim", ["p"])
            self.assertEqual(prior["p"]["status"], "agreed_same_model")
            self.assertEqual(prior["p"]["distinct_models"], ["modelA"])

    def test_legacy_names_and_client_claims_never_become_trusted_models(self):
        for legacy in ("key:Alice", "wallet:Alice", "GPT", "modelA", "unspecified"):
            self.assertEqual(main._trusted_stored_model(legacy), "unspecified")
        for info in ({}, {"name": "Alice"}, {"reader": "GPT"}, {"model": "key:Alice"}):
            _, model = main._reader_identity("fake", info, "self-declared-model")
            self.assertEqual(model, "unspecified")
        _, model = main._reader_identity("fake", {"reader_model": "modelA"}, "forged")
        self.assertEqual(model, "trusted-model:modelA")
        self.assertEqual(main._trusted_stored_model(model), "modelA")
        self.assertEqual(main._judgment_status({"asserts": 2}, models=["key:A", "key:B"]), "read_once")

    def test_legacy_registry_records_have_no_trusted_model_diversity(self):
        for names in (("key:A", "key:B"), ("GPT", "Claude")):
            records = [{"paper_id": "p", "verdict": "asserts", "reason": "", "judged_by": reader,
                        "judged_by_model": model, "attestation_pda": None}
                       for reader, model in zip(("key:a", "key:b"), names)]
            conn = Mock()
            conn.execute.return_value.fetchall.return_value = records
            with (patch.object(main, "_judgments_read_conn", side_effect=[conn, None]),
                  patch.object(main, "_read_claim_id", return_value="claim")):
                _, registry = main._registry_for_claim("claim")
            with (patch.object(main, "_judgments_read_conn", return_value=conn),
                  patch.object(main, "_read_claim_id", return_value="claim")):
                prior = main._prior_readings("claim", ["p"])
            for result in (registry, prior):
                self.assertEqual(result["p"]["status"], "read_once")
                self.assertEqual(result["p"]["distinct_models"], [])

    def test_null_state_url_preserves_payload_origin(self):
        result = main._result_from_hit({"payload": {"arxiv_id": "eip:4337", "url": "https://example.test/spec",
                                                   "text": "# Spec"}, "score": 0.9})
        out = main._hydrate_evidence([result], {"eip:4337": {"fulltext_source": "spec-markdown", "source_url": None}})[0]
        self.assertEqual(out["url"], "https://example.test/spec")
        self.assertEqual(out["evidence"]["source_url"], "https://example.test/spec")
        self.assertTrue(out["fulltext"])
        self.assertIn("PMLR", main.license_of("pmlr:1767")["terms"])

    def test_successful_empty_coarse_search_is_not_backend_partial(self):
        with (patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])),
              patch.object(main, "_coarse_paper_search", return_value=[]),
              patch.object(main, "PAPER_LEXICAL", False)):
            out = main._hier_run([0.1], None, [], main.SearchBody(query="q"), 1, 4, 1, None)
        self.assertEqual(out[2], [])

    def test_metadata_failure_degrades_honestly(self):
        with patch.object(main, "_paper_rows", side_effect=RuntimeError("boom")):
            result = main._hydrate_evidence([{"arxiv_id": "1", "text": "chunk"}])[0]
        self.assertIsNone(result["fulltext"])
        self.assertEqual(result["completeness"], "metadata_missing")

    def test_extraction_endpoints_carry_batched_passports(self):
        rows = {"acl:x": {"fulltext_source": "acl-pdf", "source_url": "https://example.test/x.pdf"}}
        payload = {"text": "1 2 3 4", "section_type": "experiments", "section_title": "Results",
                   "element_type": "table", "title": "Paper"}
        with (patch.object(main, "auth_and_limit"), patch.object(main, "_paper_rows", return_value=rows) as fetch,
              patch.object(main, "qdrant_scroll_by_arxiv", return_value=[{"payload": payload}])):
            spec = main.paper_spec("acl:x")
            self.assertTrue(spec["fulltext"])
            self.assertFalse(spec["sections"][0]["evidence"]["exact_latex"])
            fetch.assert_called_once_with(["acl:x"])
            fetch.reset_mock()
            compared = main.compare("acl:x", "acl:x")
            fetch.assert_called_once_with(["acl:x", "acl:x"])
            self.assertTrue(compared["a"]["fulltext"])
            self.assertIn("fragment_hash", compared["a"]["results_chunks"][0]["evidence"])
            section = main.paper_section("acl:x", title="Results")
            self.assertTrue(section["fulltext"])
            self.assertEqual(section["evidence"]["fragment_scope"], "returned_section_page")
            empty = main.paper_spec("acl:x", target_elements="equation")
            self.assertEqual(empty["sections"], [])
            for key in ("source", "url", "license", "fulltext", "evidence"):
                self.assertIn(key, empty)

    def test_registry_read_path_never_initializes_or_creates_nodes(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE claim_nodes (claim_norm TEXT, claim_id TEXT);"
                           "CREATE TABLE claim_judgments (paper_id TEXT, verdict TEXT, reason TEXT,"
                           "judged_by TEXT, judged_by_model TEXT, claim_id TEXT, claim_norm TEXT);")
        queries = []
        conn.set_trace_callback(queries.append)
        with (patch.object(main, "_judgments_read_conn", side_effect=[conn, None]),
              patch.object(main, "_judgments_conn", side_effect=AssertionError("write connection")),
              patch.object(main, "_canonical_claim", side_effect=AssertionError("node creation"))):
            self.assertEqual(main._registry_for_claim("new claim"), ("new claim", {}))
        self.assertTrue(all(q.lstrip().upper().startswith("SELECT") for q in queries))


class StrictSearchTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"HIER_SEARCH": False, "PAPER_LEXICAL": False,
                            "embed_query": [0.1], "_bm25_candidates": [],
                            "_paper_facts": {}, "_lookup_note": None,
                            "_paper_rows": {}, "_exact_matches": [],
                            "_twins": {"canonical": {}, "members": {}}}.items():
            self.stack.enter_context(patch.object(main, name, value) if isinstance(value, bool)
                                     else patch.object(main, name, return_value=value))
        self.dense = self.stack.enter_context(patch.object(main, "two_phase_dense_search", return_value=([], [], [], [])))

    def test_unknown_term_is_explicit_error_before_retrieval(self):
        with self.assertRaises(HTTPException) as error:
            main._run_search(main.SearchBody(query="q", strict=True, terms=["EIP-1559"]))
        self.assertEqual(error.exception.status_code, 400)
        self.dense.assert_not_called()

    def test_strict_disables_soft_layers_relaxation_and_diagnosis(self):
        with (patch.object(main, "_soft_partner", side_effect=AssertionError("soft layer")),
              patch.object(main, "_auto_relax", side_effect=AssertionError("relaxation"))):
            out = main._run_search(main.SearchBody(query="agent memory", layer="ai-agents", strict=True,
                                                  auto_relax=True, terms=["rollup"]))
        self.assertEqual(out["count"], 0)
        self.assertNotIn("why_empty", out)
        self.assertEqual(self.dense.call_count, 1)

    def test_strict_floor_not_capped_and_named_lookup_respects_layer(self):
        self.dense.return_value = ([{"score": 0.85, "payload": {"arxiv_id": "1", "text": "body"}}], [], [], [])
        with patch.object(main, "_inject_exact", side_effect=lambda hits, q, filters, limit, timeout:
                          (self.assertTrue(any(c["key"] == "layers" for c in filters)) or hits)):
            out = main._run_search(main.SearchBody(query="q", layer="web3", strict=True,
                                                  min_score=0.95, hybrid=False))
        self.assertEqual(out["count"], 0)
        self.assertNotIn("min_score_note", out)

    def test_strict_blocks_unscored_lexical_fallback(self):
        with (patch.object(main, "_bm25_candidates", return_value=["x"]),
              patch.object(main, "_score_specific_papers", return_value=[]),
              patch.object(main, "_lexical_fallback_results", side_effect=AssertionError("fallback"))):
            out = main._run_search(main.SearchBody(query="q", strict=True, diagnose=False))
        self.assertEqual(out["count"], 0)

    def test_strict_exact_id_below_default_floor_but_not_explicit_floor(self):
        fresh = {"arxiv_id": "eip:4337", "title": "Account abstraction", "score": 0.6206,
                 "layers": ["web3"], "section_type": "method", "element_type": "code", "year": 2023}
        with (patch.object(main, "_exact_matches", return_value=[("eip:4337", "exact id")]),
              patch.object(main, "_score_specific_papers", return_value=[fresh]) as score):
            default = main._run_search(main.SearchBody(query="EIP-4337", layer="web3", strict=True,
                                                       hybrid=False, diagnose=False))
            self.assertEqual(default["count"], 1)
            self.assertEqual(default["results"][0]["score"], 0.6206)
            self.assertEqual(default["results"][0]["relevance_kind"], "identifier")
            self.assertIn({"key": "layers", "match": {"any": ["web3"]}},
                          score.call_args.kwargs["constraints"])
            explicit = main._run_search(main.SearchBody(query="EIP-4337", layer="web3", strict=True,
                                                        hybrid=False, diagnose=False, min_score=0.9))
            self.assertEqual(explicit["count"], 0)

    def test_strict_and_ordinary_caches_do_not_collide(self):
        main._run_search(main.SearchBody(query="q", strict=False, diagnose=False, hybrid=False))
        main._run_search(main.SearchBody(query="q", strict=True, diagnose=False, hybrid=False))
        self.assertEqual(self.dense.call_count, 2)


if __name__ == "__main__":
    unittest.main()
