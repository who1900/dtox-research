import contextlib
import copy
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from api import source_representations as catalog


def revision(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source(source_id, *, year=2020, layers=None, status="done", title=None):
    return {"source_id": source_id, "title": title or source_id, "year": year,
            "layers": ["crypto"] if layers is None else layers, "status": status,
            "publication_revision": revision(source_id + "-publication"), "source_url": None}


def representation(rep_id, target, *, origin=None, kind="own_passage", text="needle",
                   section_type=None, element_type=None):
    origin = origin or target
    locator = {"start": 17, "end": 17 + len(text), "unit": "unicode_codepoint"}
    for field, value in (("section_type", section_type), ("element_type", element_type)):
        if value is not None:
            locator[field] = value
    binding = {"exact_key": "[7]", "identifier": target} if kind == "citation_context" else None
    return {"representation_id": rep_id, "source_id": target, "origin_id": origin,
            "origin_revision": revision(origin + "-artifact"), "kind": kind,
            "locator": locator, "text": text, "binding": binding, "binding_status": "resolved"}


class SourceRepresentationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "generation.sqlite"

    def build(self, sources, representations, **kwargs):
        self.canonical = kwargs.pop("canonicalize", None)
        return catalog._build_catalog(self.path, sources, representations, **kwargs)

    def search(self, query="needle", **kwargs):
        if "deadline" in kwargs:
            kwargs["timeout"] = max(0, kwargs.pop("deadline") - time.monotonic())
        for old, new in (("year_min", "year_from"), ("year_max", "year_to"), ("source_limit", "limit")):
            if old in kwargs:
                kwargs[new] = kwargs.pop(old)
        kwargs.setdefault("canonical_id", getattr(self, "canonical", None))
        return catalog.search_catalog(self.path, query, **kwargs)

    def test_canonical_dedup_before_rrf_has_one_vote_per_channel(self):
        canonicalize = lambda x: "a" if x in {"a", "alias"} else x
        fused = catalog._aggregate_rank_lists(
            {"passages": ["alias", "a", "alias", "b"], "citations": ["a", "a", "b"]},
            canonicalize=canonicalize)
        self.assertEqual([r["source_id"] for r in fused], ["a", "b"])
        self.assertEqual(fused[0]["channel_ranks"], {"passages": 1, "citations": 1})
        self.assertAlmostEqual(fused[0]["rrf_score"], 2 / 61)
        self.assertAlmostEqual(fused[1]["rrf_score"], 2 / 62)
        reps = [representation(f"a-{i}", "alias" if i % 2 else "a") for i in range(12)]
        self.build([source("a"), source("alias"), source("b")], [*reps, representation("b", "b")],
                   canonicalize=canonicalize)
        rows = self.search()["sources"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["rrf_score"] <= 1 / 61 for r in rows))
        self.assertEqual(sorted(r["channel_ranks"]["lexical"] for r in rows), [1, 2])

    def test_year_and_layer_use_target_not_text_owner(self):
        self.build([source("target", year=2012, layers=["protocol"]),
                    source("owner", year=2025, layers=["ai"])],
                   [representation("citation", "target", origin="owner", kind="citation_context")])
        rows = self.search(year_min=2010, year_max=2015, layer="protocol")["sources"]
        self.assertEqual([r["source_id"] for r in rows], ["target"])
        self.assertEqual(rows[0]["year"], 2012)
        self.assertEqual(self.search(year_min=2024)["sources"], [])
        self.assertEqual(self.search(layer="ai")["sources"], [])

    def test_foreign_text_never_claimed_as_target_evidence(self):
        self.build([source("target", title="Target title"), source("owner", title="Owner title")],
                   [representation("citation", "target", origin="owner", kind="citation_context")])
        row, = self.search()["sources"]
        context, = row["representations"]
        self.assertEqual(row["title"], "Target title")
        self.assertNotIn("text", row)
        self.assertEqual(row["evidence_state"], "not_read")
        self.assertTrue(row["verification_required"])
        self.assertEqual(context["target_source_id"], "target")
        self.assertEqual(context["origin_source_id"], "owner")
        self.assertEqual(context["origin_title"], "Owner title")
        self.assertEqual(context["origin_revision"], revision("owner-artifact"))
        self.assertEqual(context["origin_publication_revision"], revision("owner-publication"))
        self.assertEqual(context["kind"], "citation_context")
        self.assertEqual(context["binding"], {"exact_key": "[7]", "identifier": "target"})
        self.assertEqual(context["locator"]["start"], 17)
        self.assertNotIn("probability", context)

    def test_section_element_filters_require_matching_owned_passages(self):
        self.build([source("foreign"), source("owner"), source("summary"), source("owned")],
                   [representation("citation", "foreign", origin="owner", kind="citation_context",
                                   section_type="method", element_type="proof"),
                    representation("summary", "summary", kind="own_summary",
                                   section_type="method", element_type="proof"),
                    representation("owned", "owned", section_type="method", element_type="proof")])
        for kwargs in ({"section_type": "method"}, {"element_type": "proof"},
                       {"section_type": "method", "element_type": "proof"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual([r["source_id"] for r in self.search(**kwargs)["sources"]], ["owned"])
        self.assertEqual(self.search(section_type="conclusion")["sources"], [])
        with self.assertRaises(ValueError):
            self.search(section_type="")
        with self.assertRaises(TypeError):
            self.search(unsupported_filter="ignored")

    def test_hidden_targets_and_origins_excluded_before_candidate_limit(self):
        self.build([source("hidden-target", status="processing"), source("hidden-owner", status="failed"),
                    source("target"), source("owner")],
                   [representation("hidden-target", "hidden-target", origin="owner", kind="citation_context"),
                    representation("hidden-owner", "target", origin="hidden-owner", kind="citation_context"),
                    representation("visible", "target", origin="owner", kind="citation_context")])
        row, = self.search()["sources"]
        self.assertEqual(row["source_id"], "target")
        self.assertEqual([r["representation_id"] for r in row["representations"]], ["visible"])

    def test_missing_invalid_and_valid_empty_are_distinct(self):
        self.assertEqual(self.search()["diagnostics"]["reasons"][0], "missing_catalog")
        sqlite3.connect(self.path).close()
        self.assertEqual(self.search()["status"], "invalid")
        self.path.unlink()
        self.build([], [])
        result = self.search()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["partial"])

    def test_published_generation_cannot_be_overwritten_and_stage_survives(self):
        stage = self.root / "stage"
        stage.mkdir()
        marker = stage / "caller-owned.sqlite"
        conn = sqlite3.connect(marker)
        conn.execute("CREATE TABLE untouched (id)")
        conn.close()
        self.build([source("target")], [representation("passage", "target")], stage_dir=stage)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build([], [], stage_dir=stage)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(stage.iterdir()), [marker])
        conn = catalog._connect(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM sources")
        finally:
            conn.close()

    def test_builder_cache_temp_and_mmap_are_explicitly_bounded(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            catalog._configure_builder(conn)
            self.assertEqual(conn.execute("PRAGMA cache_size").fetchone(), (-8192,))
            self.assertEqual(conn.execute("PRAGMA temp_store").fetchone(), (1,))
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone(), (1,))

    def test_finalization_rejects_external_fts_content_mismatch(self):
        with contextlib.closing(sqlite3.connect(self.root / "stage.sqlite")) as conn:
            catalog._configure_builder(conn)
            conn.executescript(catalog._SCHEMA)
            catalog._insert_source(conn, source("a"))
            catalog._insert_representation(conn, representation("p", "a"))
            conn.execute("UPDATE representations SET text='unindexed corruption'")
            with self.assertRaises(sqlite3.DatabaseError):
                catalog._finish_catalog(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM catalog").fetchone(), (0,))

    def test_atomic_no_clobber_race_and_failed_build_cleanup(self):
        stage = self.root / "stage"
        stage.mkdir()
        real_link = os.link

        def concurrent_publish(src, dst):
            real_link(src, dst)
            raise FileExistsError("another publisher won")

        with patch.object(catalog.os, "link", side_effect=concurrent_publish):
            with self.assertRaises(FileExistsError):
                self.build([source("a")], [representation("p", "a")], stage_dir=stage)
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(self.search()["status"], "ready")
        other = self.root / "failed.sqlite"
        with self.assertRaises(sqlite3.IntegrityError):
            catalog._build_catalog(other, [], [representation("p", "missing")], stage_dir=stage)
        self.assertFalse(other.exists())
        self.assertEqual(list(stage.iterdir()), [])

    def test_validation_rejects_unresolved_unbound_and_wrong_ownership(self):
        valid = representation("p", "a")
        invalid = []
        for field, value in (("origin_revision", "not-sha"), ("representation_id", "bad id"),
                             ("binding_status", "unresolved"), ("origin_id", "b"),
                             ("locator", {"start": True, "end": 9, "unit": "utf8_byte"}),
                             ("locator", {"start": 0, "end": 9, "unit": "page"}),
                             ("locator", {"start": 0, "end": 9, "unit": "utf8_byte", "x": float("nan")})):
            invalid.append({**valid, field: value})
        citation = representation("p", "a", origin="b", kind="citation_context")
        invalid.extend([{**citation, "binding": None},
                        {**citation, "binding": {"exact_key": "[7]", "identifier": "b"}},
                        {**citation, "origin_id": "a"}])
        for rep in invalid:
            with self.subTest(rep=rep):
                with self.assertRaises(ValueError):
                    self.build([source("a"), source("b")], [rep])
                self.assertFalse(self.path.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_self_citation_and_owned_summary_remain_discovery(self):
        self_cite = representation("citation", "a", kind="citation_context")
        self_cite["binding"]["self_citation"] = True
        self.build([source("a")], [self_cite, representation("summary", "a", kind="own_summary")])
        row, = self.search()["sources"]
        self.assertEqual(row["evidence_state"], "not_read")
        self.assertEqual({r["kind"] for r in row["representations"]}, {"citation_context", "own_summary"})

    def test_conflicting_alias_metadata_rejected_and_inputs_unchanged(self):
        a = source("a")
        alias = {**a, "source_id": "alias"}
        reps = [representation("p", "alias")]
        original = copy.deepcopy([a, alias, reps])
        canonicalize = lambda x: "a" if x == "alias" else x
        self.build([a, alias], reps, canonicalize=canonicalize)
        self.assertEqual([a, alias, reps], original)
        self.assertEqual(self.search()["sources"][0]["source_id"], "a")
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.root / "conflict.sqlite", [a, {**a, "year": 2021}], [])

    def test_caps_sources_representations_contexts_and_origins(self):
        sources = [source(f"s{i}") for i in range(25)]
        reps = [representation(f"p{i}", f"s{i}") for i in range(25)]
        owners = [source(f"o{i}") for i in range(6)]
        reps += [representation(f"c{i}", "s0", origin=f"o{i}", kind="citation_context") for i in range(6)]
        self.build(sources + owners, reps)
        result = self.search(limit=999)
        self.assertLessEqual(len(result["sources"]), 20)
        self.assertLessEqual(sum(len(r["representations"]) for r in result["sources"]), 128)
        row = next(r for r in result["sources"] if r["source_id"] == "s0")
        self.assertEqual(len({r["origin_source_id"] for r in row["representations"]
                              if r["kind"] == "citation_context"}), 3)
        self.assertIn("context_limit", result["diagnostics"]["reasons"])
        self.assertIn("source_limit", result["diagnostics"]["reasons"])
        self.assertLessEqual(result["diagnostics"]["collected_representations"], 128)

    def test_dangerous_fts_queries_are_literal_safe_bounded_or_tokens(self):
        self.build([source("a")], [representation("p", "a")])
        for query in ('needle" OR * NOT title:evil NEAR(a,b)', '"', '*',
                      "needle'); DROP TABLE sources; --", "needle\x00*", "needle " * 10000):
            with self.subTest(query=query[:80]):
                result = self.search(query)
                self.assertEqual(result["status"], "ready")
        self.assertEqual(catalog._safe_fts_query('needle" : x*'), '"needle" OR "x"')
        self.assertEqual(len(catalog._safe_fts_query(" ".join(f"x{i}" for i in range(100))).split(" OR ")), 32)
        self.assertEqual(len(self.search()["sources"]), 1)

    def test_deadline_retains_completed_channel_and_closes_connection(self):
        self.build([source("a"), source("b")],
                   [representation("summary", "a", kind="own_summary"), representation("passage", "b")])
        real_connect = catalog._connect
        connections = []

        def tracked(path):
            conn = real_connect(path)
            connections.append(conn)
            return conn

        checks = 0

        def deadline_check(deadline):
            nonlocal checks
            checks += 1
            if checks == 4:
                raise catalog._Deadline

        with patch.object(catalog, "_connect", side_effect=tracked), \
                patch.object(catalog, "_check_deadline", side_effect=deadline_check):
            result = self.search(deadline=time.monotonic() + 10)
        self.assertEqual([r["source_id"] for r in result["sources"]], ["a"])
        self.assertIn("deadline", result["diagnostics"]["reasons"])
        self.assertTrue(result["partial"])
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        expired = self.search(deadline=time.monotonic() - 1)
        self.assertEqual(expired["status"], "unavailable")
        self.assertEqual(expired["sources"], [])
        self.assertIn("deadline", expired["diagnostics"]["reasons"])

    def test_sql_progress_handler_interrupts_and_is_removed_on_cleanup(self):
        self.build([source("a")], [representation("p", "a")])
        now = time.monotonic()
        closed = []

        class SlowConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "SELECT r.*" in sql and parameters[-1] != 0:
                    super().execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n").fetchone()
                return super().execute(sql, parameters)

            def close(self):
                closed.append(True)
                super().close()

        def connect(path):
            conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, factory=SlowConnection)
            conn.row_factory = sqlite3.Row
            return conn

        with patch.object(catalog, "_connect", side_effect=connect), \
                patch.object(catalog.time, "monotonic", side_effect=[now] * 8 + [now + 20] * 100):
            result = self.search(deadline=now + 10)
        self.assertIn("deadline", result["diagnostics"]["reasons"])
        self.assertEqual(closed, [True])


    def test_multiline_context_and_opaque_artifact_locator(self):
        rep = representation("p", "a", text="paragraph\n\\\\latex\tcontext")
        rep["locator"]["artifact"] = "latex_source"
        rep["locator"]["end"] = 1234
        built = catalog.build_catalog(self.path, [source("a")], [rep])
        result = self.search("paragraph")
        self.assertEqual(result["generation"], built["generation"])
        context = result["sources"][0]["representations"][0]
        self.assertEqual(context["text"], rep["text"])
        self.assertEqual(context["locator"], rep["locator"])

    def test_nullable_publication_and_url_metadata_are_not_live_certificate(self):
        a = {**source("a"), "publication_revision": None, "source_url": "https://example.org/a"}
        self.build([a], [representation("p", "a")])
        row, = self.search()["sources"]
        self.assertEqual(row["publication_state"], "legacy_unmarked")
        self.assertEqual(row["source_url"], a["source_url"])
        self.assertIsNone(row["publication_revision"])
        for url in ("file:///a", "https://user:pass@example.org/a", "https://a/a b"):
            with self.assertRaises(ValueError):
                catalog.build_catalog(self.root / "invalid-url.sqlite", [{**a, "source_url": url}], [])

    def test_manifest_generation_is_content_based_order_independent(self):
        sources = [source("a"), source("b")]
        reps = [representation("p", "a"), representation("q", "b")]
        a = catalog.build_catalog(self.path, sources, reps)
        b = catalog.build_catalog(self.root / "other.sqlite", list(reversed(sources)), list(reversed(reps)))
        self.assertEqual(a["generation"], b["generation"])
        explicit = revision("caller generation")
        c = catalog.build_catalog(self.root / "caller.sqlite", sources, reps, generation_id=explicit)
        self.assertEqual(c["generation"], explicit)
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.root / "bad.sqlite", [], [], generation_id="UUID-not-SHA")

    def test_foreign_strict_hit_needs_own_qualifier_and_preserves_owned_context(self):
        foreign = representation("c", "a", origin="b", kind="citation_context", text="needle foreign")
        foreign["terms"] = ["proof"]
        own = representation("p", "a", text="owned theorem", section_type="method", element_type="proof")
        own["terms"] = ["proof"]
        self.build([source("a"), source("b")], [foreign, own])
        result = self.search(section_type="method", element_type="proof", terms=["PROOF"])
        row, = result["sources"]
        self.assertEqual([r["kind"] for r in row["representations"]], ["own_passage", "citation_context"])
        self.assertEqual(self.search(terms=["absent"])["sources"], [])
        self.assertEqual(self.search(element_type="table")["sources"], [])
        with self.assertRaises(ValueError):
            self.search(terms="proof")

    def test_citation_only_discovery_keeps_own_summary_before_foreign_context(self):
        self.build([source("a"), source("b")],
                   [representation("c", "a", origin="b", kind="citation_context"),
                    representation("s", "a", kind="own_summary", text="owned overview")])
        row, = self.search()["sources"]
        self.assertEqual([r["kind"] for r in row["representations"]], ["own_summary", "citation_context"])
        self.assertEqual(row["channel_ranks"], {"lexical": 1})
        self.assertEqual(row["publication_state"], "published_generation_snapshot")

    def test_generation_caps_even_many_matching_representations(self):
        self.build([source("a")], [representation(f"p{i}", "a") for i in range(180)])
        result = self.search()
        self.assertLessEqual(result["diagnostics"]["collected_representations"], 128)
        self.assertLessEqual(len(result["sources"][0]["representations"]), 8)
        self.assertIn("representation_limit", result["diagnostics"]["reasons"])

    def test_copying_passage_across_kinds_is_one_lexical_vote(self):
        metadata = [source("a-copy"), source("z-better"), source("owner")]
        metadata += [source(f"noise{i}") for i in range(8)]
        baseline = [representation("better", "z-better", text="needle mechanism")]
        baseline += [representation(f"noise{i}", f"noise{i}", text="unrelated filler")
                     for i in range(8)]
        for kinds in (("own_passage",), catalog.KINDS):
            with self.subTest(kinds=kinds):
                copies = [representation(f"copy-{kind}", "a-copy", kind=kind,
                                         origin="owner" if kind == "citation_context" else "a-copy",
                                         text="needle") for kind in kinds]
                path = self.root / f"copies-{len(kinds)}.sqlite"
                catalog.build_catalog(path, metadata, baseline + copies)
                with patch.object(catalog, "_aggregate_rank_lists", wraps=catalog._aggregate_rank_lists) as fusion:
                    result = catalog.search_catalog(path, "needle mechanism")
                self.assertEqual(fusion.call_count, 1)
                self.assertEqual(set(fusion.call_args.args[0]), {"lexical"})
                better, copied = result["sources"]
                self.assertEqual([better["source_id"], copied["source_id"]], ["z-better", "a-copy"])
                self.assertLess(min(r["bm25_score"] for r in better["representations"]),
                                min(r["bm25_score"] for r in copied["representations"]))
                self.assertEqual(better["channel_ranks"], {"lexical": 1})
                self.assertEqual(copied["channel_ranks"], {"lexical": 2})
                self.assertAlmostEqual(better["rrf_score"], 1 / 61)
                self.assertAlmostEqual(copied["rrf_score"], 1 / 62)
                self.assertEqual(len(copied["representations"]), len(kinds))

    def test_target_filters_apply_before_source_cap(self):
        metadata = [source(f"wrong{i}", year=1990, layers=["other"]) for i in range(32)]
        metadata += [source("correct", year=2020), source("owner", year=2025)]
        reps = [representation(f"p{i:02}", f"wrong{i}", origin="owner", kind="citation_context")
                for i in range(32)]
        reps.append(representation("z-correct", "correct", origin="owner", kind="citation_context"))
        self.build(metadata, reps)
        for filters in ({"layer": "crypto"}, {"year_from": 2010, "year_to": 2021},
                        {"layer": "crypto", "year_from": 2010}):
            with self.subTest(filters=filters):
                result = self.search(**filters)
                self.assertEqual([r["source_id"] for r in result["sources"]], ["correct"])
                self.assertFalse(result["partial"])

    def test_own_qualifier_filter_applies_before_source_cap(self):
        ids = [f"wrong{i}" for i in range(32)] + ["correct"]
        reps = []
        for i, source_id in enumerate(ids):
            reps.append(representation(f"c{i:02}", source_id, origin="owner", kind="citation_context"))
            own = representation(f"p{i:02}", source_id, text="definitions",
                                 section_type="method" if source_id == "correct" else "introduction",
                                 element_type="proof" if source_id == "correct" else "table")
            own["terms"] = ["proof" if source_id == "correct" else "other"]
            reps.append(own)
        self.build([source(source_id) for source_id in ids] + [source("owner")], reps)
        for filters in ({"section_type": "method"}, {"element_type": "proof"}, {"terms": ["proof"]}):
            with self.subTest(filters=filters):
                result = self.search(**filters)
                self.assertEqual([r["source_id"] for r in result["sources"]], ["correct"])
                self.assertFalse(result["partial"])

    def test_canonical_alias_diversity_before_read_cap(self):
        for count in (32, 33):
            with self.subTest(count=count):
                aliases = {f"alias{i}" for i in range(count)}
                metadata = [source(source_id) for source_id in sorted(aliases)] + [source("a"), source("b")]
                reps = [representation(f"p{i:02}", f"alias{i}") for i in range(count)]
                reps.append(representation("z-b", "b"))
                path = self.root / f"aliases-{count}.sqlite"
                catalog.build_catalog(path, metadata, reps)
                result = catalog.search_catalog(path, "needle", canonical_id=lambda x: "a" if x in aliases else x)
                self.assertEqual([r["source_id"] for r in result["sources"]], ["a", "b"])
                self.assertEqual(result["sources"][1]["channel_ranks"], {"lexical": 2})
                self.assertLessEqual(result["diagnostics"]["read_representations"], 128)

    def test_matching_orphans_are_explicitly_invalid_without_global_fk_scan(self):
        for missing in ("target", "origin", "representation"):
            with self.subTest(missing=missing):
                path = self.root / f"orphan-{missing}.sqlite"
                catalog.build_catalog(path, [source("target"), source("origin")],
                                      [representation("c", "target", origin="origin", kind="citation_context")])
                path.chmod(0o600)
                conn = sqlite3.connect(path)
                try:
                    if missing == "representation":
                        conn.execute("DELETE FROM representations")
                    else:
                        conn.execute("DELETE FROM sources WHERE source_id=?", (missing,))
                    conn.commit()
                finally:
                    conn.close()
                result = catalog.search_catalog(path, "needle")
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["sources"], [])
                self.assertIn("matching_orphan", result["diagnostics"]["reasons"])
                unrelated = catalog.search_catalog(path, "absent")
                self.assertEqual(unrelated["status"], "ready")
                self.assertFalse(unrelated["partial"])

    def test_parenthesized_doi_is_opaque_before_alias_mapping(self):
        doi = "doi:10.1234/example(" + revision("sample")[:8] + ")"
        self.build([source(doi), source("canonical")], [representation("p", doi)])
        result = self.search(canonical_id=lambda x: "canonical" if x == doi else x)
        self.assertEqual([r["source_id"] for r in result["sources"]], ["canonical"])
        self.assertEqual(result["sources"][0]["representations"][0]["origin_source_id"], doi)
        self.assertEqual(catalog._identifier(doi[4:]), doi[4:])
        for invalid in (doi + "\x00", doi + " space"):
            with self.assertRaises(ValueError):
                catalog._identifier(invalid)


if __name__ == "__main__":
    unittest.main()
