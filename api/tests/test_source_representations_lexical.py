import sqlite3
import time

import pytest

from api import source_representations as catalog
from api.tests.test_source_representations import representation, source


def build(tmp_path, sources, reps):
    path = tmp_path / "catalog.sqlite"
    result = catalog.build_catalog(path, sources, reps)
    return path, result["generation"]


def reader(path, generation, **filters):
    return catalog.CatalogReader(path, generation, deadline=time.monotonic() + 5, **filters)


def test_many_best_chunks_and_aliases_do_not_starve_canonical_heads(tmp_path):
    sources = [source("flood"), source("alias"), *[source(f"s{i:03}") for i in range(130)]]
    reps = [representation(f"f{i:03}", "flood" if i % 2 else "alias", text="needle " * 10)
            for i in range(300)]
    reps += [representation(f"r{i:03}", f"s{i:03}", text="needle weaker context") for i in range(130)]
    path, generation = build(tmp_path, sources, reps)
    with reader(path, generation, canonical_id=lambda sid: "flood" if sid == "alias" else sid) as r:
        first = r.conn.execute("SELECT rowid FROM representation_fts WHERE representation_fts MATCH ? "
                               "ORDER BY rank LIMIT 128", ('"needle"',)).fetchall()
        assert all(hit[0] <= 300 for hit in first)
        rows = r.lexical("needle")
        hydrated = r.hydrate([row["representation_id"] for row in rows])
        assert len(rows) == len(hydrated) == 128
        assert set(hydrated) == {"flood", *[f"s{i:03}" for i in range(127)]}


def test_target_filters_and_strict_own_qualifier_precede_source_cap(tmp_path):
    excluded = [source(f"x{i:03}", year=2025, layers=["ai"]) for i in range(140)]
    sources = [*excluded, source("target", year=2012, layers=["protocol"]),
               source("summary", year=2012, layers=["protocol"])]
    reps = [representation(f"x{i:03}", f"x{i:03}", text="needle " * 10,
                           section_type="method", element_type="proof") for i in range(140)]
    reps += [representation("foreign", "target", origin="x000", kind="citation_context"),
             representation("summary-foreign", "summary", origin="x000", kind="citation_context"),
             representation("summary-own", "summary", kind="own_summary", text="not matching",
                            section_type="method", element_type="proof")]
    own = representation("qualifier", "target", text="not matching", section_type="method", element_type="proof")
    own["terms"] = ["kv cache"]
    reps.append(own)
    path, generation = build(tmp_path, sources, reps)
    with reader(path, generation, layer="protocol", year_from=2010, year_to=2015,
                section_type="method", element_type="proof", terms=["KV CACHE"]) as r:
        assert [row["representation_id"] for row in r.lexical("needle")] == ["foreign"]


def test_boundary_ties_choose_exact_canonical_and_rep_heads(tmp_path):
    sources = [source(f"s{i:03}") for i in reversed(range(140))] + [source("alias")]
    reps = [representation(f"z{i:03}", f"s{i:03}", text="needle equal") for i in reversed(range(140))]
    reps.append(representation("a000", "alias", text="needle equal"))
    path, generation = build(tmp_path, sources, reps)
    with reader(path, generation, canonical_id=lambda sid: "s000" if sid == "alias" else sid) as r:
        ranks = r.conn.execute("SELECT DISTINCT rank FROM representation_fts "
                               "WHERE representation_fts MATCH ?", ('"needle"',)).fetchall()
        assert len(ranks) == 1
        rows = r.lexical("needle")
        assert [row["representation_id"] for row in rows] == ["a000", *[f"z{i:03}" for i in range(1, 128)]]
        assert [row["representation_id"] for row in r.lexical("needle", limit=3)] == ["a000", "z001", "z002"]


def test_alternates_are_bounded_and_only_follow_all_source_heads(tmp_path):
    reps = [representation(f"z{i:03}", "a") for i in range(150)]
    reps += [representation("b2", "b"), representation("b1", "b")]
    path, generation = build(tmp_path, [source("a"), source("b")], reps)
    with reader(path, generation) as r:
        rows = r.lexical("needle", limit=6)
        assert [row["representation_id"] for row in rows] == ["z000", "b1", "z001", "b2", "z002", "z003"]


@pytest.mark.parametrize("missing", ["representation", "target", "owner"])
def test_observed_matching_orphan_fails_without_global_validation(tmp_path, missing):
    path, generation = build(tmp_path, [source("target"), source("owner")],
        [representation("foreign", "target", origin="owner", kind="citation_context")])
    path.chmod(0o600)
    with sqlite3.connect(path) as conn:
        if missing == "representation":
            conn.execute("DELETE FROM representations")
        else:
            conn.execute("DELETE FROM sources WHERE source_id=?", (missing,))
    with reader(path, generation) as r:
        with pytest.raises(ValueError, match="orphan|target or owner"):
            r.lexical("needle")
        assert r.lexical("unmatched") == []


def test_rank_cursor_stops_before_worse_orphan_without_preflight_scan(tmp_path):
    sources = [source(f"s{i:03}") for i in range(128)] + [source("tail")]
    reps = [representation(f"r{i:03}", f"s{i:03}", text="needle " * 10) for i in range(128)]
    reps.append(representation("tail", "tail", text="needle " + "weaker " * 100))
    path, generation = build(tmp_path, sources, reps)
    path.chmod(0o600)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM representations WHERE representation_id='tail'")
    with reader(path, generation) as r:
        trace = []
        r.conn.set_trace_callback(trace.append)
        assert len(r.lexical("needle")) == 128
        assert not any("LEFT JOIN" in sql or "MATERIALIZED" in sql or "foreign_key_check" in sql for sql in trace)
        ranked = [sql for sql in trace if "ORDER BY rank" in sql]
        assert len(ranked) == 1
        plan = [row["detail"] for row in r.conn.execute("EXPLAIN QUERY PLAN " + ranked[0])]
        assert any("VIRTUAL TABLE INDEX 32:" in detail for detail in plan)
        assert not any("TEMP B-TREE" in detail or "MATERIALIZE" in detail for detail in plan)


def test_boundary_tie_deadline_closes_cursor_and_does_not_return_false_complete(tmp_path, monkeypatch):
    path, generation = build(tmp_path, [source("a"), source("b")],
                             [representation("a", "a"), representation("b", "b")])
    with reader(path, generation) as r:
        qualify = r.qualify
        def expire(record):
            result = qualify(record)
            r.deadline = time.monotonic() - 1
            return result
        monkeypatch.setattr(r, "qualify", expire)
        with pytest.raises(catalog._Deadline):
            r.lexical("needle", limit=1)
        r.deadline = time.monotonic() + 5
        monkeypatch.setattr(r, "qualify", qualify)
        assert len(r.lexical("needle")) == 2


def test_unknown_catalog_header_fails_before_lexical(tmp_path):
    path, generation = build(tmp_path, [source("a")], [representation("a", "a")])
    path.chmod(0o600)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="header"):
        with reader(path, generation):
            pytest.fail("unknown header was activated")
