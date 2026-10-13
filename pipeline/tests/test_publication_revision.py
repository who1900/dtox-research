"""Disk-only fixtures for attached revision transactions; no services or models."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from pipeline import publication as p
from pipeline.fts_catalog import ensure_catalog
from pipeline.tests.revision_fixture import admitted_artifact, stage_artifact


ADMISSION = "a" * 64


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def ready(job, artifact):
    return p.revision_readiness_expected(job["paper_id"], job["revision"], artifact)


def vector_plan(projection, prior=None, parent=None):
    recipe = {"source_sha256": "1" * 64, "abstract_char_limit": 800, "min_abstract_len": 80,
              "fallback_text": None, "fallback_source_sha256": None}
    fixed = {k: recipe[k] for k in ("source_sha256", "abstract_char_limit", "min_abstract_len")}
    inputs = {"fulltext": [(c["point_id"], c["text"]) for c in projection["chunks"]],
              "coarse": [(str(uuid.uuid5(uuid.NAMESPACE_URL, "coarse:" + projection["chunks"][0]["point_id"])),
                          p._revision_coarse_input(projection, recipe, {}))]}
    families = {}
    for name, points in inputs.items():
        family = {"collection": "fixture_" + name, "model_id": "fixture_" + name,
                  "model_sha256": "2" * 64, "dimension": 384 if name == "fulltext" else 768,
                  "recipe_sha256": hashlib.sha256(p._revision_bytes(fixed)).hexdigest() if name == "coarse" else "3" * 64,
                  "points": []}
        for point_id, text in points:
            input_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            source = next((point for point in prior["vector_families"][name]["points"]
                           if point["input_text_sha256"] == input_sha), None) if prior else None
            family["points"].append({"point_id": point_id, "input_text_sha256": input_sha,
                "vector_sha256": source["vector_sha256"] if source else hashlib.sha256((name + text).encode()).hexdigest(),
                "reuse_from": {"revision": parent, "collection": family["collection"], "point_id": source["point_id"]} if source else None})
        families[name] = family
    return families, recipe


def writer(root):
    root = Path(root)
    return p.revision_writer(root / "state.db", root / "chunks.db", root / "papers.db",
                             timeout=0.1, storage_admission_sha256=ADMISSION)


def crash(root, job, phase):
    with writer(root) as conn:
        def fault(at):
            if at == phase:
                os._exit(73)
        p.activate_revision(conn, root, job, ready, _fault=fault)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.projection("oldtoken", "old")
        self.new = self.projection("newtoken", "new")
        self.peer = self.projection("oldtoken peer", "peer")
        with database(self.root / "state.db") as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,title TEXT,"
                               "abstract TEXT,layers TEXT,year INTEGER,source_url TEXT);"
                               "CREATE TABLE completion_totals(first_completed INTEGER);"
                               "INSERT INTO completion_totals VALUES(7);")
            for paper, projection in (("p", self.old), ("peer", self.peer)):
                m = projection["metadata"]
                conn.execute("INSERT INTO papers VALUES(?,'done',?,?,?,?,?)", (paper, m["title"],
                    m["abstract"], m["layers"], m["year"], m["source_url"]))
            conn.commit()
            p.ensure_revision_schema(conn)
            families, recipe = vector_plan(self.old)
            artifact = admitted_artifact("p", self.old, families, recipe,
                {"original": "b" * 64, "vector_ownership": "e" * 64})
            self.base = hashlib.sha256(p._revision_bytes(artifact)).hexdigest()
            p._store(conn, self.root, "p", self.base, "revision", artifact, self.base)
            conn.execute("UPDATE paper_publications SET state='published'")
            conn.execute("INSERT INTO paper_active VALUES('p',?,1)", (self.base,))
            p._claim_revision_vectors(conn, "p", self.base, artifact, reserve=True)
            conn.commit()
            families, recipe = vector_plan(self.peer)
            peer = admitted_artifact("peer", self.peer, families, recipe,
                {"original": "b" * 64, "vector_ownership": "e" * 64})
            peer_revision = hashlib.sha256(p._revision_bytes(peer)).hexdigest()
            p._store(conn, self.root, "peer", peer_revision, "revision", peer, peer_revision)
            p._claim_revision_vectors(conn, "peer", peer_revision, peer, reserve=True)
        with database(self.root / "chunks.db") as conn:
            conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                         "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED,"
                         "tokenize='porter unicode61')")
            ensure_catalog(conn)
        with database(self.root / "papers.db") as conn:
            conn.executescript("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED,title,abstract,"
                               "layers UNINDEXED,year UNINDEXED,tokenize='porter unicode61');"
                               "CREATE TABLE pipeline_fts_papers(table_name TEXT NOT NULL,arxiv_id TEXT NOT NULL,"
                               "PRIMARY KEY(table_name,arxiv_id));"
                               "CREATE TABLE pipeline_fts_rows(table_name TEXT NOT NULL,arxiv_id TEXT NOT NULL,"
                               "row_id INTEGER NOT NULL,PRIMARY KEY(table_name,arxiv_id,row_id));")
        with writer(self.root) as conn:
            for paper, projection in (("p", self.old), ("peer", self.peer)):
                for schema, table in (("chunk_fts", "chunks"), ("paper_fts", "papers_fts")):
                    conn.execute(f"INSERT INTO {schema}.pipeline_fts_papers VALUES(?,?)", (table, paper))
                p._replace_revision_fts(conn, paper, projection, lambda _: None)
            conn.commit()
            self.job = self.prepare(conn, self.root, "p", self.new, expected_revision=self.base,
                expected_generation=1, artifact_bindings={"original": "b" * 64, "validated_chunks": "c" * 64})

    def projection(self, text, identity):
        return {"metadata": {"title": text, "abstract": text + " independent fixture abstract." * 5, "layers": "research", "year": 2026,
                             "source_url": "https://example.invalid/" + identity},
                "chunks": [{"point_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)), "text": text,
                            "section_type": "body", "element_type": "paragraph"}]}

    def prepare(self, conn, root, paper, projection, **kwargs):
        _, parent = p._load_revision(conn, root, paper, kwargs["expected_revision"])
        families, recipe = vector_plan(projection, parent, kwargs["expected_revision"])
        kwargs["artifact_bindings"] = {**kwargs["artifact_bindings"], "vector_ownership": "e" * 64}
        artifact = admitted_artifact(paper, projection, families, recipe, kwargs["artifact_bindings"],
            parent_revision=kwargs["expected_revision"], parent_generation=kwargs["expected_generation"])
        return stage_artifact(conn, root, artifact)

    def snapshot(self, conn):
        return (p._read_revision_projection(conn, "p"),
                tuple(conn.execute("SELECT revision,generation FROM paper_active WHERE arxiv_id='p'").fetchone()),
                conn.execute("SELECT epoch FROM publication_epoch").fetchone()[0],
                p._read_revision_projection(conn, "peer"),
                conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0])

    def assert_old(self, conn):
        self.assertEqual(self.snapshot(conn), (self.old, (self.base, 1), 0, self.peer, 7))
        self.assertEqual(conn.execute("SELECT state FROM paper_publications WHERE revision=?",
                                     (self.job["revision"],)).fetchone()[0], "staged")

    def activate(self, conn, **kwargs):
        return p.activate_revision(conn, self.root, self.job, ready, **kwargs)

    def test_staging_has_no_fts_or_bm25_effect(self):
        with writer(self.root) as conn:
            self.assert_old(conn)
            before = list(map(tuple, conn.execute("SELECT arxiv_id,bm25(chunks) FROM chunk_fts.chunks "
                                                 "WHERE chunks MATCH 'oldtoken' ORDER BY arxiv_id")))
            self.prepare(conn, self.root, "p", self.projection("nevervisible", "another"),
                expected_revision=self.base, expected_generation=1, artifact_bindings={"source": "d" * 64})
            after = list(map(tuple, conn.execute("SELECT arxiv_id,bm25(chunks) FROM chunk_fts.chunks "
                                                "WHERE chunks MATCH 'oldtoken' ORDER BY arxiv_id")))
            self.assertEqual(before, after)
            for schema, table in (("chunk_fts", "chunks"), ("paper_fts", "papers_fts")):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {schema}.{table} WHERE {table} MATCH 'newtoken OR nevervisible'").fetchone()[0], 0)

    def test_commit_and_reverse_rollback(self):
        with writer(self.root) as conn:
            result = self.activate(conn)
            self.assertEqual(result, {"revision": self.job["revision"], "generation": 2, "epoch": 1})
            self.assertEqual(self.snapshot(conn), (self.new, (self.job["revision"], 2), 1, self.peer, 7))
            for schema, table in (("chunk_fts", "chunks"), ("paper_fts", "papers_fts")):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {schema}.{table} WHERE {table} MATCH 'newtoken'").fetchone()[0], 1)
            result = p.rollback_revision(conn, self.root, "p", expected_revision=self.job["revision"],
                expected_generation=2, target_revision=self.base, qdrant_ready=ready)
            self.assertEqual(result, {"revision": self.base, "generation": 3, "epoch": 2})
            self.assertEqual(self.snapshot(conn), (self.old, (self.base, 3), 2, self.peer, 7))
            events = json.loads(conn.execute("SELECT completed FROM paper_publications WHERE revision=?", (self.base,)).fetchone()[0])
            self.assertEqual(events[-1]["operation"], "rollback")

    def test_qdrant_readiness_is_outside_transaction_and_bound(self):
        with writer(self.root) as conn:
            for field, value in (("verified", False), ("verified", 1), ("point_ids_sha256", "f" * 64),
                                 ("artifact_sha256", self.base), ("durable", False), ("revision_isolated", False)):
                def bad(job, artifact):
                    self.assertFalse(conn.in_transaction)
                    receipt = ready(job, artifact)
                    receipt[field] = value
                    return receipt
                with self.subTest(field=field, value=value), self.assertRaises(p.PublicationBlocked):
                    p.activate_revision(conn, self.root, self.job, bad)
                self.assert_old(conn)

    def test_failures_at_each_mutation_roll_back_every_database(self):
        with writer(self.root) as conn:
            for phase in ("after_chunk_fts", "after_paper_fts", "after_metadata", "before_commit"):
                def fault(at):
                    if at == phase:
                        raise RuntimeError(phase)
                with self.subTest(phase=phase), self.assertRaises(RuntimeError):
                    self.activate(conn, _fault=fault)
                self.assertFalse(conn.in_transaction)
                self.assert_old(conn)

    def test_reverse_failure_preserves_new_active(self):
        with writer(self.root) as conn:
            self.activate(conn)
            before = self.snapshot(conn)
            def fault(at):
                if at == "after_paper_fts":
                    raise RuntimeError("reverse failed")
            with self.assertRaises(RuntimeError):
                p.rollback_revision(conn, self.root, "p", expected_revision=self.job["revision"],
                    expected_generation=2, target_revision=self.base, qdrant_ready=ready, _fault=fault)
            self.assertEqual(self.snapshot(conn), before)

    def test_reader_sees_old_before_commit(self):
        def fault(at):
            if at == "before_commit":
                with writer(self.root) as reader:
                    self.assert_old(reader)
        with writer(self.root) as conn:
            self.activate(conn, _fault=fault)
            self.assertEqual(self.snapshot(conn)[0], self.new)

    def test_process_crash_before_commit_recovers_old(self):
        self.run_crash("before_commit")
        with writer(self.root) as conn:
            self.assert_old(conn)

    def test_process_crash_after_commit_retains_new(self):
        self.run_crash("after_commit")
        with writer(self.root) as conn:
            self.assertEqual(self.snapshot(conn), (self.new, (self.job["revision"], 2), 1, self.peer, 7))

    def run_crash(self, phase):
        code = "import json,sys; from pipeline.tests.test_publication_revision import crash; crash(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3])"
        result = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(self.job), phase],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", errors="replace"))

    def test_stale_candidate_and_invalid_reverse_are_rejected(self):
        with writer(self.root) as conn:
            other = self.prepare(conn, self.root, "p", self.projection("third", "third"),
                expected_revision=self.base, expected_generation=1, artifact_bindings={"source": "d" * 64})
            self.activate(conn)
            before = self.snapshot(conn)
            with self.assertRaises(p.PublicationBlocked):
                p.activate_revision(conn, self.root, other, ready)
            with self.assertRaises(p.PublicationBlocked):
                p.rollback_revision(conn, self.root, "p", expected_revision=self.job["revision"],
                    expected_generation=2, target_revision=other["revision"], qdrant_ready=ready)
            self.assertEqual(before, self.snapshot(conn))

    def test_wal_is_rejected_without_conversion(self):
        for name in ("state.db", "chunks.db", "papers.db"):
            with database(self.root / name) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
            with self.subTest(database=name), self.assertRaises(p.PublicationBlocked):
                with writer(self.root):
                    self.fail("WAL admitted")
            with database(self.root / name) as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                conn.execute("PRAGMA journal_mode=DELETE")

    def test_durability_change_is_rejected(self):
        with writer(self.root) as conn:
            conn.execute("PRAGMA paper_fts.synchronous=OFF")
            with self.assertRaises(p.PublicationBlocked):
                self.activate(conn)
            conn.execute("PRAGMA paper_fts.synchronous=FULL")
            self.assert_old(conn)

    def test_unknown_fts_and_triggers_are_rejected(self):
        with database(self.root / "papers.db") as conn:
            conn.execute("DROP TABLE papers_fts")
            conn.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED,title,abstract,layers UNINDEXED,year UNINDEXED)")
        with self.assertRaises(p.PublicationBlocked):
            with writer(self.root):
                self.fail("unknown tokenizer admitted")
        with database(self.root / "state.db") as conn:
            conn.execute("CREATE TRIGGER extra AFTER UPDATE ON papers BEGIN SELECT 1; END")
        with self.assertRaises(p.PublicationBlocked):
            with writer(self.root):
                self.fail("trigger admitted")

    def test_unowned_legacy_row_blocks_without_deletion(self):
        with writer(self.root) as conn:
            conn.execute("INSERT INTO chunk_fts.chunks(text,arxiv_id) VALUES('unowned','p')")
            conn.commit()
            with self.assertRaises(p.PublicationBlocked):
                self.activate(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM chunk_fts.chunks WHERE arxiv_id='p'").fetchone()[0], 2)

    def test_incomplete_global_ownership_blocks(self):
        with database(self.root / "chunks.db") as conn:
            conn.execute("UPDATE pipeline_fts_scope SET complete=0")
        with self.assertRaises(p.PublicationBlocked):
            with writer(self.root):
                self.fail("legacy scope admitted")

    def test_parent_uuid_reuse_blocks(self):
        with writer(self.root) as conn:
            candidate = self.projection("changed", "old")
            with self.assertRaises(p.PublicationBlocked):
                self.prepare(conn, self.root, "p", candidate, expected_revision=self.base,
                             expected_generation=1, artifact_bindings={"source": "e" * 64})
            self.assert_old(conn)

    def test_peer_uuid_collision_rolls_back(self):
        with writer(self.root) as conn:
            with self.assertRaises(p.PublicationBlocked):
                self.prepare(conn, self.root, "p", self.peer, expected_revision=self.base,
                             expected_generation=1, artifact_bindings={"source": "e" * 64})
            self.assert_old(conn)

    def test_artifact_tampering_blocks(self):
        Path(self.job["artifact_path"]).write_bytes(b"{}")
        with writer(self.root) as conn:
            with self.assertRaises(p.PublicationBlocked):
                self.activate(conn)
            self.assert_old(conn)

    def test_projection_validation_does_not_accept_bad_fields(self):
        with writer(self.root) as conn:
            for value in ([], {}, {"metadata": self.new["metadata"], "chunks": []},
                          {"metadata": {**self.new["metadata"], "year": True}, "chunks": self.new["chunks"]},
                          {"metadata": self.new["metadata"], "chunks": self.new["chunks"] * 2}):
                with self.subTest(value=value), self.assertRaises(p.PublicationBlocked):
                    p.prepare_revision(conn, self.root, "p", value, expected_revision=self.base,
                                       expected_generation=1, artifact_bindings={"source": "e" * 64},
                                       vector_families={}, coarse_recipe={})
            self.assert_old(conn)

    def test_memory_and_extra_attachment_block(self):
        with database(":memory:") as conn:
            with self.assertRaises(p.PublicationBlocked):
                p._revision_connection(conn)
        with writer(self.root) as conn:
            conn.execute("ATTACH ':memory:' AS extra")
            with self.assertRaises(p.PublicationBlocked):
                self.activate(conn)

    def test_alias_paths_and_missing_admission_block(self):
        with self.assertRaises(p.PublicationBlocked):
            with p.revision_writer(self.root / "state.db", self.root / "state.db", self.root / "papers.db",
                                   timeout=0.1, storage_admission_sha256=ADMISSION):
                self.fail("alias admitted")
        with self.assertRaises(p.PublicationBlocked):
            with p.revision_writer(self.root / "state.db", self.root / "chunks.db", self.root / "papers.db",
                                   timeout=float("nan"), storage_admission_sha256="unknown"):
                self.fail("unknown storage admitted")


if __name__ == "__main__":
    unittest.main()
