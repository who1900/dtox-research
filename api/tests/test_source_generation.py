import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import time
import threading

import pytest

from api import source_generation as generation
from api import source_representations as catalog
from api.tests.test_source_representations import representation, source


FULL_COVERAGE = {"version": 1, "scope": "full_snapshot", "partial": False,
                 "input_sha256": "a" * 64, "export_report_sha256": "b" * 64}


@pytest.fixture
def corpus(tmp_path):
    def build(sources=None, reps=None):
        path = tmp_path / "catalog.sqlite"
        result = catalog.build_catalog(path, sources or [source("a"), source("b")],
            reps if reps is not None else [representation("a", "a"), representation("b", "b")], coverage=FULL_COVERAGE)
        manifest = {"version": 1, "generation": result["generation"], "catalog_path": str(path),
                    "collection": generation.collection_name(result["generation"]),
                    "model_id": generation.MODEL_ID, "dimension": 768, "complete": True,
                    "embedding": {"model_sha256": "a" * 64, "tokenizer_sha256": "b" * 64,
                                  "dimension": 768, "max_length": 384, "pooling": "CLS/L2",
                                  "query_instruction": generation.QUERY_INSTRUCTION},
                    "counts": {"sources": len(sources or [1, 2]), "representations": len(reps) if reps is not None else 2,
                               "points": max(2, len(reps or []))}}
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        return path, manifest_path, manifest
    return build


def discover(path, dense=None, **kwargs):
    kwargs.setdefault("runtime_descriptor", lambda timeout: {
        "model": generation.MODEL_ID,
        "embedding": {"model_sha256": "a" * 64, "tokenizer_sha256": "b" * 64,
                      "dimension": 768, "max_length": 384, "pooling": "CLS/L2",
                      "query_instruction": generation.QUERY_INSTRUCTION}})
    return generation.discover_sources(path, kwargs.pop("query", "needle"),
        embed_query=kwargs.pop("embed_query", lambda text, timeout: [0.1] * 768),
        query_dense=dense or (lambda *args: []), timeout=kwargs.pop("timeout", 2), **kwargs)


def test_one_vote_per_source_per_channel(corpus):
    reps = [representation("a-summary", "a", kind="own_summary"),
            representation("a-own", "a"),
            representation("citation", "a", origin="b", kind="citation_context"),
            representation("b", "b")]
    _, path, manifest = corpus(reps=reps)
    result = discover(path, lambda *args: [{"representation_id": r["representation_id"], "score": 1}
                                         for r in reps])
    assert result["status"] == "ready"
    assert not result["partial"]
    row = result["sources"][0]
    assert row["source_id"] == "a"
    assert row["channel_ranks"] == {"dense": 1, "lexical": 1}
    assert row["rrf_score"] == pytest.approx(2 / 61)
    foreign = next(r for r in row["representations"] if r["kind"] == "citation_context")
    assert foreign["origin_source_id"] == "b"
    assert foreign["target_source_id"] == "a"
    assert foreign["evidence_state"] == row["evidence_state"] == "not_read"
    assert result["generation"] == manifest["generation"]


@pytest.mark.parametrize("mutate", [lambda m: m.update(complete=False),
    lambda m: m.update(dimension=384), lambda m: m.update(collection="papers_coarse"),
    lambda m: m.update(generation="0" * 64), lambda m: m.update(version=True),
    lambda m: m.update(counts={"sources": 2, "representations": 2, "points": 1})])
def test_manifest_never_silently_activates(corpus, mutate):
    _, path, manifest = corpus()
    mutate(manifest)
    path.write_text(json.dumps(manifest))
    result = discover(path, lambda *args: pytest.fail("invalid generation called dense"))
    assert result["status"] == "invalid"
    assert result["sources"] == []
    assert generation.generation_cache_identity(path) is None


def test_missing_manifest_and_cache_identity(corpus, tmp_path):
    assert discover(tmp_path / "absent")["status"] == "unavailable"
    _, path, manifest = corpus()
    first = generation.generation_cache_identity(path)
    assert first
    manifest["extra"] = "changed-generation-descriptor"
    path.write_text(json.dumps(manifest))
    assert generation.generation_cache_identity(path) != first


def test_dense_failure_is_explicit_partial_not_empty_complete(corpus):
    _, path, _ = corpus()
    def fail(*args):
        raise TimeoutError()
    result = discover(path, fail)
    assert result["sources"]
    assert result["partial"]
    assert "dense_failed:TimeoutError" in result["diagnostics"]["reasons"]
    assert all(set(row["channel_ranks"]) == {"lexical"} for row in result["sources"])


@pytest.mark.parametrize("rows", [[{"representation_id": "a", "score": 1, "generation": "0" * 64}],
                                 [{"representation_id": "unknown", "score": 1}],
                                 [{"representation_id": "a", "score": float("nan")}],
                                 [{"representation_id": "a", "score": 1}] * 129])
def test_dense_invalid_rows_cannot_contribute(corpus, rows):
    _, path, _ = corpus()
    result = discover(path, lambda *args: rows)
    assert result["partial"]
    assert all("dense" not in s["channel_ranks"] for s in result["sources"])


def test_target_metadata_filters_before_lexical_cap_and_dense_hydration(corpus):
    sources = [source("a", year=2012, layers=["protocol"]), source("b", year=2025, layers=["ai"])]
    reps = [representation("noise" + str(i), "b") for i in range(150)]
    reps.append(representation("lead", "a", origin="b", kind="citation_context"))
    _, path, _ = corpus(sources=sources, reps=reps)
    calls = []
    def dense(vector, collection, filters, limit, timeout):
        calls.append(filters)
        return [{"representation_id": "lead", "score": 1}, {"representation_id": "noise0", "score": 2}]
    result = discover(path, dense, layer="protocol", year_to=2015)
    assert [s["source_id"] for s in result["sources"]] == ["a"]
    assert {"key": "target_layers", "match": {"value": "protocol"}} in calls[0]["must"]
    assert {"key": "target_year", "range": {"lte": 2015}} in calls[0]["must"]


def test_strict_filter_requires_own_passage_not_foreign_tags(corpus):
    reps = [representation("lead", "a", origin="b", kind="citation_context", section_type="method"),
            representation("summary", "a", kind="own_summary", section_type="method"),
            representation("b", "b", section_type="method")]
    _, path, _ = corpus(reps=reps)
    result = discover(path, lambda *args: [{"representation_id": "lead", "score": 10}], section_type="method")
    assert [s["source_id"] for s in result["sources"]] == ["b"]


def test_grouped_response_and_aliases(corpus):
    reps = [representation("a", "a"), representation("alias", "alias"), representation("b", "b")]
    _, path, _ = corpus(sources=[source("a"), source("alias"), source("b")], reps=reps)
    result = discover(path, lambda *args: {"groups": [{"hits": [
        {"payload": {"representation_id": "alias"}, "score": 1},
        {"payload": {"representation_id": "a"}, "score": 1}]}]},
        canonical_id=lambda value: "a" if value == "alias" else value)
    assert [s["source_id"] for s in result["sources"]].count("a") == 1
    assert result["sources"][0]["channel_ranks"]["dense"] == 1


def test_stale_visibility_never_leaks_foreign_context(corpus):
    reps = [representation("lead", "a", origin="b", kind="citation_context")]
    _, path, _ = corpus(reps=reps)
    def visible(records, timeout):
        return {r["source_id"]: r["publication_revision"] if r["source_id"] == "a" else "0" * 64 for r in records}
    result = discover(path, lambda *args: [{"representation_id": "lead", "score": 1}], visible_sources=visible)
    assert not result["sources"]
    assert result["partial"]


def test_valid_visibility_and_error_fail_closed(corpus):
    _, path, _ = corpus()
    result = discover(path, visible_sources=lambda records, timeout: {r["source_id"]: r["publication_revision"] for r in records})
    assert len(result["sources"]) == 2
    def fail(*args):
        raise RuntimeError()
    result = discover(path, visible_sources=fail)
    assert result["partial"] and not result["sources"]


def test_deadline_passed_to_every_callback_and_checked_after(corpus):
    _, path, _ = corpus()
    finished = threading.Event()
    def embed(text, timeout):
        assert 0 < timeout <= 0.02
        time.sleep(0.03)
        finished.set()
        return [0.1] * 768
    result = discover(path, embed_query=embed, timeout=0.02)
    assert any("deadline" in reason for reason in result["diagnostics"]["reasons"])
    assert result["partial"]
    assert all("dense" not in row["channel_ranks"] for row in result["sources"])
    assert finished.wait(0.2)


def test_manifest_swap_during_callback_is_rejected(corpus):
    _, path, manifest = corpus()
    def dense(*args):
        path.write_text(json.dumps({**manifest, "descriptor": "new"}))
        return []
    result = discover(path, dense)
    assert result["status"] == "invalid" and not result["sources"]


def test_hydration_uses_rep_id_index_no_full_table_scan(corpus):
    path, _, manifest = corpus()
    with catalog.CatalogReader(path, manifest["generation"], deadline=time.monotonic() + 2) as reader:
        plans = reader.conn.execute("EXPLAIN QUERY PLAN SELECT * FROM representations WHERE representation_id IN (?)", ("a",)).fetchall()
        assert all("SCAN representations" not in row[3] for row in plans)
        assert list(reader.hydrate(["a"])) == ["a"]
    with pytest.raises(ValueError):
        catalog.hydrate_representations(path, ["a"], generation="0" * 64)


def test_legacy_none_is_visible_but_missing_or_new_revision_is_not(corpus):
    a, b = source("a"), source("b")
    a["publication_revision"] = b["publication_revision"] = None
    _, path, _ = corpus(sources=[a, b])
    result = discover(path, visible_sources=lambda records, timeout: {"a": None})
    assert [s["source_id"] for s in result["sources"]] == ["a"]
    assert result["sources"][0]["publication_state"] == "legacy_unmarked"
    result = discover(path, visible_sources=lambda records, timeout: {"a": "a" * 64, "b": None})
    assert [s["source_id"] for s in result["sources"]] == ["b"]


def test_limit_50_is_applied_after_canonical_fusion(corpus):
    sources = [source("s" + str(i)) for i in range(60)]
    reps = [representation("r" + str(i), "s" + str(i)) for i in range(60)]
    _, path, _ = corpus(sources=sources, reps=reps)
    assert len(discover(path, limit=50)["sources"]) == 50
    assert len(discover(path)["sources"]) == 20


def test_context_trimming_is_not_candidate_incompleteness(corpus):
    reps = [representation("r" + str(i), "a") for i in range(12)]
    _, path, _ = corpus(reps=reps)
    result = discover(path)
    assert result["diagnostics"]["details_truncated"] == 4
    assert not result["partial"]


def test_stale_dense_rep_loses_vote_even_when_lexical_own_rep_is_live(corpus):
    reps = [representation("lead", "a", origin="b", kind="citation_context", text="foreign"),
            representation("own", "a", text="needle")]
    _, path, _ = corpus(reps=reps)
    result = discover(path, lambda *args: [{"representation_id": "lead", "score": 1}],
        visible_sources=lambda records, timeout: {"a": source("a")["publication_revision"]})
    assert result["sources"][0]["channel_ranks"] == {"lexical": 1}


def test_dense_owner_target_metadata_mismatch_is_rejected(corpus):
    _, path, _ = corpus()
    result = discover(path, lambda *args: [{"representation_id": "a", "origin_id": "b", "score": 1}])
    assert result["partial"]
    assert all("dense" not in s["channel_ranks"] for s in result["sources"])


@pytest.mark.parametrize("proof", [None, {"model": generation.MODEL_ID},
    {"model": generation.MODEL_ID, "embedding": {"model_sha256": "0" * 64}}])
def test_missing_or_wrong_query_runtime_proof_fails_closed(corpus, proof):
    _, path, _ = corpus()
    result = discover(path, runtime_descriptor=lambda timeout: proof)
    assert result["status"] == "unavailable" and not result["sources"]
    assert "query_runtime_unverified" in result["diagnostics"]["reasons"]


def test_query_callback_proof_attribute_preserves_vector_callback_contract(corpus):
    _, path, manifest = corpus()
    def embed(text, timeout):
        return [0.1] * 768
    embed.embedding_descriptor = {"model": manifest["model_id"], "embedding": manifest["embedding"]}
    result = generation.discover_sources(path, "needle", embed_query=embed,
        query_dense=lambda *args: [], timeout=2)
    assert result["status"] == "ready" and result["sources"]


def test_standalone_api_import():
    result = subprocess.run([sys.executable, "-B", "-c", "import source_generation; print(source_generation.DIMENSION)"],
        cwd=Path(generation.__file__).parent, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "768"


def native_point(rep_id, source_id, text="needle", revision=None):
    return {"id": rep_id, "score": 1.0, "payload": {"arxiv_id": source_id, "text": text,
        "publication_revision": source(source_id)["publication_revision"] if revision is None else revision}}


def test_native_exact_text_revision_and_indexed_mapping(corpus):
    path, manifest_path, manifest = corpus(reps=[representation("a1", "a"), representation("a2", "a"),
                                                 representation("b", "b")])
    result = discover(manifest_path, query_owned=lambda *args: [native_point("old-a", "a")])
    a = next(s for s in result["sources"] if s["source_id"] == "a")
    assert a["channel_ranks"]["dense"] == 1
    assert all(rep["native_provenance"][0]["point_id"] == "old-a" for rep in a["representations"])
    with catalog.CatalogReader(path, manifest["generation"], deadline=time.monotonic() + 2) as reader:
        assert reader.owned_payload(native_point("old-a", "a")["payload"]) == ["a1", "a2"]
        assert reader.owned_payload(native_point("old-a", "a", text="needle different sentence")["payload"]) == []
        assert reader.owned_payload(native_point("old-a", "a", revision="0" * 64)["payload"]) == []
        plan = reader.conn.execute("EXPLAIN QUERY PLAN SELECT representation_id FROM representations "
            "WHERE source_id=? AND indexed_text_sha256=? AND kind='own_passage' AND origin_id=source_id", ("a", "0" * 64)).fetchall()
        assert all("SCAN representations" not in row[3] for row in plan)


@pytest.mark.parametrize("changed", ["text", "publication_revision"])
def test_native_fresh_owner_is_as_of_excluded_but_changed_member_is_partial(corpus, changed):
    _, path, manifest = corpus()
    fresh, inside = native_point("old-fresh", "fresh"), native_point("old-a", "a")
    checked = []
    def visible(records, timeout):
        checked.extend(r["source_id"] for r in records)
        return {sid: source(sid)["publication_revision"] for sid in ("a", "fresh")}
    result = discover(path, query_owned=lambda *args: [fresh, inside], hybrid=False, visible_sources=visible)
    assert result["status"] == "ready" and not result["partial"]
    assert result["generation"] == manifest["generation"]
    assert [row["source_id"] for row in result["sources"]] == ["a"]
    assert checked and set(checked) == {"a"}
    assert result["diagnostics"]["native_outside_generation"] == 1
    assert result["diagnostics"]["reasons"] == []
    changed_inside = {**inside, "payload": {**inside["payload"],
        changed: "changed whole chunk" if changed == "text" else "0" * 64}}
    mismatch = discover(path, query_owned=lambda *args: [fresh, changed_inside], hybrid=False,
                        visible_sources=visible)
    assert mismatch["partial"] and not mismatch["sources"]
    assert mismatch["diagnostics"]["native_outside_generation"] == 1
    assert "native_unmatched_payload" in mismatch["diagnostics"]["reasons"]


@pytest.mark.parametrize("canonical", [False, True])
def test_native_ids_are_validated_before_membership_lookup(corpus, monkeypatch, canonical):
    path, _, manifest = corpus()
    with catalog.CatalogReader(path, manifest["generation"], deadline=time.monotonic() + 2,
            canonical_id=(lambda sid: "bad\x00id") if canonical else None) as reader:
        point = native_point("old-fresh", "fresh" if canonical else "bad\x00id")
        monkeypatch.setattr(reader, "source", lambda sid: pytest.fail("invalid ID reached membership lookup"))
        with pytest.raises(ValueError, match="identifier"):
            reader.owned_payload(point["payload"])


@pytest.mark.parametrize("physical", ["dense_derived", "dense_native"])
def test_normal_full_candidate_window_is_ready_bounded_not_partial(corpus, physical):
    sources = [source(f"s{i:03}") for i in range(128)]
    kind = "own_summary" if physical == "dense_derived" else "own_passage"
    reps = [representation(f"r{i:03}", f"s{i:03}", kind=kind) for i in range(128)]
    _, path, _ = corpus(sources=sources, reps=reps)
    dense = (lambda *args: [{"representation_id": rep["representation_id"], "score": 1} for rep in reps]
             ) if physical == "dense_derived" else (lambda *args: [])
    native = (lambda *args: [native_point(f"old-{i:03}", f"s{i:03}") for i in range(128)]
              ) if physical == "dense_native" else None
    result = discover(path, dense, query_owned=native, limit=50)
    assert result["status"] == "ready" and not result["partial"]
    assert len(result["sources"]) == 50
    assert result["diagnostics"]["reasons"] == []
    assert set(result["diagnostics"]["limits"]) == {physical + "_candidate_limit", "lexical_candidate_limit"}
    assert result["diagnostics"]["retrieval_bounded"] is True
    assert result["diagnostics"]["exhaustive"] is False
    assert all(row["channel_ranks"].keys() == {"dense", "lexical"} for row in result["sources"])


def test_two_dense_physical_feeds_have_one_vote_not_score_calibration(corpus):
    reps = [representation("summary", "a", kind="own_summary"), representation("a", "a"), representation("b", "b")]
    _, path, _ = corpus(reps=reps)
    result = discover(path, lambda *args: [{"representation_id": "summary", "score": 0.001}],
        query_owned=lambda *args: [native_point("old-b", "b"), {**native_point("old-a", "a"), "score": 0.99}])
    a = next(s for s in result["sources"] if s["source_id"] == "a")
    assert a["dense_physical_ranks"] == {"dense_derived": 1, "dense_native": 2}
    assert set(a["channel_ranks"]) == {"lexical", "dense"}
    assert a["rrf_score"] <= 2 / 61


def test_lexical_sql_interrupt_does_not_erase_dense_native(corpus, monkeypatch):
    reps = [representation("summary", "a", kind="own_summary"), representation("b", "b")]
    _, path, _ = corpus(reps=reps)
    def interrupted(self, query):
        error = sqlite3.OperationalError("interrupted")
        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        raise error
    monkeypatch.setattr(catalog.CatalogReader, "lexical", interrupted)
    result = discover(path, lambda *args: [{"representation_id": "summary", "score": 1}],
                      query_owned=lambda *args: [native_point("old-b", "b")])
    assert {s["source_id"] for s in result["sources"]} == {"a", "b"}
    assert all(set(s["channel_ranks"]) == {"dense"} for s in result["sources"])
    assert result["partial"] and "lexical_deadline" in result["diagnostics"]["reasons"]


def test_real_lexical_stream_deadline_preserves_finished_dense_native(corpus, monkeypatch):
    _, path, _ = corpus(reps=[representation("summary", "a", kind="own_summary"), representation("b", "b")])
    dense_done, native_done = threading.Event(), threading.Event()
    qualify, expired = catalog.CatalogReader.qualify, []
    def expire_once(self, record):
        result = qualify(self, record)
        if not expired:
            assert dense_done.wait(0.5) and native_done.wait(0.5)
            expired.append(True)
            self.deadline = time.monotonic() - 1
        return result
    def dense(*args):
        dense_done.set()
        return [{"representation_id": "summary", "score": 1}]
    def native(*args):
        native_done.set()
        return [native_point("old-b", "b")]
    monkeypatch.setattr(catalog.CatalogReader, "qualify", expire_once)
    result = discover(path, dense, query_owned=native)
    assert expired and {s["source_id"] for s in result["sources"]} == {"a", "b"}
    assert all(set(s["channel_ranks"]) == {"dense"} for s in result["sources"])
    assert result["partial"] and "lexical_deadline" in result["diagnostics"]["reasons"]


def test_all_channels_fail_is_explicit_partial(corpus, monkeypatch):
    _, path, _ = corpus()
    def interrupted(*args):
        raise catalog._Deadline()
    def failure(*args):
        raise TimeoutError()
    monkeypatch.setattr(catalog.CatalogReader, "lexical", interrupted)
    result = discover(path, failure, query_owned=failure)
    assert not result["sources"] and result["partial"]
    assert "lexical_deadline" in result["diagnostics"]["reasons"]


def test_hybrid_false_disables_only_lexical_keeps_same_query(corpus, monkeypatch):
    _, path, _ = corpus()
    monkeypatch.setattr(catalog.CatalogReader, "lexical", lambda *args: pytest.fail("lexical must not run"))
    seen = []
    def embed(text, timeout):
        seen.append(text)
        return [0.1] * 768
    def owned(text, filters, limit, timeout):
        seen.append(text)
        return [native_point("old-a", "a")]
    result = discover(path, embed_query=embed, query_owned=owned, hybrid=False, query="same original query")
    assert seen == ["same original query", "same original query"]
    assert result["sources"][0]["channel_ranks"] == {"dense": 1}


def test_native_legacy_requires_explicit_none_and_live_visibility(corpus):
    a = source("a")
    a["publication_revision"] = None
    _, path, _ = corpus(sources=[a], reps=[representation("a", "a")])
    point = {"id": "legacy", "score": 1, "payload": {"arxiv_id": "a", "text": "needle", "publication_revision": None}}
    result = discover(path, query_owned=lambda *args: [point], hybrid=False,
                      visible_sources=lambda records, timeout: {"a": None})
    assert result["sources"][0]["publication_state"] == "legacy_unmarked"
    point["payload"].pop("publication_revision")
    result = discover(path, query_owned=lambda *args: [point], hybrid=False)
    assert result["partial"] and not result["sources"]
