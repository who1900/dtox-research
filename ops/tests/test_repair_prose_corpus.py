"""Offline orchestration tests; all corpus writes use disposable fixtures."""

import argparse
import copy
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from ops import repair_prose_corpus as runner


def normal_child(connection, kwargs):
    connection.send_bytes(json.dumps({"status": "no_change", "paper_id": kwargs["paper_id"]}).encode())
    connection.close()


def slow_child(connection, kwargs):
    time.sleep(5)
    connection.close()


@contextlib.contextmanager
def fixture_db(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class FakeBackend:
    def __init__(self, papers):
        self.papers = papers

    def scroll(self, paper_id):
        return copy.deepcopy(self.papers[paper_id])


class FakeRepairBlocked(RuntimeError):
    def __init__(self, message, code="repair_invalid", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FakeEngine:
    RepairBlocked = FakeRepairBlocked

    def __init__(self):
        self.prepared = []
        self.applied = []
        self.failure = None

    def _load_journal(self, path):
        return json.loads(Path(path).read_bytes())

    def prepare_journal(self, paper_id, source, cache, additions, old_points, new_points,
                        path, collection):
        journal = {"paper_id": paper_id, "collection": collection,
                   "source_sha256": runner.sha(source), "cache_before_sha256": runner.sha(cache),
                   "additions": additions, "new_points": new_points}
        self.prepared.append(journal)
        with Path(path).open("x", encoding="utf-8") as stream:
            json.dump(journal, stream)
        return journal

    def apply_or_resume(self, path, source, cache, fts, backend, state_db_path, mode):
        self.applied.append(mode)
        if self.failure:
            if isinstance(self.failure, FakeRepairBlocked):
                raise self.failure
            raise FakeRepairBlocked(self.failure)
        journal = self._load_journal(path)
        cache_path = Path(cache)
        if runner.sha(cache_path.read_bytes()) == journal["cache_before_sha256"]:
            chunks = json.loads(cache_path.read_bytes())
            cache_path.write_text(json.dumps(chunks + journal["additions"]), encoding="utf-8")
        return {"status": "done", "repair_id": "fixture", "fts_rows": []}


class CorpusRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "corpus"
        self.root.mkdir()
        self.ledger = Path(self.temp.name) / "private" / "ledger.sqlite"
        self.journals = Path(self.temp.name) / "private" / "journals"
        with fixture_db(self.root / "state.db") as state:
            state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,fulltext_source TEXT)")
            state.execute("CREATE TABLE paper_index_sync(arxiv_id TEXT PRIMARY KEY)")
        self.args = argparse.Namespace(root=self.root, ledger=self.ledger, journal_dir=self.journals,
                                       mode="scan", limit=10, max_seconds=60.0, pause=0.0,
                                       apply_limit=2, worker_hours=24.0, cycle_pause=5.0,
                                       qdrant_url="http://127.0.0.1:6333")
        self.engine = FakeEngine()
        self.old_points = {}
        self.embedded = []

    def paper(self, paper_id, source=True, cache=True, kind="latex"):
        with fixture_db(self.root / "state.db") as state:
            state.execute("INSERT INTO papers VALUES (?,?,?)", (paper_id, "done", kind))
        source_path, cache_path = runner.paper_paths(self.root, paper_id)
        if source:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("cached TeX " + paper_id, encoding="utf-8")
        if cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps([{"chunk_index": 0, "section_title": "Discussion",
                                               "section_type": "analysis", "element_type": "figure",
                                               "text": "old structured text"}]), encoding="utf-8")
        self.old_points[paper_id] = [{"id": "old-" + paper_id, "vector": [0.1] * 384,
                                    "payload": {"arxiv_id": paper_id, "chunk_index": 0,
                                                "title": "Original title", "layers": ["llm-slm"],
                                                "year": 2024, "section_title": "Discussion",
                                                "section_type": "analysis", "element_type": "figure",
                                                "text": "old structured text", "has_math": True}}]

    def planner(self, **kwargs):
        self.assertIsNone(kwargs["section"])
        return {"status": "dry_run_only", "parser_mode": "pylatexenc_recovered",
                "blockers": ["apply needs supplied vectors"],
                "additions": [{"chunk_index": 1, "section_title": "Discussion",
                               "section_type": "analysis", "element_type": "prose",
                               "text": "New recovered prose."}]}

    def embed(self, texts, budget):
        self.embedded.append(texts)
        return [[0.2] * 384 for _ in texts]

    def run_corpus(self, mode=None, planner=None, embedder=None):
        args = copy.copy(self.args)
        args.mode = mode or args.mode
        return runner.run(args, planner=planner or self.planner, engine=self.engine,
                          backend=FakeBackend(self.old_points), embedder=embedder or self.embed,
                          gate=lambda: None)

    def rows(self):
        with fixture_db(self.ledger) as conn:
            conn.row_factory = sqlite3.Row
            return {row["paper_id"]: dict(row) for row in conn.execute("SELECT * FROM entries")}

    def due(self):
        with fixture_db(self.ledger) as conn:
            conn.execute("UPDATE entries SET retry_after=0")

    def test_cursor_durable_across_small_invocations_and_status_is_readonly(self):
        self.paper("a")
        self.paper("b")
        self.args.limit = 1
        first = self.run_corpus()
        self.assertEqual(first["scan_cursor"], "a")
        self.assertFalse(first["frontier_complete"])
        second = self.run_corpus()
        self.assertEqual(second["scan_cursor"], "b")
        self.assertTrue(second["frontier_complete"])
        before = self.ledger.read_bytes()
        self.assertEqual(self.run_corpus("status")["counts"], {"pending": 2})
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.embedded, [])

    def test_missing_source_cache_and_outside_latex_are_distinct(self):
        self.paper("a", source=False)
        self.paper("b", cache=False)
        self.paper("c", kind="pdf")
        self.paper("d")
        def no_change(**kwargs):
            return {"status": "no_change", "parser_mode": "pylatexenc_recovered"}
        result = self.run_corpus(planner=no_change)
        self.assertEqual(result["counts"], {"skipped_missing_source": 1,
                                           "skipped_missing_cache": 1,
                                           "skipped_outside_latex": 1, "no_change": 1})
        self.assertEqual(result["scan_cursor"], "d")

    def test_plan_status_governs_not_explanatory_blockers_or_recovered_flag(self):
        self.paper("a")
        self.paper("b")
        def plans(**kwargs):
            if kwargs["paper_id"] == "a":
                return self.planner(**kwargs)
            return {"status": "blocked", "additions": [], "blockers": ["ambiguous heading"]}
        self.run_corpus(planner=plans)
        self.assertEqual(self.rows()["a"]["state"], "pending")
        self.assertEqual(self.rows()["b"]["state"], "blocked")
        self.run_corpus("apply")
        self.assertEqual(len(self.engine.prepared), 1)

    def test_only_new_prose_embedded_metadata_preserved_and_apply_is_idempotent(self):
        self.paper("a")
        self.run_corpus()
        self.run_corpus("apply")
        self.run_corpus("apply")
        self.assertEqual(self.embedded, [["New recovered prose."]])
        self.assertEqual(len(self.engine.prepared), 1)
        self.assertEqual(self.engine.applied, ["apply"])
        point = self.engine.prepared[0]["new_points"][0]
        self.assertEqual(point["id"], str(runner.uuid.uuid5(runner.uuid.NAMESPACE_URL, "a#1")))
        self.assertEqual(point["payload"]["title"], "Original title")
        self.assertEqual(point["payload"]["layers"], ["llm-slm"])
        self.assertFalse(point["payload"]["has_math"])
        self.assertEqual(self.rows()["a"]["state"], "applied")

    def test_resume_existing_journal_before_embedding_or_second_prepare(self):
        self.paper("a")
        self.run_corpus()
        self.engine.failure = FakeRepairBlocked("time budget", "repair_timeout", True)
        self.run_corpus("apply")
        self.due()
        self.engine.failure = None
        self.run_corpus("apply")
        self.assertEqual(self.engine.applied, ["apply", "resume"])
        self.assertEqual(len(self.embedded), 1)
        self.assertEqual(len(self.engine.prepared), 1)

    def test_typed_engine_failure_retries_independently_of_message_language(self):
        self.paper("a")
        self.run_corpus()
        message = "Временная задержка backend"
        self.engine.failure = FakeRepairBlocked(message, "backend_timeout", True)
        self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "retry")
        self.assertEqual(self.rows()["a"]["reason"], message)
        self.due()
        self.engine.failure = None
        self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "applied")
        self.assertEqual(self.engine.applied, ["apply", "resume"])
        self.assertEqual(len(self.embedded), 1)
        self.assertEqual(len(self.engine.prepared), 1)

    def test_hash_race_blocks_before_journal_or_mutation(self):
        self.paper("a")
        self.run_corpus()
        def raced_embed(texts, budget):
            _, cache = runner.paper_paths(self.root, "a")
            cache.write_bytes(cache.read_bytes() + b" ")
            return self.embed(texts, budget)
        self.run_corpus("apply", embedder=raced_embed)
        self.assertEqual(self.rows()["a"]["state"], "blocked")
        self.assertEqual(self.engine.prepared, [])
        self.assertEqual(self.engine.applied, [])

    def test_pending_sync_is_retry_and_does_not_embed(self):
        self.paper("a")
        self.run_corpus()
        with fixture_db(self.root / "state.db") as state:
            state.execute("INSERT INTO paper_index_sync VALUES ('a')")
        self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "retry")
        self.assertEqual(self.embedded, [])

    def test_partial_journal_retry_cap_requires_attention_and_manual_resume_can_continue(self):
        self.paper("a")
        self.run_corpus()
        self.engine.failure = FakeRepairBlocked("FTS revision conflict", "fts_revision_conflict", True)
        for _ in range(3):
            self.due()
            self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "manual_attention")
        self.assertEqual(len(self.engine.prepared), 1)
        self.assertEqual(len(self.embedded), 1)
        self.run_corpus("cycle")
        self.assertEqual(len(self.engine.applied), 3)
        self.engine.failure = None
        self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "applied")
        self.assertEqual(self.engine.applied[-1], "resume")

    def test_scan_retries_increment_and_do_not_starve_fresh_cursor(self):
        for paper_id in ("a", "b", "c", "d", "e"):
            self.paper(paper_id)
        self.args.limit = 3
        def planner(**kwargs):
            if kwargs["paper_id"] == "a":
                raise runner.Retryable("fixture transient parse failure")
            return {"status": "no_change"}
        self.run_corpus(planner=planner)
        self.due()
        result = self.run_corpus(planner=planner)
        self.assertEqual(result["scan_cursor"], "e")
        self.assertEqual(self.rows()["a"]["attempts"], 2)
        self.due()
        self.run_corpus(planner=planner)
        self.assertEqual(self.rows()["a"]["state"], "retry_exhausted")

    def test_hard_parser_timeout_is_persisted_and_cursor_continues(self):
        self.paper("a")
        self.paper("b")
        def planner(**kwargs):
            target = slow_child if kwargs["paper_id"] == "a" else normal_child
            return runner.bounded_plan(runner.Budget(10), worker=target,
                                       timeout=0.3 if target == slow_child else 5, **kwargs)
        result = self.run_corpus(planner=planner)
        self.assertEqual(result["scan_cursor"], "b")
        self.assertEqual(self.rows()["a"]["state"], "blocked")
        self.assertEqual(self.rows()["a"]["reason"], "parser_timeout")
        self.assertEqual(self.rows()["b"]["state"], "no_change")

    def test_mode_guards_and_scan_status_never_call_loopback_http(self):
        self.paper("a")
        with patch.object(runner, "http_json", side_effect=AssertionError("HTTP forbidden")):
            self.run_corpus()
            self.run_corpus("status")
        for mode in ("invalid",):
            with self.assertRaises(runner.Blocked):
                self.run_corpus(mode)
        self.args.max_seconds = 61
        with self.assertRaises(runner.Blocked):
            self.run_corpus()

    def test_worker_stops_when_frontier_drained_without_relabeling_blocked(self):
        self.paper("a")
        def planner(**kwargs):
            return {"status": "blocked", "blockers": ["fixture ambiguity"]}
        with patch("builtins.print") as printed:
            result = self.run_corpus("worker", planner=planner)
        self.assertTrue(printed.call_args.kwargs["flush"])
        self.assertEqual(result["reason"], "frontier_drained_with_unresolved")
        self.assertEqual(result["last_cycle"]["counts"], {"blocked": 1})
        self.assertEqual(self.engine.applied, [])

    def test_paths_support_validated_old_arxiv_and_reject_escape_or_ambiguity(self):
        source = self.root / "latex_cache" / "hep-th" / "9901001" / "source.tex"
        source.parent.mkdir(parents=True)
        source.write_text("fixture")
        self.assertEqual(runner.paper_paths(self.root, "hep-th/9901001")[0], source)
        for paper_id in ("../secret", "x/../y", "/absolute", "iacr:2020/123"):
            with self.assertRaises(runner.Blocked):
                runner.paper_paths(self.root, paper_id)
        flat = self.root / "latex_cache" / "hep-th_9901001" / "source.tex"
        flat.parent.mkdir(parents=True)
        flat.write_text("fixture")
        with self.assertRaises(runner.Blocked):
            runner.paper_paths(self.root, "hep-th/9901001")

    def test_footprint_cap_stops_before_cursor_moves(self):
        self.paper("a")
        with patch.object(runner, "MAX_LEDGER_BYTES", 1):
            with self.assertRaises(runner.Blocked):
                self.run_corpus()
        self.assertEqual(self.run_corpus("status")["scan_cursor"], "")

    def test_existing_corpus_database_cannot_be_used_as_ledger(self):
        self.args.ledger = self.root / "state.db"
        with self.assertRaises(runner.Blocked):
            self.run_corpus()

    def test_initial_universe_is_frozen_and_later_done_ids_are_explicitly_outside_scope(self):
        self.paper("a")
        self.paper("b")
        self.args.limit = 1
        first = self.run_corpus()
        self.paper("0")
        self.paper("z")
        second = self.run_corpus()
        self.assertEqual(first["universe_count"], 2)
        self.assertEqual(second["universe_frozen"], first["universe_frozen"])
        self.assertTrue(second["frontier_complete"])
        self.assertEqual(set(self.rows()), {"a", "b"})

    def test_applied_partial_retains_unresolved_sections_and_requires_frontier_attention(self):
        self.paper("a")
        def planner(**kwargs):
            plan = self.planner(**kwargs)
            plan.update(partial_scope=True, unresolved_sections=[{"section": "Methods", "reason": "parse failure"}])
            return plan
        self.run_corpus(planner=planner)
        self.run_corpus("apply")
        row = self.rows()["a"]
        self.assertEqual(row["state"], "applied_partial")
        self.assertEqual(json.loads(row["reason"])["unresolved_sections"][0]["section"], "Methods")
        with patch("builtins.print"):
            result = self.run_corpus("worker")
        self.assertEqual(result["reason"], "frontier_drained_with_unresolved")
        self.assertEqual(result["unresolved_count"], 1)

    def test_health_gate_does_not_consume_apply_attempts(self):
        self.paper("a")
        self.run_corpus()
        args = copy.copy(self.args)
        args.mode = "apply"
        def unhealthy():
            raise runner.Retryable("host load gate is closed")
        with self.assertRaises(runner.Retryable):
            runner.run(args, engine=self.engine, backend=FakeBackend(self.old_points), gate=unhealthy)
        self.assertEqual(self.rows()["a"]["attempts"], 0)

    def test_fts_interruption_retries_and_permanent_failure_with_journal_requires_attention(self):
        self.paper("a")
        self.run_corpus()
        error = sqlite3.OperationalError("interrupted")
        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        with patch.object(self.engine, "apply_or_resume", side_effect=error):
            self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "retry")
        self.due()
        self.engine.failure = "immutable digest mismatch"
        self.run_corpus("apply")
        self.assertEqual(self.rows()["a"]["state"], "manual_attention")
        self.assertEqual(len(self.embedded), 1)

    def test_unsupported_engine_ids_are_recorded_without_parsing_or_embedding(self):
        self.paper("oa:W123")
        self.paper("hep-th/9901001")
        result = self.run_corpus(planner=lambda **kwargs: self.fail("unsupported ID was parsed"))
        self.assertEqual(result["counts"], {"skipped_unsupported_id": 2})
        self.run_corpus("apply")
        self.assertEqual(self.embedded, [])

    def test_symlinked_subtree_is_rejected_before_file_reads(self):
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path.name == "latex_cache"):
            with self.assertRaises(runner.Blocked):
                runner.paper_paths(self.root, "a")

    def test_cycle_drains_large_backlog_without_new_scan(self):
        for index in range(20):
            self.paper(f"a{index:02}")
        self.args.limit = 20
        self.args.apply_limit = 3
        self.run_corpus()
        result = self.run_corpus("cycle")
        self.assertTrue(result["drain_backlog"])
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["counts"], {"applied": 3, "pending": 17})

    def test_cli_worker_exit_codes_are_restartable_only_for_worker_time_limit(self):
        argv = ["--mode", "worker", "--ledger", str(self.ledger), "--journal-dir", str(self.journals)]
        cases = (({"reason": "worker_time_limit"}, 75),
                 ({"reason": "frontier_drained"}, 0),
                 ({"reason": "frontier_drained_with_unresolved", "unresolved_count": 1}, 2),
                 ({"status": "blocked"}, 2))
        for result, expected in cases:
            with patch.object(runner, "run", return_value=result), patch("builtins.print"):
                self.assertEqual(runner.main(argv), expected)


class GateTests(unittest.TestCase):
    def test_backend_http_preserves_engine_snapshot_and_operation_deadlines(self):
        class Base:
            def __init__(self, url, collection, timeout):
                self.base_url, self.timeout = url, timeout
        engine = types.SimpleNamespace(QdrantHTTP=Base)
        backend = runner.make_backend(engine, "http://127.0.0.1:6333", runner.Budget(10))
        backend._operation_deadline = time.monotonic() + 3
        backend._snapshot_deadline = time.monotonic() + 1
        with patch.object(runner, "http_json", return_value={"status": "ok"}) as request:
            backend._request("GET", "/collections/papers_fulltext")
        self.assertEqual(request.call_args.kwargs["request_deadline"], backend._snapshot_deadline)
        self.assertLessEqual(request.call_args.kwargs["max_timeout"], 5)

    def test_current_live_resource_shape_passes_memory_and_per_core_load_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = types.SimpleNamespace(total=100 * 1024 ** 3, free=38 * 1024 ** 3)
            with (patch.object(runner.sys, "platform", "linux"),
                  patch.object(runner.os, "getloadavg", return_value=(4.1, 4.0, 4.0), create=True),
                  patch.object(runner.os, "cpu_count", return_value=8),
                  patch.object(Path, "read_text", return_value="MemAvailable: 1700000 kB\nSwapFree: 0 kB"),
                  patch.object(runner.shutil, "disk_usage", return_value=usage)):
                runner.local_health(root, root / "ledger", root)
                with patch.object(Path, "read_text", return_value="MemAvailable: 260000 kB"):
                    with self.assertRaises(runner.Retryable):
                        runner.local_health(root, root / "ledger", root)

    def test_embedding_endpoint_and_vector_shape_fail_closed(self):
        with self.assertRaises(runner.Blocked):
            runner.http_json("GET", "http://example.com", runner.Budget(1))
        with patch.object(runner, "http_json", return_value={"vectors": [[True] * 384]}):
            with self.assertRaises(runner.Blocked):
                runner.embed_new(["only new prose"], runner.Budget(1))
        with patch.object(runner, "http_json", return_value={"vectors": [[0.1] * 384]}) as request:
            runner.embed_new(["only new prose"], runner.Budget(1))
            self.assertEqual(request.call_args.args[1], "http://127.0.0.1:8006/embed_batch")


if __name__ == "__main__":
    unittest.main()
