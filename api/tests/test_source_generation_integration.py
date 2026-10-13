import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from api import main, source_generation as engine, source_representations as catalog


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def source(sid, *, revision=True, layer="web3", year=2020):
    return {"source_id": sid, "title": sid, "year": year, "layers": [layer],
            "status": "done", "publication_revision": sha(sid) if revision else None,
            "source_url": None}


def representation(rid, target, *, owner=None, text="needle mechanism", **facets):
    owner = owner or target
    foreign = owner != target
    return {"representation_id": rid, "source_id": target, "origin_id": owner,
            "origin_revision": sha(owner + "-artifact"), "text": text,
            "kind": "citation_context" if foreign else "own_passage",
            "locator": {"start": 0, "end": len(text), "unit": "unicode_codepoint"},
            "binding": {"identifier": target, "exact_key": "ref"} if foreign else None,
            "binding_status": "resolved", **facets}


def original(sid, *, score=0.1, text="Original own method", **fields):
    return {"arxiv_id": sid, "score": score, "text": text, "title": sid,
            "layers": ["web3"], "section_type": "method", "element_type": "prose",
            "publication_revision": sha(sid), **fields}


class SourceGenerationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.manifest = self.root / "active.json"
        self.db = self.root / "state.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,title TEXT,year INTEGER,layers TEXT,status TEXT,"
                         "venue TEXT,citation_count INTEGER,influential INTEGER,abstract TEXT,fulltext_source TEXT,"
                         "source_url TEXT,parser_mode TEXT,parse_status TEXT,matched_terms TEXT)")
            conn.execute("CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT,kind TEXT,state TEXT)")
        main.search_cache.data.clear()
        main.source_health_cache.data.clear()
        main.source_embedding_cache.data.clear()
        self.health_check = main._source_health
        self.dense_query = main._source_dense
        self.source_embed_callback = main._source_embed
        self.specific_papers = main._score_specific_papers
        self.stack.enter_context(patch.object(main, "grouped_hits", return_value=[]))
        self.stack.enter_context(patch.object(main, "_source_health", side_effect=lambda manifest, timeout: {
            "status": "ok", "model": manifest["model_id"], "embedding": manifest["embedding"]}))
        self.stack.enter_context(patch.object(main, "SOURCE_GENERATION_MANIFEST", str(self.manifest)))
        self.stack.enter_context(patch.object(main, "STATE_DB_PATH", str(self.db)))
        self.source_embed = self.stack.enter_context(patch.object(main, "_source_embed"))
        self.dense = self.stack.enter_context(patch.object(main, "_source_dense", return_value=[]))
        self.small_embed = self.stack.enter_context(patch.object(main, "embed_query", return_value=[0.1] * 384))
        self.own = self.stack.enter_context(patch.object(main, "_score_specific_papers", return_value=[]))
        self.named = self.stack.enter_context(patch.object(main, "_exact_matches", return_value=[]))
        self.stack.enter_context(patch.object(main, "_paper_facts", return_value={}))
        self.stack.enter_context(patch.object(main, "_evidence_rows", return_value={}))
        self.old = self.stack.enter_context(patch.object(main, "_hier_run", side_effect=AssertionError("old hierarchy")))
        self.stack.enter_context(patch.object(main, "_coarse_paper_search", side_effect=AssertionError("old coarse")))
        self.stack.enter_context(patch.object(main, "_graph_channel", side_effect=AssertionError("old graph")))
        self.stack.enter_context(patch.object(main, "_bm25_candidates", side_effect=AssertionError("old lexical")))
        self.counter = 0
        self.stack.callback(self.close_state_connection)

    def close_state_connection(self):
        conn = getattr(main._ro_conn_local, "conns", {}).pop(str(self.db), None)
        if conn is not None:
            conn.close()

    def build(self, sources=None, representations=None):
        sources = sources or [source("target"), source("owner")]
        representations = representations or [representation("foreign", "target", owner="owner")]
        self.counter += 1
        catalog_path = self.root / f"catalog-{self.counter}.sqlite"
        built = catalog.build_catalog(catalog_path, sources, representations, coverage={
            "version": 1, "scope": "full_snapshot", "partial": False,
            "input_sha256": sha("fixture packets"), "export_report_sha256": sha("fixture report")})
        generation = built["generation"]
        manifest = {"version": engine.MANIFEST_VERSION, "generation": generation,
                    "catalog_path": catalog_path.name, "collection": engine.collection_name(generation),
                    "model_id": engine.MODEL_ID, "dimension": engine.DIMENSION, "complete": True,
                    "embedding": {"model_sha256": sha("model"), "tokenizer_sha256": sha("tokenizer"),
                                  "query_instruction": engine.QUERY_INSTRUCTION,
                                  "max_length": engine.MAX_LENGTH, "pooling": "CLS/L2", "dimension": engine.DIMENSION},
                    "counts": {"sources": len(sources), "representations": len(representations),
                               "points": len(representations)}}
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.source_embed.return_value = {"vector": [0.1] * engine.DIMENSION, "embedding": manifest["embedding"]}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM papers")
            conn.execute("DELETE FROM paper_publications")
            for row in sources:
                conn.execute("INSERT INTO papers(arxiv_id,title,year,layers,status) VALUES (?,?,?,?,?)", (
                    row["source_id"], row["title"], row["year"], ",".join(row["layers"]), row["status"]))
                if row["publication_revision"] is not None:
                    conn.execute("INSERT INTO paper_publications VALUES (?,?,'snapshot','published')",
                                 (row["source_id"], row["publication_revision"]))
        return manifest

    def run_search(self, **fields):
        defaults = dict(query="needle", layer="web3", auto_relax=False, diagnose=False)
        defaults.update(fields)
        return main._run_search(main.SearchBody(**defaults))

    def run_validate(self, read_ids=(), judgment=None, depth="fast"):
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "JUDGMENTS_DB_PATH", str(self.root / "judgments.sqlite")))
            if judgment:
                with closing(main._judgments_conn()) as conn, conn:
                    claim_id, _ = main._canonical_claim(conn, "needle mechanism")
                    for index in range(2):
                        conn.execute("INSERT INTO claim_judgments "
                                     "(claim_norm,claim_text,claim_id,paper_id,verdict,judged_by,judged_by_model,judged_at) "
                                     "VALUES (?,?,?,?,?,?,?,?)", (
                                         main._norm_claim("needle mechanism"), "needle mechanism", claim_id,
                                         "paper0", judgment, f"wallet:{index}", f"trusted-model:model{index}", "2026-10-07"))
            for name, value in (("auth_and_limit", ("test", {})), ("_corpus_stats", {"papers_indexed": 100}),
                                ("trends", {}),
                                ("_citation_neighbours", {}), ("_graph_coverage", {}),
                                ("_needs_rerank", False), ("_audit_named_source_ids", [])):
                stack.enter_context(patch.object(main, name, return_value=value))
            stack.enter_context(patch.object(main, "prime_query_embeddings"))
            stack.enter_context(patch.object(main, "prime_qdrant_searches"))
            stack.enter_context(patch.object(main, "qdrant_scroll_by_arxiv", return_value=[]))
            stack.enter_context(patch.object(main, "paper_spec", side_effect=HTTPException(status_code=404)))
            stack.enter_context(patch.object(main, "_hydrate_evidence", side_effect=lambda rows, *args, **kwargs: rows))
            stack.enter_context(patch.object(main, "_hydrate_hits", side_effect=lambda hits, timeout: hits))
            hits = [{"id": sid, "score": 0.9, "payload": original(
                sid, score=0.9, section_type="related_work" if sid.startswith("scope") else "method")}
                for sid in read_ids]
            stack.enter_context(patch.object(main, "grouped_hits", side_effect=lambda *args, **kwargs:
                                             hits if kwargs["group_size"] == 8 else []))
            return main.validate_project(main.ValidateBody(
                idea="needle protocol mechanism", claims=["needle mechanism"], layer="web3", depth=depth))

    def test_active_replaces_legacy_and_low_original_cosine_does_not_hide_source(self):
        self.build()
        self.own.return_value = [original("target")]
        payload = self.run_search()
        self.assertFalse(payload.get("partial"), payload)
        row = payload["results"][0]
        self.assertEqual(row["arxiv_id"], "target")
        self.assertEqual(row["score"], 0.1)
        self.assertGreater(row["retrieval_score"], 0)
        self.assertNotEqual(row["retrieval_score"], row["score"])
        self.assertEqual(row["text"], "Original own method")
        self.old.assert_not_called()

    def test_foreign_description_never_becomes_original_evidence(self):
        self.build()
        payload = self.run_search()
        row = payload["results"][0]
        self.assertEqual(row["score"], None)
        self.assertEqual(row["text"], "")
        self.assertIsNone(row["fulltext"])
        self.assertEqual(row["evidence_state"], "not_read")
        self.assertEqual(row["retrieval_role"], "discovery_only")
        self.assertEqual(row["source_representations"][0]["origin_source_id"], "owner")
        self.assertEqual(row["evidence"]["returned_chars"], 0)
        self.assertIsNone(main._corroborated({**row, "citation_count": 999999}))
        self.assertEqual(main._evidence_band({**row, "from_registry": True}), "weak")
        self.assertEqual(main._score_band(None), "weak")
        with self.assertRaises(HTTPException) as error:
            main._scored_results_for_audit(payload)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(payload["retrieval_summary"]["observed_candidate_count"], 1)
        self.assertEqual(payload["retrieval_summary"]["discovery_only_count"], 1)

    def test_strict_facets_and_unknown_terms(self):
        self.build(representations=[representation("foreign", "target", owner="owner"),
                   representation("own", "target", text="unrelated own passage", section_type="method",
                                  element_type="prose", terms=["kv cache"])])
        with patch.object(main, "_resolve_terms", return_value=(["kv cache"], [])):
            found = self.run_search(strict=True, section_type="method", element_type="prose", terms=["kv cache"])
            self.assertEqual(found["count"], 1)
            missing = self.run_search(strict=True, section_type="experiments", terms=["kv cache"])
            self.assertEqual(missing["count"], 0)
        with patch.object(main, "_resolve_terms", return_value=([], ["unknown"])):
            with self.assertRaises(HTTPException) as error:
                self.run_search(strict=True, terms=["unknown"])
            self.assertEqual(error.exception.status_code, 400)

    def test_compact_keeps_discovery_provenance_without_mutating_full_cache(self):
        self.build()
        full = self.run_search()
        before = copy.deepcopy(full)
        compact = self.run_search(compact=True)
        self.assertEqual(compact["results"][0]["evidence_state"], "not_read")
        self.assertIn("source_representations", compact["results"][0])
        self.assertIn("retrieval_score", compact["results"][0])
        self.assertNotIn("license", compact["results"][0])
        self.assertEqual(full, before)

    def test_exact_lookup_is_retained_without_old_candidate_votes(self):
        self.build([source("target"), source("owner"), source("eip:7702")])
        self.named.return_value = [("eip:7702", "exact id")]
        self.own.side_effect = [[], [original("eip:7702", score=0.2)]]
        payload = self.run_search(query="EIP-7702 needle", limit=1, strict=True)
        self.assertEqual(payload["results"][0]["arxiv_id"], "eip:7702")
        self.assertEqual(payload["results"][0]["found_via"], "exact id")
        self.assertIsNone(payload["results"][0]["retrieval_score"])
        self.assertIn("_deadline", self.own.call_args.kwargs)

    def test_exact_original_target_is_live_checked_even_outside_discovery_pool(self):
        self.build([source("target"), source("owner"), source("eip:7702")])
        self.named.return_value = [("eip:7702", "exact id")]
        self.own.side_effect = [[], [original("eip:7702", score=0.9)]]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE papers SET status='pending' WHERE arxiv_id='eip:7702'")
        payload = self.run_search(query="EIP-7702 needle")
        self.assertNotIn("eip:7702", [row["arxiv_id"] for row in payload["results"]])

    def test_requested_original_score_cannot_hide_discovered_source_identity(self):
        self.build()
        self.own.return_value = [original("target", score=0.1)]
        payload = self.run_search(min_score=0.6, strict=True)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["source_candidates"][0]["arxiv_id"], "target")
        self.assertEqual(payload["retrieval_summary"]["observed_candidate_count"], 1)
        with self.assertRaises(HTTPException):
            main._scored_results_for_audit(payload)

    def test_nonstrict_min_score_keeps_calibrated_original_cosine_policy(self):
        self.build()
        self.own.return_value = [original("target", score=0.74)]
        payload = self.run_search(min_score=0.99)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["min_score_note"]["applied"], 0.73)
        self.assertTrue(payload["results"][0]["below_min_score"])
        self.assertEqual(main._scored_results_for_audit(payload)[0]["score"], 0.74)
        strict = self.run_search(min_score=0.99, strict=True)
        self.assertEqual(strict["count"], 0)
        self.assertNotIn("min_score_note", strict)
        self.assertEqual(strict["source_candidates"][0]["arxiv_id"], "target")

    def test_metadata_layer_year_filters_match_current_engine_and_original_evidence(self):
        self.build([source("target"), source("other", year=2024), source("outside", layer="llm-slm")],
                   [representation(sid, sid) for sid in ("target", "other", "outside")])
        self.own.return_value = [original("target")]
        payload = self.run_search(strict=True, year_from=2019, year_to=2021)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["target"])
        expected = main._source_chunk_must({"layer": "web3", "year_from": 2019, "year_to": 2021})
        self.assertEqual(self.own.call_args.kwargs["constraints"], expected)
        self.assertEqual(payload["filters_applied"]["year_to"], 2021)

    def test_generation_metadata_authority_survives_original_and_live_database_drift(self):
        self.build()
        self.own.return_value = [original("target", title="old point title", year=1990, layers=["llm-slm"])]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE papers SET title='live changed title',year=2030,layers='llm-slm' WHERE arxiv_id='target'")
        with patch.object(main, "_evidence_rows", side_effect=main._paper_rows):
            payload = self.run_search(strict=True, year_from=2019, year_to=2021)
            compact = self.run_search(strict=True, year_from=2019, year_to=2021, compact=True)
        self.assertFalse(payload.get("partial"), payload)
        for result in (payload, compact):
            row = result["results"][0]
            self.assertEqual((row["title"], row["year"], row["layers"]), ("target", 2020, ["web3"]))
            self.assertEqual(row["metadata_authority"], "frozen_source_catalog")
            self.assertEqual(result["source_generation"]["metadata_authority"], "frozen_source_catalog")
        self.assertEqual(self.run_search(strict=True, year_from=2030)["count"], 0)

    def test_none_original_score_never_enters_cross_score_audit_comparisons(self):
        self.build([source("target"), source("unread")],
                   [representation(sid, sid) for sid in ("target", "unread")])
        self.own.return_value = [original("target", score=0.8), original("unread", score=None)]
        payload = self.run_search()
        for row in payload["results"]:
            self.assertIn(main._evidence_band(row), ("weak", "contextual", "direct", "supporting"))
        with self.assertRaises(HTTPException) as error:
            main._scored_results_for_audit(payload)
        self.assertEqual(error.exception.status_code, 503)
        finite = {**payload, "results": [payload["results"][0]],
                  "retrieval_summary": {"discovery_only_count": 0}}
        self.assertEqual([row["score"] for row in main._scored_results_for_audit(finite)], [0.8])

    def test_validate_keeps_readable_proofs_when_an_observed_source_is_unread(self):
        self.build([source(f"paper{i}") for i in range(9)],
                   [representation(f"rep{i}", f"paper{i}") for i in range(9)])
        with patch.object(main, "_topic_coverage", side_effect=AssertionError("legacy scope veto")):
            report = self.run_validate([f"paper{i}" for i in range(8)])
        claim = report["claims"][0]
        self.assertTrue(claim["incomplete_evidence"])
        self.assertEqual(claim["unread_count"], 1)
        self.assertEqual(claim["verdict"], "strong_candidates")
        self.assertTrue(claim["evidence"])
        self.assertTrue(all(row["score"] == 0.9 for row in claim["evidence"]))
        self.assertEqual(claim["source_candidates"][0]["id"], "paper8")
        self.assertFalse(claim["absence_verdict_allowed"])
        self.assertFalse(report["absence_verdict_allowed"])
        self.assertEqual(report["unread_count"], 1)
        self.assertEqual(claim["corpus_coverage"]["papers_about_this_claim"], 8)
        self.assertTrue(claim["corpus_coverage"]["is_lower_bound"])
        self.assertEqual(report["scope_generation"]["metric"], "qualified_original_sources_from_source_generation")

    def test_validate_all_unread_is_unknown_not_absence_or_prior_art(self):
        self.build()
        with patch.object(main, "_topic_coverage", side_effect=AssertionError("legacy scope veto")):
            report = self.run_validate()
        claim = report["claims"][0]
        self.assertIsNone(report["in_scope"])
        self.assertTrue(report["scope_unverified"])
        self.assertEqual(report["scope_assessment"]["state"], "unverified")
        self.assertEqual(report["scope_assessment"]["observed_qualified_original_sources"], 0)
        self.assertEqual(claim["verdict"], "incomplete_evidence")
        self.assertEqual(claim["unread_count"], 1)
        self.assertEqual(claim["evidence"], [])
        self.assertIsNone(claim["out_of_scope_signal"])
        self.assertFalse(report["absence_verdict_allowed"])
        self.assertEqual(report["verdict_summary"]["no_match_in_corpus"], 0)
        self.assertEqual(report["verdict_summary"]["prior_art_confirmed"], 0)

    def test_ready_low_scope_keeps_positive_evidence_without_inventing_unread_sources(self):
        self.build([source("paper0")], [representation("paper0", "paper0")])
        with patch.object(main, "_topic_coverage", side_effect=AssertionError("legacy scope veto")):
            report = self.run_validate(["paper0"])
        claim = report["claims"][0]
        self.assertEqual(claim["verdict"], "strong_candidates")
        self.assertEqual(claim["evidence"][0]["id"], "paper0")
        self.assertIsNone(report["in_scope"])
        self.assertEqual(report["scope_assessment"]["observed_qualified_original_sources"], 1)
        self.assertEqual(claim["corpus_coverage"]["papers_about_this_claim"], 1)
        self.assertTrue(claim["incomplete_evidence"])
        self.assertTrue(claim["scope_unverified"])
        self.assertEqual((report["unread_count"], claim["unqualified_count"]), (0, 0))
        self.assertEqual(claim["source_candidates"], [])
        self.assertNotIn("Some discovered sources lack", claim["reading"])
        self.assertFalse(report["absence_verdict_allowed"])
        self.assertIsNone(claim["out_of_scope_signal"])

    def test_ready_low_scope_keeps_confirmed_reading_but_not_negative_absence_verdict(self):
        for judgment, expected in (("asserts", "prior_art_confirmed"), ("does_not_assert", "incomplete_evidence")):
            with self.subTest(judgment=judgment):
                self.build([source("paper0")], [representation("paper0", "paper0")])
                with patch.object(main, "JUDGMENTS_DB_PATH", str(self.root / "judgments.sqlite")):
                    with closing(main._judgments_conn()) as conn, conn:
                        conn.execute("DELETE FROM claim_judgments")
                report = self.run_validate(["paper0"], judgment=judgment)
                claim = report["claims"][0]
                self.assertEqual(claim["verdict"], expected)
                self.assertTrue(claim["evidence"])
                self.assertIsNone(report["in_scope"])
                self.assertEqual(report["unread_count"], 0)
                self.assertFalse(claim["absence_verdict_allowed"])
                self.assertEqual(report["verdict_summary"]["candidates_ruled_out"], 0)
                self.assertEqual(report["verdict_summary"]["no_match_in_corpus"], 0)

    def test_runtime_failure_remains_503_not_epistemic_scope_uncertainty(self):
        self.build([source("paper0")], [representation("paper0", "paper0")])
        with patch.object(main, "_source_health", side_effect=ValueError("runtime mismatch")):
            with self.assertRaises(HTTPException) as error:
                self.run_validate(["paper0"])
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("retrieval unavailable", error.exception.detail)

    def test_validate_keeps_positive_registry_judgment_with_unread_candidates(self):
        ids = ("paper0", "scope0", "scope1", "unread")
        self.build([source(sid) for sid in ids], [representation(sid, sid) for sid in ids])
        report = self.run_validate(ids[:3], judgment="asserts", depth="full")
        claim = report["claims"][0]
        self.assertEqual(claim["verdict"], "prior_art_confirmed")
        self.assertTrue(claim["incomplete_evidence"])
        self.assertTrue(claim["evidence"][0]["from_registry"])
        self.assertEqual(claim["registry"][0]["status"], "confirmed_prior_art")
        self.assertFalse(claim["absence_verdict_allowed"])

    def test_validate_negative_reading_does_not_rule_out_unread_sources(self):
        ids = ("paper0", "scope0", "scope1", "unread")
        self.build([source(sid) for sid in ids], [representation(sid, sid) for sid in ids])
        report = self.run_validate(ids[:3], judgment="does_not_assert")
        claim = report["claims"][0]
        self.assertEqual(claim["registry"][0]["status"], "ruled_out")
        self.assertEqual(claim["verdict"], "incomplete_evidence")
        self.assertFalse(claim["absence_verdict_allowed"])

    def test_validate_refuses_generation_change_between_scope_probes(self):
        ids = ("paper0", "paper1", "paper2")
        self.build([source(sid) for sid in ids], [representation(sid, sid) for sid in ids])
        run = main._run_search
        calls = 0
        def changing(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                sources = [source(sid) for sid in ids]
                sources[0]["title"] = "new metadata generation"
                self.build(sources, [representation(sid, sid) for sid in ids])
            return run(*args, **kwargs)
        with patch.object(main, "_run_search", side_effect=changing):
            with self.assertRaises(HTTPException) as error:
                self.run_validate(ids)
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("generation changed", error.exception.detail)

    def test_original_evidence_transport_failure_preserves_discovery_lead_as_partial(self):
        self.build()
        self.own.side_effect = HTTPException(status_code=502, detail="evidence unavailable")
        payload = self.run_search()
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["results"][0]["arxiv_id"], "target")
        self.assertIsNone(payload["results"][0]["score"])
        self.assertEqual(payload["results"][0]["evidence_state"], "not_read")

    def test_generation_aware_cache_revalidates_manifest_and_visibility(self):
        first = self.build()
        self.run_search()
        self.run_search()
        self.assertEqual(self.own.call_count, 1)
        second = self.build([source("different"), source("owner")],
                            [representation("new-foreign", "different", owner="owner")])
        payload = self.run_search()
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(payload["results"][0]["arxiv_id"], "different")
        self.assertEqual(self.own.call_count, 2)
        second["complete"] = False
        self.manifest.write_text(json.dumps(second), encoding="utf-8")
        invalid = self.run_search()
        self.assertTrue(invalid["partial"])
        self.assertEqual(invalid["results"], [])
        self.assertEqual(self.own.call_count, 2)

    def test_missing_and_invalid_configured_manifest_fail_closed(self):
        missing = self.run_search()
        self.assertTrue(missing["partial"])
        self.assertEqual(missing["count"], 0)
        self.manifest.write_text("{broken", encoding="utf-8")
        invalid = self.run_search()
        self.assertTrue(invalid["partial"])
        self.assertEqual(invalid["count"], 0)
        self.old.assert_not_called()

    def test_dense_failure_is_explicit_partial_and_not_cached(self):
        self.build()
        self.dense.side_effect = TimeoutError("dense unavailable")
        one = self.run_search()
        two = self.run_search()
        self.assertTrue(one["partial"])
        self.assertEqual(one["count"], 1)
        self.assertTrue(two["partial"])
        self.assertEqual(self.own.call_count, 2)
        with self.assertRaises(HTTPException):
            main._scored_results_for_audit(one)

    def test_hidden_foreign_owner_checked_before_final_result_cut(self):
        self.build([source("target"), source("owner"), source("live")],
                   [representation("foreign", "target", owner="owner"), representation("own-live", "live")])
        self.dense.return_value = [{"representation_id": "foreign", "score": 0.99},
                                   {"representation_id": "own-live", "score": 0.5}]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE paper_publications SET state='staged' WHERE arxiv_id='owner'")
        payload = self.run_search(limit=1)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["live"])
        self.assertTrue(payload["partial"])

    def test_cached_lead_cannot_survive_stale_owner_revision(self):
        self.build()
        self.assertEqual(self.run_search()["count"], 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE paper_publications SET revision=? WHERE arxiv_id='owner'", (sha("new"),))
        payload = self.run_search()
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["partial"])

    def test_live_visibility_legacy_none_requires_done_and_no_snapshot_job(self):
        records = [source("legacy", revision=False), source("pending", revision=False),
                   source("published"), source("ambiguous"), source("not-done", revision=False)]
        self.build(records, [representation("legacy-rep", "legacy")])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO paper_publications VALUES ('pending','pending-rev','snapshot','staged')")
            conn.execute("INSERT INTO paper_publications VALUES ('ambiguous','other','snapshot','staged')")
            conn.execute("UPDATE papers SET status='pending' WHERE arxiv_id='not-done'")
        self.assertEqual(main._source_visible(records, 1), {"legacy": None, "published": sha("published")})
        payload = self.run_search()
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["legacy"])

    def test_target_revision_mismatch_cannot_expose_own_text(self):
        self.build()
        self.own.return_value = [original("target", publication_revision=sha("wrong"))]
        payload = self.run_search()
        self.assertEqual(payload["results"][0]["text"], "")
        self.assertIsNone(payload["results"][0]["score"])

    def test_absolute_deadline_bounds_uncooperative_adapter_and_no_followup_steps(self):
        release, started, finished = threading.Event(), threading.Event(), threading.Event()
        phase_start = []
        def phase_now():
            return time.monotonic() - phase_start[0] if phase_start else 0.0
        def slow(*args, **kwargs):
            phase_start.append(time.monotonic())
            started.set()
            try:
                release.wait(0.5)
                return {"status": "ready", "generation": sha("slow"), "sources": [], "partial": False}
            finally:
                finished.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            submit = pool.submit
            def submit_started(*args, **kwargs):
                future = submit(*args, **kwargs)
                self.assertTrue(started.wait(2))
                return future
            with patch.object(main, "_SOURCE_POOL", pool), \
                    patch.object(main, "_SOURCE_SLOTS", threading.BoundedSemaphore(1)), \
                    patch.object(pool, "submit", side_effect=submit_started), \
                    patch.object(main, "time", wraps=time) as phase_time, \
                    patch.object(main, "discover_sources", side_effect=slow) as discovery, \
                    patch.object(main, "_lookup_note") as lookup, \
                    patch.object(main, "_hydrate_search_payload") as hydrate:
                phase_time.monotonic.side_effect = phase_now
                try:
                    payload = main._run_source_search(
                        main.SearchBody(query="needle"), "needle", [], [], None,
                        0.02, 0.005, 0.005, True, False, deadline=0.03)
                    self.assertLess(phase_now(), 0.25)
                    self.assertFalse(finished.is_set())
                    discovery.assert_called_once()
                    lookup.assert_not_called()
                    hydrate.assert_not_called()
                finally:
                    release.set()
                    self.assertTrue(main._SOURCE_SLOTS.acquire(timeout=2))
                    main._SOURCE_SLOTS.release()
        self.assertTrue(payload["partial"])
        self.assertIn("deadline", payload["source_generation"]["diagnostics"]["reasons"])
        self.small_embed.assert_not_called()
        self.own.assert_not_called()
        self.named.assert_not_called()

    def test_repeated_deadlines_cannot_grow_outstanding_source_workers(self):
        release, drained = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, identities = set(), set()
        def slow():
            identity = threading.get_ident()
            with lock:
                active.add(identity)
                identities.add(identity)
            try:
                release.wait(3)
            finally:
                with lock:
                    active.remove(identity)
                    if not active:
                        drained.set()
        try:
            for _ in range(4):
                with self.assertRaises(TimeoutError):
                    main._source_call(time.monotonic() + 0.05, slow)
            self.assertEqual(len(active), 4)
            before = time.monotonic()
            for _ in range(30):
                with self.assertRaises(main._SourceBusy):
                    main._source_call(time.monotonic() + 1, slow)
            self.assertLess(time.monotonic() - before, 0.2)
            self.assertEqual(len(identities), 4)
            with self.assertRaises(HTTPException) as error:
                self.run_search()
            self.assertEqual(error.exception.status_code, 503)
        finally:
            release.set()
            self.assertTrue(drained.wait(2))
            for _ in range(4):
                self.assertTrue(main._SOURCE_SLOTS.acquire(timeout=2))
            for _ in range(4):
                main._SOURCE_SLOTS.release()
        deadline = time.monotonic() + 1
        self.assertEqual(main._source_call(deadline, lambda: "recovered"), "recovered")

    def test_nested_source_calls_do_not_deadlock_a_fully_occupied_pool(self):
        barrier = threading.Barrier(4)
        deadline = time.monotonic() + 2
        def outer():
            barrier.wait(timeout=1)
            return main._source_call(deadline, lambda: threading.get_ident())
        with ThreadPoolExecutor(max_workers=4) as callers:
            results = list(callers.map(lambda _: main._source_call(deadline, outer), range(4)))
        self.assertEqual(len(set(results)), 4)

    def test_hybrid_false_uses_only_dense_source_ranking(self):
        self.build([source("lexical"), source("dense")],
                   [representation("lex", "lexical"), representation("sem", "dense", text="other words")])
        self.dense.return_value = [{"representation_id": "sem", "score": 0.8}]
        payload = self.run_search(hybrid=False)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["dense"])
        self.assertFalse(payload.get("partial"), payload)
        self.assertEqual(payload["results"][0]["channels"], ["dense"])

    def test_dedupe_false_retains_multiple_owned_chunks_only(self):
        self.build()
        hits = [{"id": index, "score": 0.8, "payload": original("target", text=f"own chunk {index}")}
                for index in range(2)]
        with patch.object(main, "grouped_hits", side_effect=lambda *args, **kwargs:
                          hits if kwargs["group_size"] == 8 else []), \
                patch.object(main, "_hydrate_hits", return_value=hits):
            payload = self.run_search(dedupe=False)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([row["text"] for row in payload["results"]], ["own chunk 0", "own chunk 1"])

    def test_active_original_fetch_groups_sources_and_prefers_own_method(self):
        self.build([source("paper0"), source("paper1")],
                   [representation(sid, sid) for sid in ("paper0", "paper1")])
        hits = [{"id": "intro", "score": 0.99, "payload": original("paper0", section_type="related_work")},
                {"id": "method", "score": 0.4, "payload": original("paper0")},
                {"id": "other", "score": 0.1, "payload": original("paper1")}]
        response = MagicMock()
        response.json.return_value = {"result": {"groups": [{"id": "paper0", "hits": hits[:2]},
                                                               {"id": "paper1", "hits": hits[2:]}]}}
        from api.grouped_search import grouped_hits
        with patch.object(main, "grouped_hits", grouped_hits), \
                patch.object(main.requests, "post", return_value=response) as post, \
                patch.object(main, "qdrant_search", side_effect=AssertionError("flat starvation")), \
                patch.object(main, "qdrant_fetch_payloads", return_value={hit["id"]: hit["payload"] for hit in hits}):
            rows = self.specific_papers("needle", ["paper0", "paper1"], vector=[0.1] * 384,
                                       _deadline=time.monotonic() + 2)
        self.assertEqual({row["arxiv_id"] for row in rows}, {"paper0", "paper1"})
        self.assertEqual(rows[0]["score"], 0.4)
        self.assertEqual(post.call_args.kwargs["json"]["group_by"], "arxiv_id")
        self.assertEqual(post.call_args.kwargs["json"]["limit"], 2)
        self.assertEqual(post.call_args.kwargs["json"]["group_size"], 8)

    def test_nondedupe_source_first_round_robin_prevents_evidence_cut_starvation(self):
        self.build([source("paper0"), source("paper1")],
                   [representation(sid, sid) for sid in ("paper0", "paper1")])
        hits = [{"id": index, "score": 0.99, "payload": original("paper0", text=f"chunk {index}")}
                for index in range(8)] + [{"id": 9, "score": 0.1, "payload": original("paper1")}]
        with patch.object(main, "grouped_hits", side_effect=lambda *args, **kwargs:
                          hits if kwargs["group_size"] == 8 else []) as grouped, \
                patch.object(main, "_hydrate_hits", return_value=hits), \
                patch.object(main, "qdrant_search", side_effect=AssertionError("flat starvation")):
            payload = self.run_search(dedupe=False, limit=2)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["paper0", "paper1"])
        self.assertEqual(grouped.call_args.kwargs["limit"], 2)
        self.assertEqual(grouped.call_args.kwargs["group_size"], 8)

    def test_soft_layers_fuse_canonical_sources_before_cut(self):
        self.build([source("agents", layer="ai-agents"), source("llm", layer="llm-slm")],
                   [representation("a", "agents"), representation("b", "llm")])
        with patch.object(main, "SOFT_LAYERS", True):
            payload = self.run_search(layer="ai-agents", query="needle agentic", limit=2)
        self.assertEqual({row["arxiv_id"] for row in payload["results"]}, {"agents", "llm"})
        self.assertEqual(payload["layer_expanded"]["also_searched"], "llm-slm")
        self.assertFalse(payload.get("partial"), payload)

    def test_source_generation_preserves_bounded_diagnostics_and_per_layer_observation_counts(self):
        sources = [source(f"paper{i}", layer="ai-agents") for i in range(engine.MAX_CANDIDATES)]
        for row in sources:
            row["layers"] = ["ai-agents", "llm-slm"]
        reps = [representation(f"rep{i}", row["source_id"], kind="own_summary")
                for i, row in enumerate(sources)]
        self.build(sources, reps)
        self.dense.return_value = [{"representation_id": rep["representation_id"], "score": 0.9} for rep in reps]
        fresh = {"id": "fresh", "score": 0.8, "payload": original("fresh")}
        with patch.object(main, "SOFT_LAYERS", True), \
                patch.object(main, "_source_owned", return_value=[fresh]) as native:
            first = self.run_search(layer="ai-agents", query="needle agentic", limit=1)
            native.return_value = [fresh, fresh]
            cached = self.run_search(layer="ai-agents", query="needle agentic", limit=1)
        for payload, count in ((first, 1), (cached, 2)):
            self.assertFalse(payload.get("partial"), payload)
            diagnostics = payload["source_generation"]["diagnostics"]
            self.assertEqual(diagnostics["reasons"], [])
            self.assertEqual(diagnostics["limits"], ["dense_derived_candidate_limit", "lexical_candidate_limit"])
            self.assertTrue(diagnostics["retrieval_bounded"])
            self.assertFalse(diagnostics["exhaustive"])
            self.assertEqual(diagnostics["native_outside_generation"], count * 2)
            self.assertEqual(diagnostics["counting"], "sum_per_layer_not_unique")
            self.assertEqual([item["layer"] for item in diagnostics["by_layer"]], ["ai-agents", "llm-slm"])
            for item in diagnostics["by_layer"]:
                self.assertEqual(item["limits"], diagnostics["limits"])
                self.assertEqual(item["native_outside_generation"], count)

    def test_soft_layers_use_best_channel_ranks_once_and_merge_bounded_representation_ids(self):
        sources = [source(sid, layer="ai-agents") for sid in ("a", "b")]
        for row in sources:
            row["layers"] = ["ai-agents", "llm-slm"]
        reps = [representation(f"rep{i}", "a") for i in range(10)] + [representation("b", "b")]
        manifest = self.build(sources, reps)
        first = main.discover_sources(str(self.manifest), "needle", embed_query=self.source_embed,
                                      query_dense=self.dense, timeout=2, visible_sources=main._source_visible)
        second = copy.deepcopy(first)
        all_reps = catalog.hydrate_representations(
            self.root / manifest["catalog_path"], [f"rep{i}" for i in range(10)],
            generation=manifest["generation"], timeout=2)["a"]["representations"]
        first["sources"][0].update(channel_ranks={"dense": 5}, representations=all_reps[:8])
        first["sources"][1].update(channel_ranks={"dense": 2, "lexical": 1})
        second["sources"] = [copy.deepcopy(first["sources"][0])]
        second["sources"][0].update(channel_ranks={"dense": 1}, representations=all_reps[2:])
        with patch.object(main, "SOFT_LAYERS", True), \
                patch.object(main, "discover_sources", side_effect=[first, second]):
            payload = self.run_search(layer="ai-agents", query="needle agentic", limit=2)
        self.assertFalse(payload.get("partial"), payload)
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["b", "a"])
        self.assertAlmostEqual(payload["results"][0]["retrieval_score"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(payload["results"][1]["retrieval_score"], 1 / 61)
        merged = payload["results"][1]["source_representations"]
        self.assertEqual(len(merged), catalog.MAX_CONTEXTS)
        self.assertEqual(len({rep["representation_id"] for rep in merged}), catalog.MAX_CONTEXTS)

    def test_auto_relax_and_diagnose_reuse_same_generation_and_deadline(self):
        self.build()
        relaxed = self.run_search(year_from=2025, auto_relax=True)
        self.assertEqual(relaxed["relaxed"]["dropped"], ["year"])
        self.assertFalse(relaxed["results"][0]["matches_filters"])
        diagnosed = self.run_search(year_from=2025, diagnose=True)
        self.assertEqual(diagnosed["why_empty"]["relax_one_of"][0]["drop"], "year_from")

    def test_limit_50_sources_not_silently_capped_at_20(self):
        self.build([source(f"paper{i}") for i in range(55)],
                   [representation(f"rep{i}", f"paper{i}") for i in range(55)])
        payload = self.run_search(limit=50)
        self.assertEqual(payload["count"], 50, payload)

    def test_internal_body_limit_clamps_before_result_and_candidate_cut(self):
        self.build([source(f"paper{i}") for i in range(55)],
                   [representation(f"rep{i}", f"paper{i}") for i in range(55)])
        body = main.SearchBody(query="needle", layer="web3", auto_relax=False, diagnose=False)
        for requested, expected in ((0, 1), (500, 50)):
            with self.subTest(limit=requested):
                copied = body.model_copy(update={"limit": requested})
                self.assertEqual(main._run_search(copied)["count"], expected)
                filtered = main._run_search(copied.model_copy(update={"min_score": 0.9, "strict": True}))
                self.assertEqual(filtered["source_candidate_count"], expected)

    def test_dense_adapter_groups_real_producer_source_field_and_forwards_deadline(self):
        response = MagicMock()
        response.json.return_value = {"result": {"groups": []}}
        with patch.object(main.requests, "post", return_value=response) as post:
            self.assertEqual(self.dense_query([0.1], "derived", {"must": []}, 50, 0.2), {"groups": []})
        self.assertEqual(post.call_args.kwargs["json"]["group_by"], "source_id")
        self.assertEqual(post.call_args.kwargs["timeout"], 0.2)

    def test_native_owned_adapter_uses_original_model_strict_filters_and_whole_text(self):
        self.build()
        whole = "whole original chunk " * 80
        hit = {"id": 1, "score": 0.8, "payload": original("target", text=whole)}
        hidden = {"id": 2, "score": 0.9, "payload": original("target", text="hidden", publication_revision="pending")}
        filters = {"layer": "web3", "section_type": "method", "element_type": "prose",
                   "terms": ["kv cache"], "year_from": 2019, "year_to": 2022}
        vector = MagicMock(return_value=[0.1] * 384)
        with patch.object(main, "grouped_hits", return_value=[hit, hidden]) as grouped:
            rows = main._source_owned("needle", filters, 50, 1, vector)
        self.assertEqual(rows, [hit])
        self.assertEqual(rows[0]["payload"]["text"], whole)
        call = grouped.call_args
        self.assertEqual(call.args[2], main.COLLECTION)
        self.assertEqual(call.args[3], [0.1] * 384)
        self.assertEqual(call.args[4], {"must": main._source_chunk_must(filters)})
        self.assertEqual(call.kwargs["group_size"], 1)
        self.assertLessEqual(call.kwargs["timeout"], 1)

    def test_real_engine_native_whole_chunk_binding_and_vector_reuse(self):
        whole = "precisely indexed own method with original assumptions"
        manifest = self.build([source("target")], [representation(
            "native", "target", text=whole, section_type="method", element_type="prose", terms=["kv cache"])])
        manifest["counts"].update(native_owned_representations=1, new_indexed_representations=0, points=0)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.own.return_value = [original("target", text=whole, terms=["kv cache"])]
        hit = {"id": "native-point", "score": 0.8, "payload": original("target", text=whole)}
        with patch.object(main, "grouped_hits", return_value=[hit]), \
                patch.object(main, "_resolve_terms", return_value=(["kv cache"], [])):
            payload = self.run_search(query="different formulation", strict=True, section_type="method",
                                      element_type="prose", terms=["kv cache"], year_from=2019, year_to=2022)
        self.assertFalse(payload.get("partial"), payload)
        self.assertEqual(payload["count"], 1)
        row = payload["results"][0]
        self.assertEqual(row["channels"], ["dense"])
        self.assertEqual(row["source_representations"][0]["native_provenance"][0]["text_sha256"], sha(whole))
        self.assertEqual(row["score"], 0.1)
        self.assertEqual(self.small_embed.call_count, 1)
        self.assertEqual(self.own.call_args.kwargs["vector"], [0.1] * 384)

    def test_native_wrong_whole_text_is_not_guessed_into_a_source(self):
        self.build([source("target")], [representation("native", "target", text="whole original")])
        hit = {"id": "wrong", "score": 0.99, "payload": original("target", text="different whole original")}
        with patch.object(main, "grouped_hits", return_value=[hit]):
            payload = self.run_search(query="paraphrase")
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["count"], 0)
        self.own.assert_not_called()

    def test_native_legacy_marker_is_explicit_only_after_live_no_job_check(self):
        self.build([source("legacy", revision=False)], [representation("own", "legacy")])
        hit = {"id": "legacy-point", "score": 0.8, "payload": {"arxiv_id": "legacy", "text": "whole"}}
        with patch.object(main, "grouped_hits", return_value=[hit]):
            rows = main._source_owned("query", {}, 8, 1, lambda *args: [0.1] * 384)
        self.assertIn("publication_revision", rows[0]["payload"])
        self.assertIsNone(rows[0]["payload"]["publication_revision"])

    def test_real_engine_native_legacy_missing_revision_normalizes_with_done_metadata(self):
        whole = "exact legacy original method chunk"
        manifest = self.build([source("legacy", revision=False)], [representation("native", "legacy", text=whole)])
        manifest["counts"].update(native_owned_representations=1, new_indexed_representations=0, points=0)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        legacy = original("legacy", text=whole)
        legacy.pop("publication_revision")
        self.own.return_value = [legacy]
        hit = {"id": "legacy-point", "score": 0.8, "payload": legacy}
        with patch.object(main, "grouped_hits", return_value=[hit]):
            payload = self.run_search(query="different formulation", strict=True)
        self.assertFalse(payload.get("partial"), payload)
        row = payload["results"][0]
        self.assertEqual(row["arxiv_id"], "legacy")
        self.assertEqual(row["evidence_state"], "original_passage_retrieved")
        self.assertIsNone(row["source_representations"][0]["origin_publication_revision"])
        self.assertEqual(row["source_representations"][0]["native_provenance"][0]["text_sha256"], sha(whole))

    def test_standalone_uvicorn_import_can_load_new_engine(self):
        completed = subprocess.run([
            sys.executable, "-B", "-c",
            "import main; r=main.discover_sources('missing.json','needle',embed_query=lambda *a: [],"
            "query_dense=lambda *a: [],timeout=.1); assert r['partial']"],
            cwd=Path(main.__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_health_descriptor_mismatch_and_old_health_fail_closed_before_dense(self):
        manifest = self.build()
        response = MagicMock()
        for health in ({"status": "ok", "model": engine.MODEL_ID},
                       {"status": "ok", "model": engine.MODEL_ID,
                        "embedding": {**manifest["embedding"], "model_sha256": sha("wrong")}}):
            with self.subTest(health=health), patch.object(main, "_source_health", self.health_check), \
                    patch.object(main, "COARSE_EMBED_URL", "http://approved.test/embed"), \
                    patch.object(main.requests, "get", return_value=response) as get:
                response.json.return_value = health
                payload = self.run_search()
                self.assertTrue(payload["partial"])
                self.assertEqual(payload["count"], 0)
                self.assertEqual(payload["source_generation"]["status"], "unavailable")
                self.assertLessEqual(get.call_args.kwargs["timeout"], 1)
        self.dense.assert_not_called()
        self.own.assert_not_called()

    def test_health_proof_is_cached_by_generation_and_descriptor_is_returned(self):
        manifest = self.build()
        response = MagicMock()
        response.json.return_value = {"status": "ok", "model": engine.MODEL_ID,
                                      "embedding": manifest["embedding"]}
        with patch.object(main, "_source_health", self.health_check), \
                patch.object(main, "COARSE_EMBED_URL", "http://approved.test/embed"), \
                patch.object(main.requests, "get", return_value=response) as get:
            one = self.run_search()
            self.run_search()
            self.assertEqual(get.call_count, 1)
            self.assertEqual(one["source_generation"]["embedding"], manifest["embedding"])
            self.build([source("next"), source("owner")], [representation("next", "next", owner="owner")])
            self.run_search()
            self.assertEqual(get.call_count, 2)

    def test_actual_vector_descriptor_is_required_even_with_fresh_health_cache(self):
        manifest = self.build()
        health, embedded = MagicMock(), MagicMock()
        health.json.return_value = {"status": "ok", "model": engine.MODEL_ID, "embedding": manifest["embedding"]}
        embedded.json.return_value = {"vector": [0.1] * engine.DIMENSION, "embedding": manifest["embedding"]}
        with patch.object(main, "_source_health", self.health_check), \
                patch.object(main, "_source_embed", self.source_embed_callback), \
                patch.object(main, "COARSE_EMBED_URL", "http://approved.test/embed"), \
                patch.object(main.requests, "get", return_value=health) as get, \
                patch.object(main.requests, "post", return_value=embedded) as post:
            good = self.run_search()
            self.assertFalse(good.get("partial"), good)
            self.assertEqual(len(main.source_embedding_cache.data), 1)
            dense_calls = self.dense.call_count
            for index, descriptor in enumerate((None, {**manifest["embedding"], "model_sha256": sha("changed")})):
                with self.subTest(descriptor=descriptor):
                    embedded.json.return_value = {"vector": [0.2] * engine.DIMENSION, "embedding": descriptor}
                    payload = self.run_search(query=f"needle changed{index}")
                    self.assertTrue(payload["partial"])
                    self.assertEqual(payload["source_generation"]["status"], "unavailable")
                    self.assertEqual(payload["results"], [])
                    self.assertEqual(len(main.source_embedding_cache.data), 1)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(post.call_count, 3)
            self.assertEqual(self.dense.call_count, dense_calls)


if __name__ == "__main__":
    unittest.main()
