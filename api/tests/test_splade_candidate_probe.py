import importlib.util
import json
import tempfile
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "eval" / "splade_candidate_probe.py"
SPEC = importlib.util.spec_from_file_location("splade_candidate_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def _original_factory():
    def original(papers, query, layer, year_from, year_to, limit=15, lexical_future=None):
        return list(papers), ["lexical-1", "lexical-2"]
    return original


def _maps():
    return ({"canon-a": "source-a", "canon-b": "source-b", "canon-c": "source-c"},
            {"alias-a": "canon-a", "source-a": "canon-a", "canon-a": "canon-a",
             "canon-b": "canon-b", "canon-c": "canon-c"},
            {"canon-a": {"web3"}, "canon-b": {"llm-slm"}, "canon-c": {"web3"}})


def test_only_appends_up_to_budget_without_replacing_or_lexical_vote():
    source, aliases, layers = _maps()
    original_papers = [{"arxiv_id": "dense-1", "paper_score": .7}]
    papers, lexical, appended = probe.append_splade_candidates(
        _original_factory(), original_papers, "query", "web3", None, None, ranked_ids=["canon-a", "canon-b", "alias-a", "canon-c"],
        source_by_canonical=source, alias_to_canonical=aliases, canonical_layers=layers)
    assert papers[0] == original_papers[0]
    assert papers[1:] == [{"arxiv_id": "source-a", "paper_score": .7},
                          {"arxiv_id": "source-c", "paper_score": .7}]
    assert lexical == ["lexical-1", "lexical-2"]
    assert appended == ["source-a", "source-c"]
    assert all(row["paper_score"] == .7 for row in papers[1:])


def test_existing_alias_canonical_candidate_is_not_appended():
    source, aliases, layers = _maps()
    _, _, appended = probe.append_splade_candidates(
        _original_factory(), [{"arxiv_id": "alias-a", "paper_score": .9}],
        "query", "web3", None, None, ranked_ids=["canon-a"],
        source_by_canonical=source, alias_to_canonical=aliases,
        canonical_layers=layers)
    assert appended == []


def test_zero_append_budget_selects_nothing():
    source, aliases, layers = _maps()
    papers, lexical, appended = probe.append_splade_candidates(
        _original_factory(), [], "query", "web3", None, None,
        ranked_ids=["canon-a"], source_by_canonical=source,
        alias_to_canonical=aliases, canonical_layers=layers, append_limit=0)
    assert papers == []
    assert lexical == ["lexical-1", "lexical-2"]
    assert appended == []


def test_query_embedding_functions_use_correct_dimensions_and_fail_closed():
    class FakeApp:
        pass
    small = {"q": [0.0] * 384}
    base = {"q": [0.0] * 768}
    probe._install_query_vectors(FakeApp, small, base)
    assert FakeApp.embed_query("q") is small["q"]
    assert len(FakeApp.embed_query("q")) == 384
    assert FakeApp.embed_query_coarse("q") is base["q"]
    assert len(FakeApp.embed_query_coarse("q")) == 768
    with pytest.raises(KeyError):
        FakeApp.embed_query("unknown")
    with pytest.raises(KeyError):
        FakeApp.embed_query_coarse("unknown")


def test_treatment_wrapper_is_query_and_layer_scoped_and_preserves_lexical():
    source, aliases, layers = _maps()
    fake_app = type("FakeApp", (), {"_lexical_paper_shortlist": staticmethod(_original_factory())})
    wrapped, log = probe._run_pair(fake_app, "q", "web3", {"q": ["canon-a"]},
                                   source, aliases, layers, 10)
    dense = [{"arxiv_id": "dense", "paper_score": .8}]
    papers, lexical = wrapped(dense, "q", "web3", None, None)
    assert papers[0] == dense[0]
    assert lexical == ["lexical-1", "lexical-2"]
    assert log[0]["appended_ids"] == ["source-a"]
    sibling, sibling_lexical = wrapped(dense, "q", "llm-slm", None, None)
    assert sibling == dense
    assert sibling_lexical == ["lexical-1", "lexical-2"]
    assert len(log) == 1


def test_aliases_deduplicate_to_first_canonical_representative():
    source, aliases, layers = _maps()
    _, _, appended = probe.append_splade_candidates(
        _original_factory(), [], "query", "web3", None, None,
        ranked_ids=["alias-a", "canon-a"], source_by_canonical=source,
        alias_to_canonical=aliases, canonical_layers=layers)
    assert appended == ["source-a"]


def test_layer_mismatch_is_not_appended():
    source, aliases, layers = _maps()
    papers, _, appended = probe.append_splade_candidates(
        _original_factory(), [], "query", "web3", None, None, ranked_ids=["canon-b"],
        source_by_canonical=source, alias_to_canonical=aliases, canonical_layers=layers)
    assert papers == []
    assert appended == []


def test_budget_counts_only_unique_eligible_splade_candidates():
    source = {f"c{i}": f"s{i}" for i in range(14)}
    aliases = {f"c{i}": f"c{i}" for i in range(14)}
    layers = {f"c{i}": {"web3"} for i in range(14)}
    _, _, appended = probe.append_splade_candidates(
        _original_factory(), [], "query", "web3", None, None, ranked_ids=list(source),
        source_by_canonical=source, alias_to_canonical=aliases,
        canonical_layers=layers)
    assert len(appended) == 10


def test_vector_loader_fails_closed_on_missing_or_bad_vectors():
    with tempfile.TemporaryDirectory(dir=MODULE_PATH.parents[2]) as directory:
        path = Path(directory) / "vectors.json"
        path.write_text(json.dumps({"q": [0.0] * 383}), encoding="utf-8")
        with pytest.raises(ValueError):
            probe._load_query_vectors(path, ["q"], 384, "small")
        path.write_text(json.dumps({"q": [float("nan")] * 384}), encoding="utf-8")
        with pytest.raises(ValueError):
            probe._load_query_vectors(path, ["q"], 384, "small")
        path.write_text(json.dumps({}), encoding="utf-8")
        with pytest.raises(ValueError):
            probe._load_query_vectors(path, ["q"], 384, "small")


def test_private_search_caches_are_replaced():
    class App:
        search_cache = object()
        qdrant_result_cache = object()
        embedding_cache = object()
    probe._disable_caches(App)
    assert App.search_cache.get("missing") is None
    assert App.qdrant_result_cache.get("missing") is None
    App.search_cache.set("key", "stale")
    assert App.search_cache.get("key") is None
