import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(EVAL_DIR))
from analyze_splade_trace import analyze


def _fixtures(tmp_path):
    original = tmp_path / "original.json"
    holdout = tmp_path / "holdout.json"
    fixture = {"groups": [{"id": "group", "layer": "web3",
                           "targets": [{"canonical_id": "target", "aliases": ["target-alias"]}],
                           "phrases": [{"id": "query", "query": "synthetic query"}]}]}
    original.write_text(json.dumps(fixture), encoding="utf-8")
    holdout.write_text(json.dumps({"groups": []}), encoding="utf-8")
    return original, holdout


def _hit(aid="target-alias", score=.76, boilerplate=False):
    return {"chunk_id": f"chunk-{aid}", "arxiv_id": aid, "score": score,
            "section_type": "method", "title": "Synthetic", "boilerplate": boilerplate}


def _event(stage, layer="web3", **fields):
    return {"stage": stage, "layer": layer, "query": "synthetic query", **fields}


def _probe(original, events, *, partial=False, added=None, stage_a=None, results=None):
    trace = {"version": 1, "events": events}
    baseline = {"results": [{"arxiv_id": "target-alias"}], "raw_payload": {
        "results": [{"arxiv_id": "target-alias"}],
        "diagnostic_trace": {"version": 1, "events": []}}}
    treatment = {"results": results if results is not None else [],
                 "ids": results if results is not None else [], "partial": partial,
                 "added_ids": added if added is not None else ["target-alias"],
                 "lexical_calls": [{"paper_ids_after": stage_a if stage_a is not None else ["other", "target-alias"]}],
                 "raw_payload": {"results": results if results is not None else [],
                                 "diagnostic_trace": trace}}
    return {"records": [{"fixture": original.name, "group_id": "group",
                          "phrase_id": "query", "query": "synthetic query", "layer": "web3",
                          "baseline": baseline, "treatment": treatment, "error": None}]}


def test_added_target_matches_candidate_prefix_and_records_stage_diagnostics(tmp_path):
    original, holdout = _fixtures(tmp_path)
    events = [
            _event("stage_b", input_arxiv_ids={"items": ["wrong-graph-pool"], "total": 1,
                                                    "truncated": False}, output_hits={"items": [], "total": 0,
                                                                                       "truncated": False}),
            _event("stage_b", input_arxiv_ids={"items": ["other", "target-alias"], "total": 2,
                                                    "truncated": False}, output_hits={"items": [_hit(boilerplate=True)],
                                                                                       "total": 1, "truncated": False}),
            _event("hier_run", ordered_hits={"items": [_hit()], "total": 1,
                                                   "truncated": False},
                        paper_scores={"target-alias": .72}, fusion_ranks={"target-alias": {"dense": 3}},
                        fusion_scores={"target-alias": .12}, lex_exempt=[]),
            _event("dedupe_preferring_content", before={"items": [{"arxiv_id": "other"}, _hit(boilerplate=True)],
                                                             "total": 2, "truncated": False},
                        after={"items": [{"arxiv_id": "other"}, {"arxiv_id": "another"},
                                         {"arxiv_id": "target-alias"}], "total": 3, "truncated": False},
                        floor=.7, lex_exempt=[], limit=10),
            _event("inject_exact", before_ids={"items": ["other", "target-alias"], "total": 2,
                                                     "truncated": False},
                        after_ids={"items": ["other", "target-alias"], "total": 2, "truncated": False}),
        ]
    row = analyze(_probe(original, events, results=["other"]), original, holdout)["per_query"][0]
    assert row["trace_comparison"]["primary_candidate_stage_b_prefix_match"] is True
    assert row["treatment"]["layers"]["web3"]["events"][0]["stage"] != "unrelated"
    added = row["added_target_sources"][0]
    assert added["stage_b_output"]["best_score"] == .76
    assert added["stage_b_output"]["boilerplate"] is True
    assert added["hier"]["rank"] == 1
    assert added["hier"]["fusion_ranks"] == {"dense": 3}
    assert added["hier"]["fusion_score"] == .12
    assert added["hier"]["paper_score"] == .72
    assert added["dedupe"]["floor"] == .7
    assert added["dedupe"]["after_rank"] == 3
    assert added["cause"] == "later_rank_or_cut"
    assert added["boilerplate_rank_worse_observed_separately"] is True


def test_partner_layer_hit_does_not_claim_primary_pool_presence(tmp_path):
    original, holdout = _fixtures(tmp_path)
    primary = _event("stage_b", input_arxiv_ids={"items": ["other"], "total": 1,
                                                           "truncated": False},
                              output_hits={"items": [], "total": 0, "truncated": False})
    partner = _event("stage_b", layer="ai-agents", input_arxiv_ids={"items": ["target-alias"],
                                                                                "total": 1, "truncated": False},
                              output_hits={"items": [_hit()], "total": 1, "truncated": False})
    row = analyze(_probe(original, [primary, partner], stage_a=["other"]), original, holdout)["per_query"][0]
    assert row["trace_comparison"]["primary_candidate_stage_b_prefix_match"] is True
    assert row["treatment"]["layers"]["ai-agents"]["primary_layer"] is False
    assert row["added_target_sources"][0]["cause"] == "not_in_primary_candidates"
    assert "says nothing about corpus/index coverage" in row["trace_comparison"]["candidate_pool_scope"]


def test_target_in_stage_a_but_absent_from_primary_stage_b_hits(tmp_path):
    original, holdout = _fixtures(tmp_path)
    event = _event("stage_b", input_arxiv_ids={"items": ["other", "target-alias"],
                                                          "total": 2, "truncated": False},
                            output_hits={"items": [], "total": 0, "truncated": False})
    row = analyze(_probe(original, [event], stage_a=["other", "target-alias"]), original, holdout)["per_query"][0]
    assert row["added_target_sources"][0]["cause"] == "not_in_primary_chunk_pool"


def test_truncated_output_does_not_claim_target_absent(tmp_path):
    original, holdout = _fixtures(tmp_path)
    event = _event("stage_b", input_arxiv_ids={"items": ["target-alias"], "total": 1,
                                                          "truncated": False},
                            output_hits={"items": [], "total": 600, "truncated": True})
    row = analyze(_probe(original, [event], stage_a=["target-alias"]), original, holdout)["per_query"][0]
    assert row["treatment"]["layers"]["web3"]["events"][0]["output"]["any_target"] is None
    assert row["added_target_sources"][0]["stage_b_output"]["present"] is None
    assert row["added_target_sources"][0]["cause"] == "not_proven"


def test_below_floor_uses_best_dedupe_input_chunk_score(tmp_path):
    original, holdout = _fixtures(tmp_path)
    stage_b_hit = _hit(score=.69)
    dedupe_best_hit = _hit(score=.73)
    events = [
            _event("stage_b", input_arxiv_ids={"items": ["target-alias"], "total": 1,
                                                     "truncated": False},
                        output_hits={"items": [stage_b_hit], "total": 1, "truncated": False}),
            _event("hier_run", ordered_hits={"items": [stage_b_hit], "total": 1, "truncated": False},
                        paper_scores={}, fusion_ranks={}, fusion_scores={}, lex_exempt=[]),
            _event("dedupe_preferring_content", before={"items": [stage_b_hit, dedupe_best_hit], "total": 2,
                                                             "truncated": False},
                        after={"items": [], "total": 0, "truncated": False},
                        floor=.79, lex_exempt=[], limit=10),
        ]
    # Both observed candidate chunks remain below the threshold; report the true maximum (.73).
    row = analyze(_probe(original, events, stage_a=["target-alias"]), original, holdout)["per_query"][0]
    assert row["added_target_sources"][0]["dedupe"]["before_best_score"] == .73
    assert row["added_target_sources"][0]["cause"] == "relevance_floor"


def test_partial_pair_keeps_denominator_but_cause_is_unknown(tmp_path):
    original, holdout = _fixtures(tmp_path)
    events = [_event("stage_b", input_arxiv_ids={"items": [], "total": 0,
                                                           "truncated": False},
                              output_hits={"items": [], "total": 0, "truncated": False})]
    report = analyze(_probe(original, events, partial=True, stage_a=[]), original, holdout)
    row = report["per_query"][0]
    assert report["denominators"]["all"] == 1
    assert report["pair_observation_counts"]["complete_baseline_and_treatment"] == 0
    assert row["added_target_sources"][0]["cause"] == "unknown_incomplete_or_error"
    assert row["treatment"]["final"]["target_hit"] is None


def test_combined_path_unions_layers_without_requiring_source_was_added(tmp_path):
    original, holdout = _fixtures(tmp_path)
    candidate = _hit(score=.75)
    events = [
        _event("hier_run", ordered_hits={"items": [candidate], "total": 1, "truncated": False}),
        _event("dedupe_preferring_content", before={"items": [candidate], "total": 1,
                                                     "truncated": False},
               after={"items": [], "total": 0, "truncated": False},
               floor=.7, lex_exempt=[], limit=10),
        _event("hier_run", layer="ai-agents", ordered_hits={"items": [candidate], "total": 1,
                                                               "truncated": False}),
        _event("dedupe_preferring_content", layer="ai-agents",
               before={"items": [candidate], "total": 1, "truncated": False},
               after={"items": [], "total": 0, "truncated": False},
               floor=.7, lex_exempt=[], limit=10),
    ]
    report = analyze(_probe(original, events, added=[], stage_a=[]), original, holdout)
    path = report["per_query"][0]["treatment"]["combined_target_path"]
    counts = report["combined_path_counts_all_layers"]["treatment"]
    assert path["hier_any_target"] is True
    assert path["dedupe_after_any_target"] is False
    assert path["cause"] == "passed_floor_not_selected"
    assert report["per_query"][0]["added_target_sources"] == []
    assert counts["complete_denominator"] == 1
    assert counts["hier_any_target"] == 1
    assert counts["dedupe_after_any_target"] == 0
    assert counts["cause_counts_complete_only"]["passed_floor_not_selected"] == 1
