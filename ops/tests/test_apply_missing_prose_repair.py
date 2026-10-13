import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ops import apply_missing_prose_repair as repair


class SimulatedCrash(BaseException):
    pass


class FakeQdrant:
    collection = "papers_fulltext"

    def __init__(self, points):
        self.points = {str(point["id"]): json.loads(json.dumps(point)) for point in points}

    def scroll(self, paper_id):
        return [json.loads(json.dumps(p)) for p in self.points.values()
                if p["payload"].get("arxiv_id") == paper_id]

    def retrieve(self, point_ids):
        return [json.loads(json.dumps(self.points[str(pid)]))
                for pid in point_ids if str(pid) in self.points]

    def upsert_additions(self, points):
        for point in points:
            point_id = str(point["id"])
            if point_id in self.points:
                raise AssertionError("test backend must not overwrite IDs")
            self.points[point_id] = json.loads(json.dumps(point))


class AdditiveRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source.tex"
        self.source = b"cached source fixture"
        self.source_path.write_bytes(self.source)
        self.cache_path = self.root / "chunks.json"
        self.fts_path = self.root / "fts.db"
        self.state_path = self.root / "state.db"
        self.journal_path = self.root / "repair.json"
        self.old_chunks = []
        for index in range(77):
            if index == 53:
                section, kind, element = "Discussions", "analysis", "figure"
            elif index >= 72:
                section, kind, element = "Appendix", "appendix", "figure"
            elif index == 10:
                section, kind, element = "Method", "method", "equation"
            elif index == 11:
                section, kind, element = "Method", "method", "algorithm"
            elif index == 12:
                section, kind, element = "Method", "method", "code"
            else:
                section, kind, element = "Method", "method", "prose"
            self.old_chunks.append({"chunk_index": index, "section_title": section,
                                   "section_type": kind, "element_type": element,
                                   "text": f"old chunk {index}", "has_math": False,
                                   "has_algorithm": False})
        self.old_cache = json.dumps(self.old_chunks, ensure_ascii=False).encode("utf-8")
        self.cache_path.write_bytes(self.old_cache)
        self.additions = [{"chunk_index": 77, "section_title": "Discussions",
                           "section_type": "analysis", "element_type": "prose",
                           "text": "Recovered limitation prose restores missing discussion passage.", "has_math": False,
                           "has_algorithm": False}]
        self.old_points = [self.point(chunk) for chunk in self.old_chunks if chunk["chunk_index"] < 72]
        self.new_points = [self.point(self.additions[0])]
        self.backend = FakeQdrant(self.old_points)
        conn = sqlite3.connect(self.fts_path)
        conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                     "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
        conn.execute("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('legacy old row','legacy-id','2404.14469')")
        conn.commit()
        conn.close()
        state = sqlite3.connect(self.state_path)
        state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT NOT NULL)")
        state.execute("CREATE TABLE paper_index_sync(arxiv_id TEXT PRIMARY KEY,revision TEXT NOT NULL,"
                      "retry_after REAL NOT NULL DEFAULT 0)")
        state.execute("INSERT INTO papers VALUES ('2404.14469','done')")
        state.commit()
        state.close()
        repair.prepare_journal("2404.14469", self.source, self.old_cache, self.additions,
                               self.old_points, self.new_points, self.journal_path)

    def tearDown(self):
        self.temp.cleanup()

    def point(self, chunk):
        index = chunk["chunk_index"]
        return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"2404.14469#{index}")),
                "vector": [0.01 * (index + 1)] * 384,
                "payload": {"arxiv_id": "2404.14469", "chunk_index": index,
                            "text": chunk["text"], "section_type": chunk["section_type"],
                            "element_type": chunk["element_type"], "layers": ["research"], "year": 2024}}

    def apply(self, mode="apply", fault_hook=None):
        return repair.apply_or_resume(self.journal_path, self.source_path, self.cache_path,
                                      self.fts_path, self.backend, self.state_path, mode, fault_hook)

    def record_completed_fixture(self):
        journal = repair._load_journal(self.journal_path)
        with closing(sqlite3.connect(self.fts_path)) as conn, conn:
            conn.execute("CREATE TABLE dtox_prose_repair_receipts "
                         "(repair_id TEXT PRIMARY KEY,digest TEXT NOT NULL,row_ids_json TEXT NOT NULL)")
            records = []
            for point in journal["new_points"]:
                row = conn.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) "
                                   "VALUES (?,?,?,?,?,?,?)", repair._fts_expected_row(point))
                records.append({"row_id": row.lastrowid, "point_id": point["id"],
                                "text_sha256": repair._sha(point["payload"]["text"].encode())})
            conn.execute("INSERT INTO dtox_prose_repair_receipts VALUES (?,?,?)",
                         (journal["repair_id"], journal["digest"], json.dumps(records)))
        self.cache_path.write_bytes(repair._new_cache_bytes(self.old_cache, self.additions))
        self.backend.points.update({str(p["id"]): json.loads(json.dumps(p)) for p in self.new_points})
        journal["stage"] = "done"
        journal["fts_row_ids"] = records
        repair._atomic_json(self.journal_path, journal)
        return journal

    def test_all_incomplete_stages_refuse_without_side_effects(self):
        for stage in ("prepared", "qdrant_applied", "cache_applied", "fts_applied"):
            with self.subTest(stage=stage):
                journal = repair._load_journal(self.journal_path)
                journal["stage"] = stage
                repair._atomic_json(self.journal_path, journal)
                before = self.journal_path.read_bytes()
                with patch.object(self.backend, "scroll", side_effect=AssertionError("no network")):
                    with self.assertRaises(repair.RepairBlocked) as caught:
                        self.apply("resume")
                self.assertEqual(caught.exception.code, "fts_identity_unknown")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(self.journal_path.read_bytes(), before)
                self.assertEqual(self.cache_path.read_bytes(), self.old_cache)
                self.assertNotIn(self.new_points[0]["id"], self.backend.points)

    def test_old_qdrant_baseline_change_blocks_without_writes(self):
        self.record_completed_fixture()
        before = self.cache_path.read_bytes()
        point_id = str(self.old_points[0]["id"])
        self.backend.points[point_id]["payload"]["text"] = "changed concurrently"
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.assertEqual(self.cache_path.read_bytes(), before)

    def test_new_uuid_collision_blocks_instead_of_overwrite(self):
        self.record_completed_fixture()
        before = self.cache_path.read_bytes()
        collision = dict(self.new_points[0], payload={**self.new_points[0]["payload"], "arxiv_id": "other"})
        self.backend.points[str(collision["id"])] = collision
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.assertEqual(self.cache_path.read_bytes(), before)

    def test_source_and_cache_hash_changes_fail_closed(self):
        self.record_completed_fixture()
        completed_cache = self.cache_path.read_bytes()
        self.source_path.write_bytes(self.source + b" changed")
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.source_path.write_bytes(self.source)
        self.cache_path.write_bytes(completed_cache + b" ")
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.assertEqual(self.backend.points[self.new_points[0]["id"]], self.new_points[0])

    def test_pending_index_sync_blocks_before_qdrant_or_cache_mutation(self):
        self.record_completed_fixture()
        before = self.cache_path.read_bytes()
        state = sqlite3.connect(self.state_path)
        state.execute("INSERT INTO paper_index_sync VALUES ('2404.14469','rev',0)")
        state.commit()
        state.close()
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.assertEqual(self.cache_path.read_bytes(), before)

    def test_receipt_digest_mismatch_blocks_and_preserves_legacy_rows(self):
        journal = repair._load_journal(self.journal_path)
        conn = sqlite3.connect(self.fts_path)
        conn.execute("CREATE TABLE dtox_prose_repair_receipts "
                     "(repair_id TEXT PRIMARY KEY,digest TEXT NOT NULL,row_ids_json TEXT NOT NULL)")
        conn.execute("INSERT INTO dtox_prose_repair_receipts VALUES (?,?,?)",
                     (journal["repair_id"], "wrong-digest", "[]"))
        conn.commit()
        conn.close()
        with self.assertRaises(repair.RepairBlocked):
            repair._fts_additions(self.fts_path, journal)
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT text FROM chunks WHERE point_id='legacy-id'").fetchone()[0],
                         "legacy old row")
        conn.close()

    def test_unreceipted_fts_uuid_or_exact_text_collision_blocks_insert(self):
        conn = sqlite3.connect(self.fts_path)
        point = self.new_points[0]
        conn.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) "
                     "VALUES (?,?,?,?,?,?,?)", repair._fts_expected_row(point))
        conn.commit()
        conn.close()
        journal = repair._load_journal(self.journal_path)
        with self.assertRaises(repair.RepairBlocked):
            repair._fts_additions(self.fts_path, journal)
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 2)
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master "
                                       "WHERE name='dtox_prose_repair_receipts'").fetchone())
        conn.close()

    def test_existing_legacy_sidecatalog_does_not_authorize_new_writes(self):
        conn = sqlite3.connect(self.fts_path)
        conn.execute("CREATE TABLE pipeline_fts_papers(table_name TEXT,arxiv_id TEXT,"
                     "PRIMARY KEY(table_name,arxiv_id))")
        conn.execute("CREATE TABLE pipeline_fts_rows(table_name TEXT,arxiv_id TEXT,row_id INTEGER,"
                     "PRIMARY KEY(table_name,row_id))")
        conn.commit()
        conn.close()
        with self.assertRaises(repair.RepairBlocked) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "fts_identity_unknown")
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT table_name,arxiv_id FROM pipeline_fts_rows").fetchall(), [])
        self.assertIsNone(conn.execute("SELECT 1 FROM pipeline_fts_papers WHERE arxiv_id='2404.14469'").fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='dtox_prose_repair_receipts'").fetchone())
        conn.close()

    def test_dryrun_preparation_does_not_create_journal(self):
        path = self.root / "dryrun.json"
        result = repair.prepare_journal("2404.14469", self.source, self.old_cache, self.additions,
                                        self.old_points, self.new_points, path, write=False)
        self.assertEqual(result["stage"], "prepared")
        self.assertFalse(path.exists())

    def test_bad_prepared_uuid_and_remote_url_are_rejected(self):
        bad = [dict(self.new_points[0], id="not-the-uuid5")]
        with self.assertRaises(repair.RepairBlocked):
            repair.prepare_journal("2404.14469", self.source, self.old_cache, self.additions,
                                   self.old_points, bad, self.root / "bad.json")
        for url in ("https://example.com", "http://user:pass@127.0.0.1:6333"):
            with self.subTest(url=url), self.assertRaises(repair.RepairBlocked):
                repair.validate_loopback_url(url)

    def test_missing_cached_structured_point_blocks_preparation(self):
        missing_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "2404.14469#10"))
        points = [point for point in self.old_points if str(point["id"]) != missing_id]
        with self.assertRaises(repair.RepairBlocked):
            repair.prepare_journal("2404.14469", self.source, self.old_cache, self.additions,
                                   points, self.new_points, self.root / "missing.json")


    def generic_snapshot(self, paper_id="2501.12345v2", count=240):
        chunks = [dict(self.old_chunks[i % len(self.old_chunks)], chunk_index=i,
                       text=f"baseline paragraph {i}") for i in range(count)]
        additions = [dict(self.additions[0], chunk_index=count, section_title="Method",
                          section_type="method", text="Recovered methodology explains bounded numerical estimation steps."),
                     dict(self.additions[0], chunk_index=count + 1, section_title="Threats to validity",
                          section_type="limitations", has_math=True,
                          text="Missing validity paragraph preserves embedded formula $x^2+y^2$ unchanged.")]

        def point(chunk):
            return {"id": repair._point_id(paper_id, chunk["chunk_index"]), "vector": [0.25] * 384,
                    "payload": {"arxiv_id": paper_id, "chunk_index": chunk["chunk_index"],
                                "section_title": chunk["section_title"], "section_type": chunk["section_type"],
                                "element_type": chunk["element_type"], "text": chunk["text"]}}

        return chunks, additions, [point(c) for c in chunks], [point(c) for c in additions]

    def test_generic_partial_mixed_sections_are_refused_without_mutation(self):
        paper_id = "2501.12345v2"
        chunks, additions, old_points, new_points = self.generic_snapshot(paper_id)
        old_cache = json.dumps(chunks).encode()
        self.cache_path.write_bytes(old_cache)
        state = sqlite3.connect(self.state_path)
        state.execute("INSERT INTO papers VALUES (?, 'done')", (paper_id,))
        state.commit()
        state.close()
        path = self.root / "generic.json"
        repair.prepare_journal(paper_id, self.source, old_cache, additions, old_points, new_points, path)
        backend = FakeQdrant(old_points)
        before = json.loads(json.dumps(backend.points))
        for mode in ("apply", "resume", "resume"):
            with self.assertRaises(repair.RepairBlocked) as caught:
                repair.apply_or_resume(path, self.source_path, self.cache_path, self.fts_path,
                                       backend, self.state_path, mode)
            self.assertEqual(caught.exception.code, "fts_identity_unknown")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), chunks)
        for point_id, point in before.items():
            self.assertEqual(backend.points[point_id], point)
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks WHERE arxiv_id=?", (paper_id,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT text FROM chunks WHERE point_id='legacy-id'").fetchone()[0], "legacy old row")
        conn.close()

    def test_generic_paper_ids_are_safe_and_not_allowlisted(self):
        for paper_id in ("2501.12345", "2501.12345v2", "eip-1559", "RFC_9110", "paper-Alpha.1"):
            with self.subTest(paper_id=paper_id):
                repair._allowed(paper_id, "papers_fulltext")
        for paper_id in (None, 5, "", "../paper", "paper..1", "paper/1", "paper\\1", "/paper",
                         "C:paper", "paper%2f1", "paper\x00", "paper\n", "paper.", "CON", "nul.json", "x" * 129):
            with self.subTest(paper_id=paper_id), self.assertRaises(repair.RepairBlocked):
                repair._allowed(paper_id, "papers_fulltext")

    def test_generic_invalid_metadata_indices_and_duplicate_text_block(self):
        chunks, additions, old_points, new_points = self.generic_snapshot()
        invalid = [{"chunk_index": True}, {"chunk_index": 239}, {"element_type": "equation"},
                   {"section_title": " "}, {"section_title": "bad\nheading"}, {"section_title": 9},
                   {"section_type": "unknown"}, {"section_type": []}, {"text": 9}, {"text": " "},
                   {"has_math": 1}, {"has_algorithm": True}, {"arxiv_id": "other"},
                   {"text": "  BASELINE   paragraph 1 "}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(repair.RepairBlocked):
                repair.prepare_journal("2501.12345v2", self.source, json.dumps(chunks).encode(),
                                       [dict(additions[0], **changes), additions[1]], old_points, new_points,
                                       self.root / "invalid.json", write=False)
        with self.assertRaises(repair.RepairBlocked):
            repair.prepare_journal("2501.12345v2", self.source, json.dumps(chunks).encode(),
                                   [additions[0], dict(additions[1], text=additions[0]["text"])],
                                   old_points, new_points, self.root / "duplicate.json", write=False)
        points = json.loads(json.dumps(new_points))
        points[0]["payload"]["section_title"] = "Wrong heading"
        with self.assertRaises(repair.RepairBlocked):
            repair.prepare_journal("2501.12345v2", self.source, json.dumps(chunks).encode(),
                                   additions, old_points, points, self.root / "wrong-title.json", write=False)

    def test_network_runs_without_state_write_lock_and_late_sync_blocks_cache(self):
        self.record_completed_fixture()
        before = self.cache_path.read_bytes()
        original_scroll = self.backend.scroll

        def unlocked_scroll(paper_id):
            conn = sqlite3.connect(self.state_path, timeout=0)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO paper_index_sync VALUES ('2404.14469','queued',0)")
            conn.commit()
            conn.close()
            return original_scroll(paper_id)

        self.backend.scroll = unlocked_scroll
        with self.assertRaises(repair.RepairBlocked):
            self.apply("resume")
        self.assertEqual(self.cache_path.read_bytes(), before)
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 2)
        conn.close()
        state = sqlite3.connect(self.state_path)
        state.execute("DELETE FROM paper_index_sync")
        state.commit()
        state.close()
        self.backend.scroll = original_scroll
        self.assertEqual(self.apply("resume")["status"], "done")


    def test_fts_collision_blocks_before_any_qdrant_or_cache_effect(self):
        conn = sqlite3.connect(self.fts_path)
        conn.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year) "
                     "VALUES (?,?,?,?,?,?,?)", repair._fts_expected_row(self.new_points[0]))
        conn.commit()
        conn.close()
        with patch.object(self.backend, "scroll", side_effect=AssertionError("no network before clean preflight")):
            with self.assertRaises(repair.RepairBlocked):
                self.apply()
        self.assertEqual(self.cache_path.read_bytes(), self.old_cache)
        self.assertNotIn(self.new_points[0]["id"], self.backend.points)
        self.assertEqual(repair._load_journal(self.journal_path)["stage"], "prepared")

    def test_done_receipted_resume_is_read_only_and_does_not_claim_legacy_fts(self):
        self.record_completed_fixture()
        before_cache = self.cache_path.read_bytes()
        before_journal = self.journal_path.read_bytes()
        check_receipt = repair._receipt_rows

        def readonly_receipt(conn, journal):
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE must_not_write(value)")
            return check_receipt(conn, journal)

        with patch.object(repair, "_receipt_rows", side_effect=readonly_receipt), \
                patch.object(self.backend, "upsert_additions", side_effect=AssertionError("no writes")):
            result = self.apply("resume")
            self.assertEqual(result["status"], "done")
            self.assertTrue(result["read_only"])
            self.assertFalse(result["legacy_fts_baseline_verified"])
            self.assertEqual(self.apply("resume"), result)
        self.assertEqual(self.cache_path.read_bytes(), before_cache)
        self.assertEqual(self.journal_path.read_bytes(), before_journal)

    def test_unrelated_fts_write_does_not_invalidate_receipt_only_validation(self):
        self.record_completed_fixture()
        before_cache = self.cache_path.read_bytes()
        original_scroll = self.backend.scroll

        def changed_fts(paper_id):
            conn = sqlite3.connect(self.fts_path)
            conn.execute("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('unrelated concurrent row','foreign','other')")
            conn.commit()
            conn.close()
            return original_scroll(paper_id)

        self.backend.scroll = changed_fts
        result = self.apply("resume")
        self.assertEqual(result["status"], "done")
        self.assertFalse(result["legacy_fts_baseline_verified"])
        self.assertEqual(self.cache_path.read_bytes(), before_cache)
        conn = sqlite3.connect(self.fts_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 3)
        conn.close()

    def test_operation_deadline_aborts_preflight_without_effects(self):
        self.record_completed_fixture()
        before_cache = self.cache_path.read_bytes()
        with patch.object(repair, "REPAIR_SECONDS", -1):
            with self.assertRaises(repair.RepairBlocked):
                self.apply("resume")
        self.assertEqual(self.cache_path.read_bytes(), before_cache)

    def test_mixed_twenty_chunk_plan_is_refused_before_fts_or_network(self):
        chunks, _, old_points, _ = self.generic_snapshot(count=77)
        paper_id = "2501.12345v2"
        additions = [dict(self.additions[0], chunk_index=77 + i,
                          section_title="Method" if i % 2 else "Limitations",
                          section_type="method" if i % 2 else "limitations",
                          text=f"Recovered bounded methodology contains distinct measurement paragraph number {i}.")
                     for i in range(20)]
        new_points = [{"id": repair._point_id(paper_id, c["chunk_index"]), "vector": [0.25] * 384,
                       "payload": {"arxiv_id": paper_id, **c}} for c in additions]
        self.cache_path.write_bytes(json.dumps(chunks).encode())
        state = sqlite3.connect(self.state_path)
        state.execute("INSERT INTO papers VALUES (?, 'done')", (paper_id,))
        state.commit()
        state.close()
        path = self.root / "twenty.json"
        repair.prepare_journal(paper_id, self.source, self.cache_path.read_bytes(),
                               additions, old_points, new_points, path)
        with patch.object(repair, "_fts_preflight", side_effect=AssertionError("no FTS scan")):
            with self.assertRaises(repair.RepairBlocked) as caught:
                repair.apply_or_resume(path, self.source_path, self.cache_path, self.fts_path,
                                       FakeQdrant(old_points), self.state_path)
        self.assertEqual(caught.exception.code, "fts_identity_unknown")
        self.assertEqual(json.loads(self.cache_path.read_bytes()), chunks)


class QdrantSnapshotTests(unittest.TestCase):
    def point(self, index, paper_id="2501.12345"):
        return {"id": repair._point_id(paper_id, index), "payload": {"arxiv_id": paper_id},
                "vector": [0.5] * 384}

    def test_scroll_paginates_exact_paper_with_vectors_and_offsets(self):
        backend = repair.QdrantHTTP("http://127.0.0.1:6333")
        points = [self.point(i) for i in range(451)]
        bodies = []

        def response(method, path, body):
            bodies.append(body)
            start = body.get("offset", 0)
            end = min(start + body["limit"], len(points))
            return {"result": {"points": points[start:end],
                               "next_page_offset": end if end < len(points) else None}}

        backend._request = response
        self.assertEqual(backend.scroll("2501.12345"), points)
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            self.assertEqual(body["filter"], {"must": [{"key": "arxiv_id", "match": {"value": "2501.12345"}}]})
            self.assertTrue(body["with_payload"])
            self.assertTrue(body["with_vector"])
        self.assertEqual([b.get("offset") for b in bodies], [None, 200, 400])

    def test_malformed_duplicate_foreign_and_repeating_pages_fail_closed(self):
        cases = [
            [{"points": "bad"}],
            [{"points": [self.point(0, "other")]}],
            [{"points": [self.point(0)], "next_page_offset": 1}, {"points": [self.point(0)]}],
            [{"points": [self.point(0)], "next_page_offset": 1},
             {"points": [self.point(1)], "next_page_offset": 1}],
            [{"points": [], "next_page_offset": 1}],
            [{"points": [self.point(0)], "next_page_offset": True}],
            [{"points": [dict(self.point(0), vector=[1] * 383)]}],
        ]
        for pages in cases:
            backend = repair.QdrantHTTP("http://localhost:6333")
            with self.subTest(pages=pages), patch.object(backend, "_request", side_effect=[{"result": p} for p in pages]):
                with self.assertRaises(repair.RepairBlocked):
                    backend.scroll("2501.12345")

    def test_snapshot_point_byte_and_time_bounds(self):
        backend = repair.QdrantHTTP("http://localhost:6333")
        with patch.object(repair, "MAX_SNAPSHOT_POINTS", 2), patch.object(backend, "_request", return_value={
                "result": {"points": [self.point(0), self.point(1)], "next_page_offset": 2}}):
            with self.assertRaises(repair.RepairBlocked):
                backend.scroll("2501.12345")
        with patch.object(repair, "MAX_SNAPSHOT_BYTES", 1), patch.object(backend, "_request", return_value={
                "result": {"points": [self.point(0)]}}):
            with self.assertRaises(repair.RepairBlocked):
                backend.scroll("2501.12345")
        with patch.object(repair.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaises(repair.RepairBlocked):
                backend.scroll("2501.12345")

    def test_http_response_bytes_and_upsert_completion_are_bounded(self):
        backend = repair.QdrantHTTP("http://localhost:6333")
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                return b"x" * limit

        with patch.object(repair, "MAX_RESPONSE_BYTES", 32), patch.object(repair.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(repair.RepairBlocked):
                backend.retrieve([self.point(0)["id"]])
        with patch.object(backend, "_request", return_value={"result": {"status": "acknowledged"}}):
            with self.assertRaises(repair.RepairBlocked):
                backend.upsert_additions([self.point(0)])


    def test_http_timeout_respects_shared_operation_deadline(self):
        backend = repair.QdrantHTTP("http://localhost:6333")
        backend._operation_deadline = 30
        with patch.object(repair.time, "monotonic", return_value=29), patch.object(repair.urllib.request, "urlopen") as call:
            call.return_value.__enter__.return_value.read.return_value = b'{"status":"ok","result":[]}'
            self.assertEqual(backend.retrieve([self.point(0)["id"]]), [])
            self.assertEqual(call.call_args.kwargs["timeout"], 1)
        with patch.object(repair.time, "monotonic", return_value=31), patch.object(repair.urllib.request, "urlopen") as call:
            with self.assertRaises(repair.RepairBlocked):
                backend.retrieve([self.point(0)["id"]])
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
