import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import uuid
from types import SimpleNamespace

from ops import repair_corpus_labels as driver


class FakeQdrant:
    def __init__(self, point):
        self.point = point
        self.points = {str(point["id"]): point}
        self.fail_once = False

    def scroll(self, **kwargs):
        aid = kwargs["scroll_filter"]["must"][0]["match"]["value"]
        return ([p for p in self.points.values() if p["payload"].get("arxiv_id") == aid], None)

    def set_payload(self, *, collection_name, payload, points, wait=True):
        for point_id in points:
            self.points[str(point_id)]["payload"].update(payload)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated request failure")


class CorpusLabelDriverTests(unittest.TestCase):
    def test_complete_catalog_includes_only_receipted_post_census_additions(self):
        from pipeline.fts_catalog import ensure_catalog
        with tempfile.TemporaryDirectory()as temp:
            path=Path(temp)/'fts.db';fts=sqlite3.connect(path)
            fts.execute('CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED)')
            ensure_catalog(fts)
            fts.execute('CREATE TABLE dtox_cached_point_receipts(operation_id,point_id,row_id)')
            aid='2401.00001';chunks=[];qdrant=FakeQdrant({'id':'p0','payload':{}});qdrant.points={}
            rowmap=sqlite3.connect(':memory:');rowmap.execute('CREATE TABLE rows(rowid INTEGER,point_id,arxiv_id)')
            for index in range(2):
                pid='p'+str(index);text='source text '+str(index)
                rid=fts.execute('INSERT INTO chunks VALUES(?,?,?,?)',(text,pid,aid,'other')).lastrowid
                fts.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(pid,aid,rid))
                fts.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(aid,rid))
                if index==0:rowmap.execute('INSERT INTO rows VALUES(?,?,?)',(rid,pid,aid))
                else:fts.execute('INSERT INTO dtox_cached_point_receipts VALUES(?,?,?)',('accepted',pid,rid))
                chunk=dict(chunk_index=index,text=text,section_title='Old',section_type='other',element_type='prose')
                chunks.append(chunk);qdrant.points[pid]=dict(id=pid,payload=dict(chunk,arxiv_id=aid),vector=[.1])
            fts.commit()
            labels=[dict(chunk_index=i,section_title='Methods',section_type='method')for i in range(2)]
            row=dict(paper_id=aid,candidate_label_map_json=json.dumps(labels),candidate_changes_json=json.dumps(labels))
            result=driver._change_plan(row,chunks,{c['chunk_index']:c for c in chunks},rowmap,path,qdrant,'papers_fulltext')
            self.assertEqual(len(result[0]),2)
            self.assertEqual(result[3],{aid:[1,2]})
            fts.execute('DELETE FROM dtox_cached_point_receipts');fts.commit()
            with self.assertRaisesRegex(driver.DriverError,'accepted point receipt'):
                driver._change_plan(row,chunks,{c['chunk_index']:c for c in chunks},rowmap,path,qdrant,'papers_fulltext')
            fts.close();rowmap.close()

    def test_publication_gate_is_only_used_for_published_and_propagates_failure(self):
        events = []
        @contextmanager
        def gate():
            events.append("stop")
            try:
                yield
            except Exception:
                events.append("keep_stopped")
                raise
            else:
                events.append("start")
        with driver._publication_context(driver.legacy, gate):
            events.append("legacy")
        with driver._publication_context(driver.published, gate):
            events.append("published")
        with self.assertRaises(RuntimeError):
            with driver._publication_context(driver.published, gate):
                raise RuntimeError("uncertain compensation")
        self.assertEqual(events, ["legacy", "stop", "published", "start", "stop", "keep_stopped"])

    def test_published_recovery_rollback_runs_inside_gate(self):
        events = []
        @contextmanager
        def gate():
            events.append("stop")
            yield
            events.append("start")
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "journal.json"
            journal.write_text(json.dumps({"phase": "applying"}))
            def rollback(path, **kwargs):
                self.assertEqual(events, ["stop"])
                path.write_text(json.dumps({"phase": "rolled_back"}))
            with patch.object(driver, "_latest_journal", return_value=(1, journal, {"phase": "applying"})), \
                 patch.object(driver, "_module_for_plan", return_value=driver.published), \
                 patch.object(driver.published, "rollback_plan", side_effect=rollback), \
                 patch.object(driver.published, "_verify"):
                driver._recover_failed_paper("paper", temp, None, gate)
            self.assertEqual(events, ["stop", "start"])

    def test_candidate_selection_includes_only_source_verified_heading_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "census.db"
            census = sqlite3.connect(path)
            census.row_factory = sqlite3.Row
            census.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
            census.execute("INSERT INTO meta VALUES ('run_complete','1')")
            census.execute("CREATE TABLE papers(paper_id,candidate_status,exact_text_ids,cache_sha256,"
                           "source_sha256,source_location,candidate_changes_json,candidate_label_map_json,"
                           "labels_source_verified)")
            base = ("fulltext_cache", "[]", "[]")
            census.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                           ("exact", "candidate", 1, "c", "s", *base, 0))
            census.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                           ("verified-heading", "heading_label_candidate", 0, "c", "s", "fulltext_cache", '[{"chunk_index":0}]', "[]", 1))
            census.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                           ("unchanged-heading", "heading_label_candidate", 0, "c", "s", *base, 1))
            census.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                           ("unverified-heading", "heading_label_candidate", 0, "c", "s", *base, 0))
            census.commit()
            rows = driver._candidate_rows(census)
            census.close()
            self.assertEqual([row["paper_id"] for row in rows], ["exact", "verified-heading"])

    def test_one_legacy_candidate_runs_from_frozen_rowmap_to_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_dir, section_dir = root / "chunk_cache", root / "section_cache"
            source_dir, latex_dir = root / "fulltext_cache", root / "latex_cache"
            for path in (cache_dir, section_dir, source_dir, latex_dir): path.mkdir()
            aid = "2401.00002"
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, aid + "#0"))
            unresolved_id = str(uuid.uuid5(uuid.NAMESPACE_URL, aid + "#1"))
            old_chunk = {"chunk_index": 0, "text": "Exact cached source", "section_title": "Misc",
                         "section_type": "other", "element_type": "prose"}
            old_invalid_title = "Existing invalid label " + ("x" * 530)
            unresolved_chunk = {"chunk_index": 1, "text": "Another exact cached source",
                                "section_title": old_invalid_title, "section_type": "other",
                                "element_type": "prose"}
            cache_path = cache_dir / (aid + ".json")
            cache_path.write_text(json.dumps([old_chunk, unresolved_chunk]), encoding="utf-8")
            source_path = source_dir / (aid + ".tex")
            source_path.write_text("source snapshot", encoding="utf-8")
            state_path = root / "state.db"
            state = sqlite3.connect(state_path)
            state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT)")
            state.execute("INSERT INTO papers VALUES (?, 'done')", (aid,))
            state.commit(); state.close()
            fts_path = root / "fts.db"
            fts = sqlite3.connect(fts_path)
            fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
            rowid = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type) VALUES (?,?,?,?,?)",
                (old_chunk["text"], point_id, aid, "other", "prose")).lastrowid
            unresolved_rowid = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type) VALUES (?,?,?,?,?)",
                (unresolved_chunk["text"], unresolved_id, aid, "other", "prose")).lastrowid
            fts.commit(); fts.close()
            stat = fts_path.stat()
            rowmap_path = root / "rowmap.db"
            rowmap = sqlite3.connect(rowmap_path)
            rowmap.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            rowmap.execute("CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id TEXT NOT NULL,arxiv_id TEXT NOT NULL)")
            rowmap.executemany("INSERT INTO meta VALUES (?,?)", [
                ("complete", "1"),
                ("source_identity", json.dumps([str(fts_path.resolve()), stat.st_dev, stat.st_ino]))])
            rowmap.executemany("INSERT INTO rows VALUES (?,?,?)", [(rowid, point_id, aid),
                (unresolved_rowid, unresolved_id, aid)])
            rowmap.commit(); rowmap.close()
            census_path = root / "census.db"
            census = sqlite3.connect(census_path)
            census.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            census.execute("INSERT INTO meta VALUES ('run_complete','1')")
            census.execute("CREATE TABLE papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json,labels_source_verified INTEGER DEFAULT 0)")
            proposed_invalid_title = "Invalid proposal " + ("y" * 530)
            full_labels = [{"chunk_index": 0, "section_title": "Methods", "section_type": "method"},
                           {"chunk_index": 1, "section_title": proposed_invalid_title, "section_type": "related_work"}]
            census.execute("INSERT INTO papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json) VALUES (?,?,?,?,?,?,?,?)", (aid, "candidate", 1,
                hashlib.sha256(cache_path.read_bytes()).hexdigest(), hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "fulltext_cache", json.dumps(full_labels), json.dumps(full_labels)))
            census.commit(); census.close()
            qdrant = FakeQdrant({"id": point_id, "payload": {"arxiv_id": aid, "chunk_index": 0,
                "text": old_chunk["text"], "section_title": "Misc", "section_type": "other"}, "vector": [0.1]})
            qdrant.points[unresolved_id] = {"id": unresolved_id, "payload": {"arxiv_id": aid,
                "chunk_index": 1, "text": unresolved_chunk["text"], "section_title": old_invalid_title,
                "section_type": "other"}, "vector": [0.2]}
            journal_dir = root / "journals"
            args = SimpleNamespace(census_db=census_path, rowmap_db=rowmap_path, fts_db=fts_path,
                state_db=state_path, cache_dir=cache_dir, section_cache_dir=section_dir,
                fulltext_cache_dir=source_dir, latex_cache_dir=latex_dir, journal_dir=journal_dir,
                run_db=None, collection="papers_fulltext", limit=0, max_seconds=0,
                verify_completed=False, retry_failed=False)
            result = driver.run(args, qdrant)
            self.assertEqual(result, {"verified": 1, "failed": 0, "skipped_verified": 0,
                "skipped_failed": 0, "candidates": 1, "scheduled": 1, "pending": 0,
                "verified_with_unresolved": 1, "unresolved_chunks": 1})
            # Resume verifies the durable after-image before consulting the now-stale
            # pre-migration cache hash from the read-only census.
            self.assertEqual(driver.run(args, qdrant), {"verified": 0, "failed": 0,
                "skipped_verified": 1, "skipped_failed": 0, "candidates": 1,
                "scheduled": 0, "pending": 0, "verified_with_unresolved": 1,
                "unresolved_chunks": 1})
            self.assertEqual(qdrant.point["payload"]["section_type"], "method")
            repaired_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired_cache[0]["section_title"], "Methods")
            self.assertEqual(repaired_cache[1]["section_title"], old_invalid_title)
            self.assertEqual(qdrant.points[unresolved_id]["payload"]["section_title"], old_invalid_title)
            ledger = sqlite3.connect(journal_dir / "repair-run.sqlite3")
            self.assertEqual(ledger.execute("SELECT status FROM papers WHERE paper_id=?", (aid,)).fetchone()[0], "verified")
            detail = json.loads(ledger.execute("SELECT detail FROM papers WHERE paper_id=?", (aid,)).fetchone()[0])
            self.assertEqual(detail["unresolved_chunks"][0]["chunk_index"], 1)
            ledger.close()
            plan = driver._latest_journal(journal_dir, aid)[2]
            self.assertEqual(plan["papers"][0]["unresolved_chunks"][0]["reason"], "invalid_candidate_label")

    def test_failed_apply_is_compensated_before_failed_status_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_dir, section_dir = root / "chunk_cache", root / "section_cache"
            source_dir, latex_dir = root / "fulltext_cache", root / "latex_cache"
            for path in (cache_dir, section_dir, source_dir, latex_dir): path.mkdir()
            aid = "2401.00003"
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, aid + "#0"))
            chunk = {"chunk_index": 0, "text": "passage", "section_title": "Old", "section_type": "other", "element_type": "prose"}
            cache_path = cache_dir / (aid + ".json"); cache_path.write_text(json.dumps([chunk]), encoding="utf-8")
            source_path = source_dir / (aid + ".tex"); source_path.write_text("source", encoding="utf-8")
            state_path = root / "state.db"; state = sqlite3.connect(state_path)
            state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT)"); state.execute("INSERT INTO papers VALUES (?, 'done')", (aid,)); state.commit(); state.close()
            fts_path = root / "fts.db"; fts = sqlite3.connect(fts_path)
            fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
            rowid = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type) VALUES (?,?,?,?)", ("passage", point_id, aid, "other")).lastrowid
            fts.commit(); fts.close()
            stat = fts_path.stat(); rowmap_path = root / "rowmap.db"; rowmap = sqlite3.connect(rowmap_path)
            rowmap.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"); rowmap.execute("CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id TEXT NOT NULL,arxiv_id TEXT NOT NULL)")
            rowmap.executemany("INSERT INTO meta VALUES (?,?)", [("complete", "1"), ("source_identity", json.dumps([str(fts_path.resolve()), stat.st_dev, stat.st_ino]))])
            rowmap.execute("INSERT INTO rows VALUES (?,?,?)", (rowid, point_id, aid)); rowmap.commit(); rowmap.close()
            census_path = root / "census.db"; census = sqlite3.connect(census_path)
            census.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"); census.execute("INSERT INTO meta VALUES ('run_complete','1')")
            census.execute("CREATE TABLE papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json,labels_source_verified INTEGER DEFAULT 0)")
            full = [{"chunk_index": 0, "section_title": "New", "section_type": "method"}]
            census.execute("INSERT INTO papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json) VALUES (?,?,?,?,?,?,?,?)", (aid,"candidate",1,hashlib.sha256(cache_path.read_bytes()).hexdigest(),hashlib.sha256(source_path.read_bytes()).hexdigest(),"fulltext_cache",json.dumps(full),json.dumps(full)))
            census.commit(); census.close()
            qdrant = FakeQdrant({"id": point_id, "payload": {"arxiv_id": aid, "chunk_index": 0,"text": "passage","section_title": "Old","section_type": "other"},"vector": [0.2]})
            args = SimpleNamespace(census_db=census_path,rowmap_db=rowmap_path,fts_db=fts_path,state_db=state_path,
                cache_dir=cache_dir,section_cache_dir=section_dir,fulltext_cache_dir=source_dir,latex_cache_dir=latex_dir,
                journal_dir=root/"journals",run_db=None,collection="papers_fulltext",limit=0,max_seconds=0,
                verify_completed=False,retry_failed=False)
            qdrant.fail_once = True
            result = driver.run(args, qdrant)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(qdrant.point["payload"]["section_type"], "other")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))[0]["section_type"], "other")
            args.retry_failed = True
            retried = driver.run(args, qdrant)
            self.assertEqual(retried["verified"], 1)
            self.assertEqual(qdrant.point["payload"]["section_type"], "method")


if __name__ == "__main__":
    unittest.main()
