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
from pipeline import document_cache
from pipeline import extractor


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
                           ("verified-nochanges-fullmap", "heading_label_candidate", 0, "c", "s",
                            "fulltext_cache", "[]",
                            '[{"chunk_index":0,"section_title":"Methods","section_type":"method"}]', 1))
            census.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                           ("unverified-heading", "heading_label_candidate", 0, "c", "s", *base, 0))
            census.commit()
            rows = driver._candidate_rows(census)
            self.assertEqual([row["paper_id"] for row in rows], ["exact", "verified-heading"])
            census.execute("INSERT INTO meta VALUES ('candidate_subset','verified-unresolved-label-retry')")
            rows = driver._candidate_rows(census)
            census.close()
            self.assertEqual([row["paper_id"] for row in rows],
                             ["exact", "verified-nochanges-fullmap"])

    def test_bounded_completed_batches_skip_cleared_prefix_and_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_census = root / "frozen-census.sqlite"
            source_census.write_bytes(b"frozen census identity")
            stat = source_census.stat()
            source_identity = json.dumps([str(source_census.resolve()), stat.st_dev,
                stat.st_ino, stat.st_size, stat.st_mtime_ns], separators=(",", ":"))
            candidate = root / "verified-unresolved.sqlite"
            conn = sqlite3.connect(candidate)
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.executemany("INSERT INTO meta VALUES (?,?)", [
                ("run_complete", "1"),
                ("candidate_subset", "verified-unresolved-label-retry"),
                ("source_census_path", str(source_census.resolve())),
                ("source_census_identity", source_identity),
                ("source_bound_selected_count", "343"),
                ("source_verified_unresolved_count", "403"),
                ("source_unresolved_chunk_count", "1758"),
                ("source_bound_resolved_chunk_count", "1134")])
            conn.commit(); conn.close()
            journal_dir = root / "journals"
            run_db = root / "run.sqlite"
            ledger = driver._ledger(run_db)
            driver._record(ledger, "000", "verified", 1, "prior.json",
                           json.dumps({"unresolved_chunks": [7]}))
            for index in range(2, 343):
                driver._record(ledger, f"{index:03}", "verified", 1, "prior.json")
            ledger.close()
            rows = [{"paper_id": f"{index:03}"} for index in range(343)]
            args = SimpleNamespace(census_db=candidate, rowmap_db=root / "rowmap.sqlite",
                fts_db=root / "fts.sqlite", state_db=root / "state.sqlite", cache_dir=root / "cache",
                section_cache_dir=root / "sections", fulltext_cache_dir=root / "source",
                latex_cache_dir=root / "latex", journal_dir=journal_dir, run_db=run_db,
                collection="papers_fulltext", limit=1, max_seconds=0,
                verify_completed=True, retry_failed=False)
            calls = []
            def complete_one(row, *, ledger, **kwargs):
                calls.append(row["paper_id"])
                driver._record(ledger, row["paper_id"], "verified", 1, "new.json")
            with patch.object(driver, "_candidate_rows", return_value=rows), \
                 patch.object(driver, "_open_rowmap", return_value=sqlite3.connect(":memory:")), \
                 patch.object(driver, "_resume_one", side_effect=complete_one):
                first = driver.run(args, object())
                second = driver.run(args, object())
            self.assertEqual(calls, ["000", "001"])
            self.assertEqual((first["scheduled"], first["pending"]), (1, 0))
            self.assertEqual((second["scheduled"], second["pending"]), (1, 0))
            self.assertEqual(second["skipped_verified"], 342)

    def test_bounded_preflight_failure_preserves_accepted_journal_until_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'frozen.sqlite';source.write_bytes(b'frozen identity')
            stat=source.stat();candidate=root/'bounded.sqlite'
            with sqlite3.connect(candidate) as c:
                c.execute('CREATE TABLE meta(key PRIMARY KEY,value)')
                c.executemany('INSERT INTO meta VALUES(?,?)',[
                    ('candidate_subset','verified-unresolved-label-retry'),
                    ('source_census_path',str(source.resolve())),
                    ('source_census_identity',json.dumps([str(source.resolve()),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns])),
                    ('source_bound_selected_count','343'),('source_verified_unresolved_count','403'),
                    ('source_unresolved_chunk_count','1758'),('source_bound_resolved_chunk_count','1134')])
            c.close()
            journal_dir=root/'journals';accepted=driver._journal_path(journal_dir,'000',3)
            accepted_bytes=json.dumps(dict(kind='section-label-repair',phase='applied',papers=[dict(arxiv_id='000')])).encode()
            accepted.write_bytes(accepted_bytes)
            run_db=root/'run.sqlite';ledger=driver._ledger(run_db)
            driver._record(ledger,'000','verified',3,accepted,json.dumps({'unresolved_chunks':[7]}))
            for i in range(1,343):driver._record(ledger,f'{i:03}','verified',1,'prior.json')
            ledger.close()
            args=SimpleNamespace(census_db=candidate,rowmap_db=root/'rowmap',fts_db=root/'fts',
                state_db=root/'state',cache_dir=root/'cache',section_cache_dir=root/'sections',
                fulltext_cache_dir=root/'source',latex_cache_dir=root/'latex',journal_dir=journal_dir,
                run_db=run_db,collection='papers_fulltext',limit=1,max_seconds=0,
                verify_completed=True,retry_failed=False,bounded_retry_id=None)
            rows=[{'paper_id':f'{i:03}'} for i in range(343)]
            with patch.object(driver,'_candidate_rows',return_value=rows), \
                 patch.object(driver,'_open_rowmap',side_effect=lambda *a:sqlite3.connect(':memory:')), \
                 patch.object(driver,'_resume_one',side_effect=driver.DriverError('preflight rejected before a new journal')) as resume, \
                 patch.object(driver,'_recover_failed_paper',side_effect=AssertionError('must not roll back accepted journal')) as recover:
                first=driver.run(args,object());second=driver.run(args,object())
                self.assertEqual((first['failed'],second['scheduled']),(1,0))
                self.assertEqual(resume.call_count,1);recover.assert_not_called()
                args.retry_failed=True;args.bounded_retry_id='explicit-invocation'
                driver.run(args,object());driver.run(args,object())
                self.assertEqual(resume.call_count,2);recover.assert_not_called()
            ledger=driver._ledger(run_db)
            state=ledger.execute('SELECT status,attempt,journal_path,detail FROM papers WHERE paper_id=?',('000',)).fetchone()
            ledger.close()
            self.assertEqual(state[:3],('verified',3,str(accepted)))
            detail=json.loads(state[3]);self.assertEqual(detail['unresolved_chunks'],[7])
            self.assertIn('preflight rejected',detail['bounded_retry_error']['error'])
            self.assertEqual(accepted.read_bytes(),accepted_bytes)

    def test_long_exact_identity_label_runs_from_frozen_rowmap_to_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_dir, section_dir = root / "chunk_cache", root / "section_cache"
            source_dir, latex_dir = root / "fulltext_cache", root / "latex_cache"
            for path in (cache_dir, section_dir, source_dir, latex_dir): path.mkdir()
            aid = "acl:https://lrec.elra.info/lrec2026-main-001"
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, aid + "#0"))
            unresolved_id = str(uuid.uuid5(uuid.NAMESPACE_URL, aid + "#1"))
            proposed_invalid_title = "Long source heading " + ("y" * 530)
            source_text = "# Methods\n\nExact cached source\n\n# " + proposed_invalid_title + "\n\nAnother exact cached source."
            parsed_chunks, _ = extractor.build_chunks_markdown(source_text, document_kind="paper")
            old_chunk = dict(parsed_chunks[0], section_title="Misc", section_type="other")
            old_invalid_title = "Existing invalid label " + ("x" * 530)
            unresolved_chunk = dict(parsed_chunks[1])
            unresolved_chunk.update(section_title=old_invalid_title, section_type="other")
            cache_path = cache_dir / (document_cache.safe_id(aid) + ".json")
            cache_path.write_text(json.dumps([old_chunk, unresolved_chunk]), encoding="utf-8")
            source_path = source_dir / (document_cache.safe_id(aid) + ".tex")
            source_path.write_text(source_text, encoding="utf-8")
            initial_cache = cache_path.read_bytes()
            initial_manifest = document_cache.make_section_manifest(aid,
                [old_chunk, unresolved_chunk], initial_cache, source_path.read_bytes(),
                "markdown", "test-source-parser")
            document_cache.publish_section_manifest(section_dir /
                (document_cache.safe_id(aid) + ".json"), initial_manifest)
            self.assertEqual(initial_manifest["source_sha256"], hashlib.sha256(source_path.read_bytes()).hexdigest())
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
            census.execute("CREATE TABLE papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json,labels_source_verified INTEGER DEFAULT 0,fulltext_source,parser_mode)")
            full_labels = [{"chunk_index": chunk["chunk_index"], "section_title": chunk["section_title"],
                            "section_type": chunk["section_type"]} for chunk in parsed_chunks]
            census.execute("INSERT INTO papers(paper_id,candidate_status,exact_text_ids,cache_sha256,source_sha256,source_location,candidate_changes_json,candidate_label_map_json,fulltext_source,parser_mode) VALUES (?,?,?,?,?,?,?,?,?,?)", (aid, "candidate", 1,
                hashlib.sha256(cache_path.read_bytes()).hexdigest(), hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "fulltext_cache", json.dumps(full_labels), json.dumps(full_labels), "acl-pdf", None))
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
            if result["failed"]:
                debug = sqlite3.connect(journal_dir / "repair-run.sqlite3")
                failure = debug.execute("SELECT detail FROM papers WHERE paper_id=?", (aid,)).fetchone()[0]
                debug.close()
                self.fail(f"source-grounded long-label repair failed: {failure}")
            self.assertEqual(result, {"verified": 1, "failed": 0, "skipped_verified": 0,
                "skipped_failed": 0, "candidates": 1, "scheduled": 1, "pending": 0,
                "verified_with_unresolved": 0, "unresolved_chunks": 0})
            # Resume verifies the durable after-image before consulting the now-stale
            # pre-migration cache hash from the read-only census.
            self.assertEqual(driver.run(args, qdrant), {"verified": 0, "failed": 0,
                "skipped_verified": 1, "skipped_failed": 0, "candidates": 1,
                "scheduled": 0, "pending": 0, "verified_with_unresolved": 0,
                "unresolved_chunks": 0})
            self.assertEqual(qdrant.point["payload"]["section_type"], "method")
            repaired_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired_cache[0]["section_title"], "Methods")
            self.assertEqual(repaired_cache[1]["section_title"], proposed_invalid_title)
            self.assertEqual(qdrant.points[unresolved_id]["payload"]["section_title"], proposed_invalid_title)
            ledger = sqlite3.connect(journal_dir / "repair-run.sqlite3")
            self.assertEqual(ledger.execute("SELECT status FROM papers WHERE paper_id=?", (aid,)).fetchone()[0], "verified")
            self.assertIsNone(ledger.execute("SELECT detail FROM papers WHERE paper_id=?", (aid,)).fetchone()[0])
            plan = driver._latest_journal(journal_dir, aid)[2]
            self.assertEqual(plan["papers"][0]["unresolved_chunks"], [])

            # A verified-but-unresolved no-change retry still has to rebuild and
            # verify the canonical manifest before it clears the unresolved ledger.
            original_cache_sha = hashlib.sha256(json.dumps([old_chunk, unresolved_chunk]).encode()).hexdigest()
            census = sqlite3.connect(census_path)
            census.row_factory = sqlite3.Row
            census.execute("ALTER TABLE papers ADD COLUMN frozen_cache_sha256 TEXT")
            census.execute("UPDATE papers SET frozen_cache_sha256=?,cache_sha256=?,candidate_changes_json='[]' WHERE paper_id=?",
                           (original_cache_sha, hashlib.sha256(cache_path.read_bytes()).hexdigest(), aid))
            census.commit()
            row = census.execute("SELECT * FROM papers WHERE paper_id=?", (aid,)).fetchone()
            ledger.execute("UPDATE papers SET detail=? WHERE paper_id=?",
                           (json.dumps({"unresolved_chunks": [1]}), aid))
            ledger.commit()
            rowmap_conn = driver._open_rowmap(rowmap_path, fts_path)
            prior_journal = Path(ledger.execute("SELECT journal_path FROM papers WHERE paper_id=?",
                                                (aid,)).fetchone()[0])
            verify_after = driver._verify_after
            def reject_historical_full_set(module, path, client):
                if Path(path) == prior_journal:
                    raise AssertionError("bounded retry consulted historical point-set after-image")
                return verify_after(module, path, client)
            with patch.object(driver, "_verify_after", side_effect=reject_historical_full_set):
                driver._resume_one(row, state_db=state_path, fts_db=fts_path, census_db=census,
                    rowmap_conn=rowmap_conn, qdrant=qdrant, cache_dir=cache_dir,
                    section_cache_dir=section_dir, fulltext_cache_dir=source_dir,
                    latex_cache_dir=latex_dir, journal_dir=journal_dir, ledger=ledger,
                    collection="papers_fulltext", verify_completed=True)
            nochange = driver._latest_journal(journal_dir, aid)[2]
            self.assertEqual(nochange["phase"], "applied")
            self.assertEqual(nochange["papers"][0]["changes"], [])
            self.assertIn("cache_parent_journal", nochange)
            self.assertEqual(ledger.execute("SELECT detail FROM papers WHERE paper_id=?", (aid,)).fetchone()[0], None)
            manifest = document_cache.load_section_manifest(root, aid,
                cache_dir=cache_dir, section_cache_dir=section_dir)
            self.assertEqual([section["section_title"] for section in manifest["sections"]],
                             [chunk["section_title"] for chunk in parsed_chunks])
            census.close(); rowmap_conn.close(); ledger.close()

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
