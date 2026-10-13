"""Only new full-payload binding fixtures; fake HTTP, no sockets or models."""
import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pipeline import publication as p
from pipeline.tests.test_revision_readback import ReadbackTests, sha


class PayloadSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.case = ReadbackTests("runTest")
        self.case.setUp()

    def row(self, artifact, name):
        family = artifact["vector_families"][name]
        return self.case.rows[family["collection"]][family["points"][0]["point_id"]]

    def test_complete_target_and_reuse_parent_independently_verified(self):
        adapter = self.case.adapter()
        result = self.case.invoke(adapter)
        self.assertEqual(result, p.revision_readiness_expected("p", self.case.job["revision"], self.case.target))
        self.assertEqual(adapter.readback_evidence["payload_admission_sha256"], adapter.payload_admission_sha256)

    def test_semantic_changes_missing_and_extra_both_families_and_parent(self):
        for artifact in (self.case.target, self.case.parent):
            for name in ("fulltext", "coarse"):
                payload = self.row(artifact, name)["payload"]
                original = copy.deepcopy(payload)
                for key in ("abstract", "section_title", "future_semantic_field"):
                    for operation in ("change", "missing", "extra"):
                        payload.clear()
                        payload.update(copy.deepcopy(original))
                        if operation == "missing":
                            del payload[key]
                        elif operation == "extra":
                            payload["unexpected_new_semantics"] = "not admitted"
                        else:
                            payload[key] = "wrong"
                        # Even a self-consistent dishonest stored marker is insufficient.
                        payload["api_payload_sha256"] = sha({k: v for k, v in payload.items() if k not in p.REVISION_PAYLOAD_EXCLUSIONS})
                        with self.subTest(parent=artifact is self.case.parent, name=name, key=key, operation=operation):
                            with self.assertRaises(p.PublicationBlocked) as error:
                                self.case.invoke()
                            self.assertEqual(error.exception.code, "api_payload_snapshot_mismatch")
                payload.clear()
                payload.update(original)

    def test_owner_still_checked_separately_and_marker_required(self):
        payload = self.case.target_row()["payload"]
        for key, value in (("publication_revision", "f" * 64), ("api_payload_sha256", "f" * 64)):
            original = payload[key]
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(p.PublicationBlocked):
                self.case.invoke()
            payload[key] = original

    def test_visibility_only_exclusion_does_not_change_semantic_digest(self):
        for artifact in (self.case.parent, self.case.target):
            for name in ("fulltext", "coarse"):
                self.row(artifact, name)["payload"]["visibility_intervals"] = [{"born": "a" * 64}]
        self.case.invoke()

    def test_old_artifact_and_unknown_versions_fail_closed(self):
        for field, value in (("schema", None), ("schema", "dtox-publication-revision-1"),
                             ("api_payload_snapshot", None)):
            artifact = copy.deepcopy(self.case.target)
            artifact[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(p.PublicationBlocked):
                p._revision_vectors(artifact)

    def test_exclusions_and_api_pin_are_not_caller_extensible(self):
        for key, value in (("exclusions", list(p.REVISION_PAYLOAD_EXCLUSIONS) + ["abstract"]),
                           ("api_source_sha256", "f" * 64), ("schema", "dtox-api-payload-snapshot-2")):
            artifact = copy.deepcopy(self.case.target)
            artifact["api_payload_snapshot"][key] = value
            artifact["artifact_bindings"]["api_payload_snapshot"] = sha(artifact["api_payload_snapshot"])
            with self.subTest(key=key), self.assertRaises(p.PublicationBlocked):
                p._revision_vectors(artifact)

    def test_coverage_projection_hash_and_snapshot_hash_tampering(self):
        for mutation in ("omit", "extra", "payload", "projection", "binding"):
            artifact = copy.deepcopy(self.case.target)
            snapshot = artifact["api_payload_snapshot"]
            rows = snapshot["families"]["coarse"]
            entry = next(iter(rows.values()))
            if mutation == "omit":
                rows.clear()
            elif mutation == "extra":
                rows["extra"] = copy.deepcopy(entry)
            elif mutation == "payload":
                entry["payload"]["abstract"] = "wrong"
            elif mutation == "projection":
                artifact["projection"]["metadata"]["year"] = 1999
            else:
                artifact["artifact_bindings"]["api_payload_snapshot"] = "f" * 64
            with self.subTest(mutation=mutation), self.assertRaises(p.PublicationBlocked):
                p._revision_vectors(artifact)

    def test_missing_independent_admission_before_any_http(self):
        for changes in ({"payload_admission": None}, {"payload_admission_sha256": "f" * 64}):
            with self.subTest(changes=changes), self.assertRaises(p.PublicationBlocked):
                self.case.invoke(self.case.adapter(**changes))
        self.assertEqual(self.case.sessions, [])

    def test_artifact_self_reseal_not_external_authority(self):
        artifact = self.case.target
        snapshot = artifact["api_payload_snapshot"]
        entry = next(iter(snapshot["families"]["coarse"].values()))
        adapter = self.case.adapter()
        entry["payload"]["abstract"] = "dishonest reseal"
        entry["sha256"] = sha(entry["payload"])
        artifact["artifact_bindings"]["api_payload_snapshot"] = sha(snapshot)
        with self.assertRaises(p.PublicationBlocked):
            adapter._payload_authority(artifact, sha(artifact))
        self.assertEqual(self.case.sessions, [])

    def test_parent_snapshot_and_inputs_retained_for_reverse_readback(self):
        before = p._revision_bytes(self.case.parent)
        p._revision_vector_transition(self.case.target, self.case.parent, sha(self.case.parent))
        self.case.invoke()
        self.assertEqual(before, p._revision_bytes(self.case.parent))
        parent_job = {"paper_id": "p", "revision": sha(self.case.parent)}
        adapter = self.case.adapter(parents={}, interval={**self.case.interval, **parent_job},
            interval_sha256=sha({**self.case.interval, **parent_job}))
        result = adapter(parent_job, self.case.parent)
        self.assertEqual(result, p.revision_readiness_expected("p", parent_job["revision"], self.case.parent))

    def test_nonfinite_and_malformed_payload_fail_closed(self):
        for value in (None, [], {"bad": float("nan")}):
            with self.subTest(value=value), self.assertRaises(p.PublicationBlocked):
                p._revision_payload_bytes(value)

    def test_existing_journal_stages_v2_and_retains_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as root, closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            p.ensure_revision_schema(conn)
            parent, target = self.case.parent, self.case.target
            parent_revision = sha(parent)
            p._store(conn, root, "p", parent_revision, "revision", parent, parent_revision)
            conn.execute("UPDATE paper_publications SET state='published'")
            conn.execute("INSERT INTO paper_active VALUES('p',?,1)", (parent_revision,))
            p._claim_revision_vectors(conn, "p", parent_revision, parent, reserve=True)
            conn.commit()
            job = p.prepare_revision(conn, root, "p", target["projection"], expected_revision=parent_revision,
                expected_generation=1, artifact_bindings=target["artifact_bindings"],
                vector_families=target["vector_families"], coarse_recipe=target["coarse_recipe"],
                api_payload_snapshot=target["api_payload_snapshot"])
            self.assertEqual(job["revision"], sha(target))
            self.assertEqual(p._load_revision(conn, root, "p", job["revision"])[1], target)
            self.assertEqual(p._load_revision(conn, root, "p", parent_revision)[1], parent)
            self.assertEqual(tuple(conn.execute("SELECT revision,generation FROM paper_active").fetchone()),
                             (parent_revision, 1))


if __name__ == "__main__":
    unittest.main()
