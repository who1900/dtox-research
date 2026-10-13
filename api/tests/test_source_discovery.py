import copy
import unittest
from unittest.mock import patch

from api import main, source_discovery


def _hit(paper_id, score, text="substantive section", section_type="method"):
    return {"id": paper_id, "score": score, "payload": {
        "arxiv_id": paper_id, "title": paper_id, "text": text,
        "section_type": section_type, "layers": ["web3"],
        "section_title": "References" if text == "References" else "Methods",
    }}


class SourceDiscoveryHelperTests(unittest.TestCase):
    def test_aliases_dedupe_prefers_content_without_mutating_hits_or_primary(self):
        boilerplate = _hit("alias-a", 0.68, "References", "method")
        content = _hit("alias-b", 0.66)
        primary = [{"arxiv_id": "main"}]
        original_hits = copy.deepcopy([boilerplate, content, _hit("main", 0.99)])

        rows = source_discovery.select_source_candidates(
            [boilerplate, content, _hit("main", 0.99)], primary, 5, 0.7,
            lambda paper_id: "canonical" if paper_id.startswith("alias-") else paper_id,
            main._result_from_hit, main._hit_is_boilerplate)

        self.assertEqual([row["arxiv_id"] for row in rows], ["alias-b"])
        self.assertEqual(rows[0]["candidate_reason"], "fragment_below_relevance_floor")
        self.assertFalse(rows[0]["passes_relevance_floor"])
        self.assertTrue(rows[0]["verification_required"])
        self.assertEqual(rows[0]["candidate_channel"], "fused")
        self.assertEqual([boilerplate, content, _hit("main", 0.99)], original_hits)
        self.assertEqual(primary, [{"arxiv_id": "main"}])

    def test_candidate_cap_is_ten_and_floor_reason_uses_raw_score(self):
        hits = [_hit(f"paper-{i}", 0.70 if i == 0 else 0.69) for i in range(15)]
        rows = source_discovery.select_source_candidates(
            hits, [], 50, 0.70, lambda paper_id: paper_id,
            main._result_from_hit, lambda hit: False)

        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["candidate_reason"], "outside_primary_result_budget")
        self.assertTrue(rows[0]["passes_relevance_floor"])
        self.assertEqual(rows[1]["candidate_reason"], "fragment_below_relevance_floor")

    def test_graph_lane_interleaves_ranked_tail_without_changing_main_order(self):
        hits = [_hit("primary", 0.95), _hit("main-1", 0.68), _hit("main-2", 0.67),
                _hit("graph-tail", 0.55), _hit("graph-two", 0.54)]
        ranks = {"graph-tail": {"graph": 2}, "graph-two": {"graph": 5}}
        rows = source_discovery.select_source_candidates(
            hits, [{"arxiv_id": "primary"}], 4, 0.7, lambda paper_id: paper_id,
            main._result_from_hit, lambda hit: False, ranks)
        self.assertEqual([row["arxiv_id"] for row in rows],
                         ["main-1", "graph-tail", "main-2", "graph-two"])
        self.assertEqual([row["candidate_channel"] for row in rows],
                         ["fused", "citation_graph", "fused", "citation_graph"])
        self.assertIn("never corroboration", rows[1]["candidate_channel_note"])
        self.assertTrue(all(not row["passes_relevance_floor"] for row in rows))
        small = source_discovery.select_source_candidates(
            hits, [{"arxiv_id": "primary"}], 1, 0.7, lambda paper_id: paper_id,
            main._result_from_hit, lambda hit: False, ranks)
        self.assertEqual([row["arxiv_id"] for row in small], ["main-1"])

    def test_graph_lane_aliases_dedupe_and_empty_graph_keeps_hier_order(self):
        aliases = [_hit("main-first", 0.66), _hit("alias-a", 0.64),
                   _hit("alias-b", 0.63)]
        aliased = source_discovery.select_source_candidates(
            aliases, [], 4, 0.7,
            lambda paper_id: "canonical" if paper_id.startswith("alias-") else paper_id,
            main._result_from_hit, lambda hit: False,
            {"alias-b": {"graph": 1}})
        alias_rows = [row for row in aliased if row["arxiv_id"].startswith("alias-")]
        self.assertEqual(len(alias_rows), 1)
        self.assertEqual(alias_rows[0]["candidate_channel"], "citation_graph")

        ordered = source_discovery.select_source_candidates(
            [_hit("a", 0.6), _hit("b", 0.5), _hit("c", 0.4)], [], 2, 0.7,
            lambda paper_id: paper_id, main._result_from_hit, lambda hit: False)
        self.assertEqual([row["arxiv_id"] for row in ordered], ["a", "b"])
        self.assertEqual([row["candidate_channel"] for row in ordered], ["fused", "fused"])

    def test_zero_limit_returns_no_candidates(self):
        args = ([_hit("p", 0.9)], [], 0, 0.7, lambda paper_id: paper_id,
                main._result_from_hit, lambda hit: False)
        self.assertEqual(source_discovery.select_source_candidates(*args), [])
        self.assertEqual(source_discovery.merge_source_candidates(
            [{"arxiv_id": "p"}], [], [], 0, lambda paper_id: paper_id,
            main.reciprocal_rank_fusion), [])

    def test_metadata_budget_caps_candidates_for_downstream_hydration(self):
        hits = [_hit(f"paper-{i}", 0.8 - i / 100) for i in range(8)]
        rows = source_discovery.select_source_candidates(
            hits, [], 10, 0.7, lambda paper_id: paper_id,
            main._result_from_hit, lambda hit: False, metadata_budget=3)
        self.assertEqual([row["arxiv_id"] for row in rows],
                         ["paper-0", "paper-1", "paper-2"])
        self.assertLessEqual(len(rows), 3)

    def test_invalid_metadata_budget_fails_closed(self):
        args = ([_hit("paper", 0.9)], [], 10, 0.7, lambda paper_id: paper_id,
                main._result_from_hit, lambda hit: False)
        self.assertEqual(source_discovery.select_source_candidates(
            *args, metadata_budget=1.5), [])


class SourceDiscoverySearchTests(unittest.TestCase):
    def setUp(self):
        main.search_cache.data.clear()
        main.qdrant_result_cache.data.clear()

    def _run(self, hits, discovery=True, hier_extra=None, **body_fields):
        body = main.SearchBody(
            query="source discovery fixture", layer="web3", hybrid=True,
            dedupe=True, diagnose=False, auto_relax=False, limit=2, **body_fields)

        def hier(*args, **kwargs):
            extra = hier_extra or {"paper_scores": {}, "global_channel": "used",
                                   "global_extra": []}
            return hits, [], [], extra

        with (patch.object(main, "SOURCE_DISCOVERY", discovery),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_hier_run", side_effect=hier) as hier_run,
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence",
                           side_effect=lambda rows, evidence_rows=None: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)
        return payload, hier_run

    def test_primary_order_count_unchanged_and_below_floor_is_auxiliary_only(self):
        hits = [_hit("primary-a", 0.91), _hit("weak-source", 0.66),
                _hit("primary-b", 0.88)]
        payload, _ = self._run(hits)

        self.assertEqual([row["arxiv_id"] for row in payload["results"]],
                         ["primary-a", "primary-b"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual([row["arxiv_id"] for row in payload["source_candidates"]],
                         ["weak-source"])
        self.assertEqual(payload["source_candidate_count"], 1)
        self.assertTrue(payload["source_candidates"][0]["verification_required"])
        self.assertIn("never proof of novelty", payload["discovery_note"])

    def test_candidates_preserve_only_actual_hierarchy_provenance(self):
        hits = [_hit("primary", 0.91), _hit("lexical-source", 0.69),
                _hit("unannotated-source", 0.68)]
        extra = {"paper_scores": {"lexical-source": 0.76},
                 "found_via": {"lexical-source": "lexical match"},
                 "fusion_ranks": {"lexical-source": {"dense": None, "lex": 1,
                                                       "global": None}},
                 "fusion_scores": {"lexical-source": 0.0123},
                 "global_channel": "used", "global_extra": []}
        payload, _ = self._run(hits, hier_extra=extra)
        candidate = payload["source_candidates"][0]
        self.assertEqual(candidate.get("found_via"), "lexical match", candidate)
        self.assertEqual(candidate["paper_score"], 0.76)
        self.assertEqual(candidate["fusion_ranks"], extra["fusion_ranks"]["lexical-source"])
        self.assertEqual(candidate["fusion_score"], 0.0123)
        self.assertEqual(candidate["channels"], ["lexical"])
        self.assertNotIn("found_via", payload["source_candidates"][1])

    def test_discovery_gate_excludes_strict_and_explicit_threshold(self):
        for fields in ({"strict": True}, {"min_score": 0.5}):
            with self.subTest(fields=fields):
                payload, _ = self._run([_hit("weak", 0.66)], **fields)
                self.assertNotIn("source_candidates", payload)
                self.assertNotIn("source_candidate_count", payload)

    def test_feature_off_preserves_primary_and_adds_observed_summary(self):
        body = main.SearchBody(query="flag off fixture", layer="web3", hybrid=True,
                               dedupe=True, diagnose=False, limit=2)
        with (patch.object(main, "SOURCE_DISCOVERY", False),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "embed_query", return_value=[0.1]),
              patch.object(main, "_hier_run", return_value=(
                  [_hit("primary", 0.9)], [], [], {"paper_scores": {},
                  "global_channel": "used", "global_extra": []})),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence",
                           side_effect=lambda rows, evidence_rows=None: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)
        self.assertEqual(set(payload), {
            "results", "count", "usage", "filters_applied", "retrieval_summary"})
        self.assertNotIn("source_candidates", payload)
        summary = payload["retrieval_summary"]
        self.assertEqual(set(summary), {
            "scope", "ordering_policy", "observed_candidate_count",
            "returned_unique_count", "channels"})
        self.assertEqual(summary, {
            "scope": "observed_local",
            "ordering_policy": "final_primary_then_pre_cut_then_hydrated_hierarchy; canonical-id dedupe",
            "observed_candidate_count": 0,
            "returned_unique_count": 1,
            "channels": {"paper": {"candidate_count": 0, "returned_count": 1}},
        })
        self.assertEqual([row["arxiv_id"] for row in payload["results"]], ["primary"])
        primary_ranks = [(row["arxiv_id"], row["rank_score"]) for row in payload["results"]]

        with (patch.object(main, "SOURCE_DISCOVERY", True),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "embed_query", return_value=[0.1]),
              patch.object(main, "_hier_run", return_value=(
                  [_hit("primary", 0.9)], [], [], {"paper_scores": {},
                  "global_channel": "used", "global_extra": []})),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence",
                           side_effect=lambda rows, evidence_rows=None: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            enabled_payload = main._run_search(body)
        self.assertEqual([row["arxiv_id"] for row in enabled_payload["results"]],
                         [row["arxiv_id"] for row in payload["results"]])
        self.assertEqual([(row["arxiv_id"], row["rank_score"])
                          for row in enabled_payload["results"]], primary_ranks)

    def test_feature_flag_does_not_change_primary_payload(self):
        hits = [_hit("primary-a", 0.91), _hit("weak-source", 0.66),
                _hit("primary-b", 0.88)]
        enabled, _ = self._run(hits)
        disabled, _ = self._run(hits, discovery=False)
        self.assertEqual(enabled["results"], disabled["results"])
        self.assertEqual(enabled["count"], disabled["count"])

    def test_hierarchy_called_once_and_filters_remain_on_original_retrieval(self):
        payload, hier_run = self._run([_hit("weak", 0.66)], section_type="method")
        self.assertEqual(hier_run.call_count, 1)
        self.assertIn({"key": "section_type", "match": {"value": "method"}},
                      hier_run.call_args.args[1]["must"])
        self.assertEqual(len(payload["source_candidates"]), 1)

    def test_audit_scoring_ignores_source_candidate_root_field(self):
        payload = {"results": [{"arxiv_id": "primary", "score": 0.9}],
                   "source_candidates": [{"arxiv_id": "weak", "score": 0.2}]}
        self.assertEqual(main._scored_results_for_audit(payload), payload["results"])

    def test_soft_layer_candidate_merge_excludes_final_primary_and_preserves_origin(self):
        primary = [{"arxiv_id": "weak-a", "relevance_floor": 0.70,
                    "retrieval_role": "source_candidate"}]
        partner = [{"arxiv_id": "weak-b", "relevance_floor": 0.79,
                    "retrieval_role": "source_candidate"},
                   {"arxiv_id": "final-primary", "relevance_floor": 0.79}]
        merged = source_discovery.merge_source_candidates(
            primary, partner, [{"arxiv_id": "final-primary"}], 10,
            lambda paper_id: paper_id, main.reciprocal_rank_fusion)
        self.assertEqual({row["arxiv_id"] for row in merged}, {"weak-a", "weak-b"})
        self.assertEqual(next(row for row in merged if row["arxiv_id"] == "weak-a")
                         ["relevance_floor"], 0.70)

    def test_soft_layer_path_merges_hydrated_candidates_without_primary_mutation(self):
        def hier(*args, **kwargs):
            body = args[3]
            hits = [_hit(f"{body.layer}-primary", 0.91),
                    _hit(f"{body.layer}-candidate", 0.70)]
            return hits, [], [], {"paper_scores": {}, "global_channel": "used",
                                  "global_extra": []}

        def hydrate(rows, evidence_rows=None):
            return [dict(row, hydrated=True) for row in rows]

        body = main.SearchBody(query="soft layer agents fixture", layer="ai-agents",
                               hybrid=True, dedupe=True, diagnose=False,
                               auto_relax=False, limit=4)
        with (patch.object(main, "SOURCE_DISCOVERY", True),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "SOFT_LAYERS", True),
              patch.object(main, "_soft_partner", return_value="llm-slm"),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_hier_run", side_effect=hier),
              patch.object(main, "_bm25_candidates", return_value=[]),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence", side_effect=hydrate),
              patch.object(main, "_evidence_rows", return_value={}) as evidence_rows,
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)

        self.assertEqual({row["arxiv_id"] for row in payload["results"]},
                         {"ai-agents-primary", "llm-slm-primary"})
        self.assertEqual({row["arxiv_id"] for row in payload["source_candidates"]},
                         {"ai-agents-candidate", "llm-slm-candidate"})
        self.assertTrue(all(row["hydrated"] for row in payload["source_candidates"]))
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["source_candidate_count"], 2)
        self.assertEqual(evidence_rows.call_count, 1)
        self.assertEqual(set(evidence_rows.call_args.args[0]), {
            "ai-agents-primary", "llm-slm-primary",
            "ai-agents-candidate", "llm-slm-candidate",
        })

    def test_soft_layer_uses_available_partner_candidates_and_keeps_partial(self):
        def hier(*args, **kwargs):
            body = args[3]
            if body.layer == "ai-agents":
                return [_hit("primary-fallback", 0.91)], [], [], None
            hits = [_hit("partner-primary", 0.91), _hit("partner-candidate", 0.70)]
            return hits, [], [], {"paper_scores": {}, "global_channel": "used",
                                  "global_extra": []}

        def lexical_timeout(*args, **kwargs):
            raise main.FuturesTimeoutError()

        body = main.SearchBody(query="soft layer partial fixture", layer="ai-agents",
                               hybrid=True, dedupe=True, diagnose=False,
                               auto_relax=False, limit=4)
        with (patch.object(main, "SOURCE_DISCOVERY", True),
              patch.object(main, "HIER_SEARCH", True),
              patch.object(main, "SOFT_LAYERS", True),
              patch.object(main, "_soft_partner", return_value="llm-slm"),
              patch.object(main, "embed_query", return_value=[0.1, 0.2]),
              patch.object(main, "_hier_run", side_effect=hier),
              patch.object(main, "_bm25_candidates", side_effect=lexical_timeout),
              patch.object(main, "_paper_facts", return_value={}),
              patch.object(main, "_hydrate_evidence",
                           side_effect=lambda rows, evidence_rows=None: rows),
              patch.object(main, "_evidence_rows", return_value={}),
              patch.object(main, "_inject_exact", side_effect=lambda rows, *args: rows),
              patch.object(main, "_lookup_note", return_value=None)):
            payload = main._run_search(body)

        self.assertTrue(payload["partial"])
        self.assertEqual([row["arxiv_id"] for row in payload["source_candidates"]],
                         ["partner-candidate"])
        self.assertEqual([row["arxiv_id"] for row in payload["results"]],
                         ["primary-fallback", "partner-primary"])

    def test_compact_copy_retains_root_candidate_fields(self):
        candidate = {
            "arxiv_id": "x", "title": "Paper", "url": "https://example.org/x",
            "source": "arXiv", "year": 2024, "section_type": "method",
            "section_title": "Methods", "element_type": "prose", "fulltext": None,
            "evidence": {"fragment_scope": "indexed_chunk", "fragment_hash": "abc",
                         "fragment_chars": 500, "returned_chars": 500,
                         "acquisition_type": "latex", "completeness": "fulltext_acquired"},
            "score": 0.6, "paper_score": 0.71, "found_via": "paper graph",
            "channels": ["paper", "graph"], "fusion_ranks": {"graph": 1},
            "retrieval_role": "source_candidate",
            "candidate_reason": "fragment_below_relevance_floor",
            "relevance_floor": 0.7, "passes_relevance_floor": False,
            "verification_required": True, "candidate_channel": "citation_graph",
            "candidate_channel_note": "lead only",
            "text": "long fragment", "terms": ["term"], "repos": ["repo"],
            "layers": ["web3"], "venue": "venue", "citation_count": 2,
            "niche_score": 0.4, "rank_score": 0.5, "fusion_score": 0.03,
            "license": {"terms": "t"}, "arxiv_url": "https://example.org/x",
        }
        full = {"results": [], "count": 0, "source_candidates": [candidate],
                "source_candidate_count": 1, "discovery_note": "caution"}
        before = copy.deepcopy(full)
        compact = main._compact_search(full)
        self.assertEqual(set(compact["source_candidates"][0]), {
            "arxiv_id", "title", "url", "source", "year", "section_type",
            "section_title", "element_type", "fulltext", "evidence", "score",
            "paper_score", "found_via", "channels", "fusion_ranks",
            "retrieval_role", "candidate_reason", "relevance_floor",
            "passes_relevance_floor", "verification_required", "candidate_channel",
            "candidate_channel_note",
        })
        compact_evidence = compact["source_candidates"][0]["evidence"]
        self.assertEqual(compact_evidence["returned_chars"], 0)
        self.assertEqual(compact_evidence["fragment_chars"], 500)
        self.assertEqual(compact_evidence["fragment_scope"], "indexed_chunk")
        self.assertEqual(compact_evidence["fragment_hash"], "abc")
        self.assertEqual(compact_evidence["acquisition_type"], "latex")
        self.assertEqual(compact_evidence["completeness"], "fulltext_acquired")
        self.assertNotIn("license", compact["source_candidates"][0])
        self.assertNotIn("arxiv_url", compact["source_candidates"][0])
        self.assertEqual(compact["licenses"], {"arXiv": {"terms": "t"}})
        self.assertEqual(full, before)
        self.assertEqual(compact["source_candidate_count"], 1)
        self.assertEqual(compact["discovery_note"], "caution")

    def test_primary_and_candidates_share_one_metadata_batch(self):
        primary = main._result_from_hit(_hit("2401.00001", 0.9))
        candidate = main._result_from_hit(_hit("2401.00002", 0.65))
        payload = {"results": [primary], "source_candidates": [candidate]}
        metadata = {
            "2401.00001": {"fulltext_source": "latex", "source_url": "https://example.org/1"},
            "2401.00002": {"fulltext_source": "latex", "source_url": "https://example.org/2"},
        }
        with patch.object(main, "_evidence_rows", return_value=metadata) as evidence_rows:
            main._hydrate_search_payload(payload)

        evidence_rows.assert_called_once_with(["2401.00001", "2401.00002"])
        self.assertEqual(payload["results"][0]["fulltext"], True)
        self.assertEqual(payload["source_candidates"][0]["fulltext"], True)
        self.assertEqual(payload["results"][0]["evidence"]["source_url"],
                         "https://example.org/1")
        self.assertEqual(payload["source_candidates"][0]["evidence"]["source_url"],
                         "https://example.org/2")


if __name__ == "__main__":
    unittest.main()
