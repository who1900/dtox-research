import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from ops import repair_section_labels as repair


class SimulatedCrash(BaseException):
    pass


class FakeQdrant:
    def __init__(self, points):
        self.points = {str(p["id"]): json.loads(json.dumps(p)) for p in points}
        self.crash_after_first_write = False
        self.writes = 0
        self.payload_calls = []
        self.scroll_page_size = None

    def scroll(self, **kwargs):
        aid = kwargs["scroll_filter"]["must"][0]["match"]["value"]
        values = [p for p in self.points.values() if p["payload"].get("arxiv_id") == aid]
        if self.scroll_page_size:
            offset = kwargs.get("offset", 0)
            page = values[offset:offset + self.scroll_page_size]
            next_offset = offset + len(page) if offset + len(page) < len(values) else None
            return page, next_offset
        return values, None

    def set_payload(self, *, collection_name, payload, points, wait=True):
        self.payload_calls.append((tuple(points), dict(payload)))
        for point_id in points:
            self.points[str(point_id)]["payload"].update(payload)
            self.writes += 1
            if self.crash_after_first_write:
                self.crash_after_first_write = False
                raise SimulatedCrash("interrupted after first payload write")


class SectionLabelRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache_dir = self.root / "chunk_cache"
        self.section_dir = self.root / "section_cache"
        self.cache_dir.mkdir(); self.section_dir.mkdir()
        self.state_path = self.root / "state.db"
        self.fts_path = self.root / "fts.db"
        self.journal = self.root / "journal.json"
        self.aid = "2401.00001"
        self.pid = str(uuid.uuid5(uuid.NAMESPACE_URL, self.aid + "#0"))
        self.source_bytes = b"## Methods\nThis is a useful passage with enough descriptive words to survive splitting.\n"
        self.chunk = {"chunk_index": 0, "text": "This is a useful passage with enough descriptive words to survive splitting.", "section_title": "Misc", "section_type": "other", "element_type": "prose", "has_math": False, "has_algorithm": False}
        self.cache_path = self.cache_dir / (self.aid + ".json")
        self.cache_path.write_text(json.dumps([self.chunk]), encoding="utf-8")
        self.cache_before = self.cache_path.read_bytes()
        cache_sha = repair._sha(self.cache_before)
        self.manifest_path = self.section_dir / (self.aid + ".json")
        self.manifest_path.write_text(json.dumps({"schema": 1, "paper_id": self.aid,
            "source_sha256": repair._sha(self.source_bytes), "cache_sha256": cache_sha, "parser_mode": "markdown",
            "parser_revision": "test", "coverage": "parsed_source", "sections": [
                {"section_title": "Misc", "section_type": "other", "source_order": 0, "text": "This is a useful passage with enough descriptive words to survive splitting."}]}), encoding="utf-8")
        state = sqlite3.connect(self.state_path)
        state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY, status TEXT)")
        state.execute("INSERT INTO papers VALUES (?, 'done')", (self.aid,))
        state.commit(); state.close()
        fts = sqlite3.connect(self.fts_path)
        fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
        self.rowid = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type) VALUES (?,?,?,?)",
                                  (self.chunk["text"], self.pid, self.aid, "other")).lastrowid
        fts.commit(); fts.close()
        self.qdrant = FakeQdrant([{"id": self.pid, "vector": [0.1, 0.2], "payload": {
            "arxiv_id": self.aid, "chunk_index": 0, "text": self.chunk["text"],
            "section_title": "Misc", "section_type": "other"}}])
        self.changes = [{"arxiv_id": self.aid, "rowid": self.rowid, "point_id": self.pid,
                         "section_title": "Methods", "section_type": "method"}]

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return repair.prepare_plan(self.changes, state_db=self.state_path, fts_db=self.fts_path,
            cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
            journal_path=self.journal, qdrant=self.qdrant,
            source_bytes_by_paper={self.aid: self.source_bytes},
            expected_source_sha256_by_paper={self.aid: repair._sha(self.source_bytes)},
            source_route_by_paper={self.aid: "paper_markdown"})

    def test_dry_prepare_apply_and_exact_rollback(self):
        self.prepare()
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)
        vector = list(self.qdrant.points[self.pid]["vector"])
        repair.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.qdrant.points[self.pid]["payload"]["section_title"], "Methods")
        self.assertEqual(self.qdrant.points[self.pid]["vector"], vector)
        chunks = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual((chunks[0]["section_title"], chunks[0]["section_type"]), ("Methods", "method"))
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual((manifest["sections"][0]["section_title"], manifest["sections"][0]["section_type"]), ("Methods", "method"))
        repair.rollback_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)
        self.assertEqual(self.qdrant.points[self.pid]["payload"]["section_type"], "other")
        fts = sqlite3.connect(self.fts_path)
        self.assertEqual(fts.execute("SELECT section_type FROM chunks WHERE rowid=?", (self.rowid,)).fetchone()[0], "other")
        fts.close()

    def test_manifest_alignment_uses_unsplit_source_sections_and_preserves_metadata(self):
        first = "This method uses a clear and reliable approach to study a practical research question. " * 80
        second = "A second detailed paragraph explains the validation approach and its measured outcomes."
        source = f"## Methods\n{first}\n## Methods\n{second}\n".encode()
        parsed, sections = repair._source_sections(source, "paper_markdown")
        self.assertGreater(len(parsed), len(sections))  # first source section is split with overlap
        old = []
        after = []
        for chunk in parsed:
            old.append(dict(chunk, section_title="Misc", section_type="other"))
            after.append(dict(chunk))
        before_bytes = repair._json_bytes(old)
        after_bytes = repair._json_bytes(after)
        citations = [{"text": "preserve", "target": "#evidence"}]
        manifest = {"schema": 1, "paper_id": self.aid,
            "source_sha256": repair._sha(source), "cache_sha256": repair._sha(before_bytes),
            "parser_mode": "markdown", "parser_revision": "test", "coverage": "parsed_source",
            "sections": [{"section_title": "Misc", "section_type": "other", "source_order": 0,
                "text": first.strip() + "\n\n" + second, "citations": citations}]}
        repaired = json.loads(repair._manifest_after(repair._json_bytes(manifest), self.aid,
            before_bytes, after_bytes, source_bytes=source, expected_source_sha256=repair._sha(source),
            source_route="paper_markdown"))
        self.assertEqual(repaired["sections"][0]["text"], manifest["sections"][0]["text"])
        self.assertEqual(repaired["sections"][0]["source_order"], 0)
        self.assertEqual(repaired["sections"][0]["citations"], citations)
        self.assertEqual((repaired["sections"][0]["section_title"],
                          repaired["sections"][0]["section_type"]), ("Methods", "method"))

    def test_manifest_alignment_defers_when_one_old_boundary_spans_new_labels(self):
        source = b"## Methods\nA detailed reliable procedure describes the study carefully.\n## Conclusion\nA measured conclusion reports the practical results.\n"
        parsed, sections = repair._source_sections(source, "paper_markdown")
        old = [dict(chunk, section_title="Misc", section_type="other") for chunk in parsed]
        after = [dict(chunk) for chunk in parsed]
        before_bytes, after_bytes = repair._json_bytes(old), repair._json_bytes(after)
        manifest = {"schema": 1, "paper_id": self.aid,
            "source_sha256": repair._sha(source), "cache_sha256": repair._sha(before_bytes),
            "parser_mode": "markdown", "parser_revision": "test", "coverage": "parsed_source",
            "sections": [{"section_title": "Misc", "section_type": "other", "source_order": 0,
                "text": "\n\n".join(section["text"].strip() for section in sections)}]}
        with self.assertRaises(repair.RepairError):
            repair._manifest_after(repair._json_bytes(manifest), self.aid, before_bytes, after_bytes,
                source_bytes=source, expected_source_sha256=repair._sha(source),
                source_route="paper_markdown")

    def test_publication_marker_rejects_without_changes(self):
        self.qdrant.points[self.pid]["payload"]["publication_revision"] = "rev"
        with self.assertRaises(repair.RepairError):
            self.prepare()
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)

    def test_prepare_rejects_mismatched_exact_row(self):
        self.changes[0]["rowid"] += 200
        with self.assertRaises(repair.RepairError):
            self.prepare()
        self.assertFalse(self.journal.exists())

    def test_apply_refuses_concurrent_cache_edit(self):
        self.prepare()
        self.cache_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(repair.RepairError):
            repair.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.qdrant.points[self.pid]["payload"]["section_title"], "Misc")

    def test_complete_qdrant_point_set_is_required(self):
        self.cache_path.write_bytes(self.cache_before + b" ")
        chunks = [self.chunk, {"chunk_index": 1, "text": "Absent point", "section_title": "Other",
                               "section_type": "other", "element_type": "prose", "has_math": False, "has_algorithm": False}]
        self.cache_path.write_text(json.dumps(chunks), encoding="utf-8")
        with self.assertRaises(repair.RepairError):
            self.prepare()
        self.assertFalse(self.journal.exists())

    def test_interrupted_first_payload_write_can_be_explicitly_rolled_back(self):
        self.prepare()
        self.qdrant.crash_after_first_write = True
        with self.assertRaises(SimulatedCrash):
            repair.apply_plan(self.journal, qdrant=self.qdrant)
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "applying")
        repair.rollback_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.qdrant.points[self.pid]["payload"]["section_title"], "Misc")
        fts = sqlite3.connect(self.fts_path)
        self.assertEqual(fts.execute("SELECT section_type FROM chunks WHERE rowid=?", (self.rowid,)).fetchone()[0], "other")
        fts.close()

    def test_label_identical_updates_are_batched_and_scroll_is_paginated(self):
        second = {"chunk_index": 1, "text": "Second passage.", "section_title": "Misc",
                  "section_type": "other", "element_type": "prose", "has_math": False, "has_algorithm": False}
        self.cache_path.write_text(json.dumps([self.chunk, second]), encoding="utf-8")
        second_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.aid + "#1"))
        fts = sqlite3.connect(self.fts_path)
        second_row = fts.execute("INSERT INTO chunks(text,point_id,arxiv_id,section_type) VALUES (?,?,?,?)",
                                 (second["text"], second_id, self.aid, "other")).lastrowid
        fts.commit(); fts.close()
        self.qdrant.points[second_id] = {"id": second_id, "vector": [0.3, 0.4], "payload": {
            "arxiv_id": self.aid, "chunk_index": 1, "text": second["text"],
            "section_title": "Misc", "section_type": "other"}}
        changes = [dict(self.changes[0]), {"arxiv_id": self.aid, "rowid": second_row,
                    "point_id": second_id, "section_title": "Methods", "section_type": "method"}]
        self.qdrant.scroll_page_size = 1
        repair.prepare_plan(changes, state_db=self.state_path, fts_db=self.fts_path,
            cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
            journal_path=self.journal, qdrant=self.qdrant, manifest=False)
        repair.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.payload_calls), 1)
        self.assertEqual(set(self.qdrant.payload_calls[0][0]), {self.pid, second_id})

    def test_filtered_cache_repairs_excluded_chunk_without_inventing_point(self):
        excluded = {"chunk_index": 1, "text": "Excluded bibliography", "section_title": "References",
                    "section_type": "other", "element_type": "bibliography"}
        self.cache_path.write_text(json.dumps([self.chunk, excluded]), encoding="utf-8")
        fts = sqlite3.connect(self.fts_path)
        rowids = [self.rowid]
        fts.commit(); fts.close()
        labels = {self.aid: [
            {"chunk_index": 0, "section_title": "Methods", "section_type": "method"},
            {"chunk_index": 1, "section_title": "References", "section_type": "appendix"},
        ]}
        indices = {self.aid: [0]}
        complete_rows = {self.aid: rowids}
        repair.prepare_plan(self.changes, state_db=self.state_path, fts_db=self.fts_path,
            cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
            journal_path=self.journal, qdrant=self.qdrant, manifest=False,
            cache_labels_by_paper=labels, expected_indexed_indices_by_paper=indices,
            complete_fts_rowids_by_paper=complete_rows)
        repair.apply_plan(self.journal, qdrant=self.qdrant)
        chunks = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(chunks[1]["section_type"], "appendix")
        self.assertNotIn(str(uuid.uuid5(uuid.NAMESPACE_URL, self.aid + "#1")), self.qdrant.points)
        fts = sqlite3.connect(self.fts_path)
        self.assertEqual(fts.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
        fts.close()

    def test_invalid_unchanged_before_image_survives_valid_sibling_repair(self):
        invalid = "legacy table content " + ("x" * 530)
        self.chunk["section_title"] = invalid
        self.cache_path.write_text(json.dumps([self.chunk]), encoding="utf-8")
        self.qdrant.points[self.pid]["payload"]["section_title"] = invalid
        self.changes = [{"arxiv_id": self.aid, "rowid": self.rowid, "point_id": self.pid,
                         "section_title": invalid, "section_type": "other"}]
        repair.prepare_plan(self.changes, state_db=self.state_path, fts_db=self.fts_path,
            cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
            journal_path=self.journal, qdrant=self.qdrant, manifest=False,
            cache_labels_by_paper={self.aid: [{"chunk_index": 0, "section_title": invalid,
                                               "section_type": "other"}]},
            expected_indexed_indices_by_paper={self.aid: [0]},
            preserve_invalid_unchanged_before_image=True,
            unresolved_chunks_by_paper={self.aid: [{"chunk_index": 0, "reason": "invalid_candidate_label"}]})
        repair.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(json.loads(self.cache_path.read_text(encoding="utf-8"))[0]["section_title"], invalid)
        self.assertEqual(self.qdrant.points[self.pid]["payload"]["section_title"], invalid)
        plan = json.loads(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(plan["papers"][0]["unresolved_chunks"],
                         [{"chunk_index": 0, "reason": "invalid_candidate_label"}])

    def test_changed_invalid_label_is_not_allowed_even_with_preservation_flag(self):
        invalid = "x" * 513
        self.chunk["section_title"] = "Misc"
        self.cache_path.write_text(json.dumps([self.chunk]), encoding="utf-8")
        with self.assertRaises(repair.RepairError):
            repair.prepare_plan([{"arxiv_id": self.aid, "rowid": self.rowid, "point_id": self.pid,
                                  "section_title": invalid, "section_type": "method"}],
                state_db=self.state_path, fts_db=self.fts_path,
                cache_dir=self.cache_dir, section_cache_dir=self.section_dir,
                journal_path=self.journal, qdrant=self.qdrant, manifest=False,
                preserve_invalid_unchanged_before_image=True)


if __name__ == "__main__":
    unittest.main()
