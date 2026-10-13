"""Changed vector contract only: synthetic readbacks, no embeddings or HTTP."""
import copy
import hashlib
import unittest

from pipeline import publication as p
from pipeline.tests.test_publication_revision import RevisionTests, ready, vector_plan, writer
from pipeline.tests.revision_fixture import admitted_artifact, stage_artifact


class VectorReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RevisionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.job = self.fixture.job

    def candidate(self, conn, projection=None):
        projection = copy.deepcopy(projection or self.fixture.projection("thirdtoken", "thirdvector"))
        _, prior = p._load_revision(conn, self.root, "p", self.fixture.base)
        families, recipe = vector_plan(projection, prior, self.fixture.base)
        return projection, families, recipe

    def stage(self, conn, projection, families, recipe, **bindings):
        artifact = admitted_artifact("p", projection, families, recipe,
            {"original": "b" * 64, "vector_ownership": "e" * 64, **bindings},
            parent_revision=self.fixture.base, parent_generation=1)
        return stage_artifact(conn, self.root, artifact)

    def reject_readback(self, edit):
        with writer(self.root) as conn:
            before = self.fixture.snapshot(conn)
            def callback(job, artifact):
                self.assertFalse(conn.in_transaction)
                result = ready(job, artifact)
                edit(result)
                return result
            with self.assertRaises(p.PublicationBlocked):
                p.activate_revision(conn, self.root, self.job, callback)
            self.assertEqual(self.fixture.snapshot(conn), before)

    def test_old_chunk_only_receipt_is_not_all_indexes_ready(self):
        def chunk_only(result):
            del result["vector_families"]
            result["point_ids_sha256"] = "a" * 64
        self.reject_readback(chunk_only)

    def test_both_family_readbacks_commit(self):
        with writer(self.root) as conn:
            def callback(job, artifact):
                self.assertFalse(conn.in_transaction)
                result = ready(job, artifact)
                self.assertEqual(set(result["vector_families"]), {"fulltext", "coarse"})
                self.assertEqual([result["vector_families"][n]["dimension"] for n in ("fulltext", "coarse")], [384, 768])
                return result
            p.activate_revision(conn, self.root, self.job, callback)
            self.assertEqual(self.fixture.snapshot(conn)[0], self.fixture.new)

    def test_declared_families_are_closed(self):
        with writer(self.root) as conn:
            for extra in (False, True):
                projection, families, recipe = self.candidate(conn)
                if extra:
                    families["source"] = copy.deepcopy(families["coarse"])
                else:
                    del families["coarse"]
                with self.subTest(extra=extra), self.assertRaises(p.PublicationBlocked):
                    self.stage(conn, projection, families, recipe)
            self.fixture.assert_old(conn)

    def test_declared_vector_coverage_is_exact(self):
        with writer(self.root) as conn:
            for name in ("fulltext", "coarse"):
                for extra in (False, True):
                    projection, families, recipe = self.candidate(conn)
                    families[name]["points"] = families[name]["points"] * 2 if extra else []
                    with self.subTest(name=name, extra=extra), self.assertRaises(p.PublicationBlocked):
                        self.stage(conn, projection, families, recipe)

    def test_changed_title_or_abstract_cannot_keep_old_coarse_input(self):
        with writer(self.root) as conn:
            _, parent = p._load_revision(conn, self.root, "p", self.fixture.base)
            for field in ("title", "abstract"):
                projection = self.fixture.projection("oldtoken", "isolated_" + field)
                projection["metadata"][field] += " changed input"
                projection, families, recipe = self.candidate(conn, projection)
                families["coarse"]["points"][0]["input_text_sha256"] = parent["vector_families"]["coarse"]["points"][0]["input_text_sha256"]
                with self.subTest(field=field), self.assertRaises(p.PublicationBlocked):
                    self.stage(conn, projection, families, recipe)

    def unchanged_inputs(self, conn):
        projection = self.fixture.projection("oldtoken", "newuuid_sameinput")
        projection["metadata"]["year"] = 2025
        projection["metadata"]["layers"] = "changed"
        return self.candidate(conn, projection)

    def test_unchanged_inputs_reuse_vectors_with_new_ids_and_metadata_readback(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.unchanged_inputs(conn)
            for family in families.values():
                self.assertIsNotNone(family["points"][0]["reuse_from"])
            job = self.stage(conn, projection, families, recipe)
            _, artifact = p._load_revision(conn, self.root, "p", job["revision"])
            readback = ready(job, artifact)
            for family in readback["vector_families"].values():
                point = family["points"][0]
                self.assertEqual(point["reuse_readback_sha256"], point["vector_sha256"])
                self.assertEqual(point["metadata_sha256"], hashlib.sha256(p._revision_bytes(projection["metadata"])).hexdigest())
            p.activate_revision(conn, self.root, job, ready)
            self.assertEqual(self.fixture.snapshot(conn)[0], projection)

    def test_unchanged_coarse_requires_lineage_and_identical_vector_bytes(self):
        with writer(self.root) as conn:
            for field, value in (("reuse_from", None), ("vector_sha256", "f" * 64)):
                projection, families, recipe = self.unchanged_inputs(conn)
                families["coarse"]["points"][0][field] = value
                with self.subTest(field=field), self.assertRaises(p.PublicationBlocked):
                    self.stage(conn, projection, families, recipe)

    def test_changed_input_cannot_claim_reuse(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            families["coarse"]["points"][0]["reuse_from"] = {"revision": self.fixture.base,
                "collection": "fixture_coarse", "point_id": self.fixture.old["chunks"][0]["point_id"]}
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)

    def test_profile_changes_are_not_model_or_collection_migration(self):
        with writer(self.root) as conn:
            for field, value in (("model_id", "different"), ("model_sha256", "f" * 64),
                                 ("dimension", 384), ("collection", "different"), ("recipe_sha256", "f" * 64)):
                projection, families, recipe = self.candidate(conn)
                families["coarse"][field] = value
                with self.subTest(field=field), self.assertRaises(p.PublicationBlocked):
                    self.stage(conn, projection, families, recipe)

    def test_parent_coarse_uuid_cannot_be_overwritten(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            _, parent = p._load_revision(conn, self.root, "p", self.fixture.base)
            families["coarse"]["points"][0]["point_id"] = parent["vector_families"]["coarse"]["points"][0]["point_id"]
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)

    def test_cross_collection_same_uuid_is_rejected(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            families["coarse"]["points"][0]["point_id"] = families["fulltext"]["points"][0]["point_id"]
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)

    def test_peer_collision_preserves_failed_staging_evidence(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            families["coarse"]["points"][0]["point_id"] = self.fixture.peer["chunks"][0]["point_id"]
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)
            self.fixture.assert_old(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM paper_publications WHERE arxiv_id='p' AND state='staged'").fetchone()[0], 2)

    def test_readback_coverage_no_extra_or_omitted_families_or_vectors(self):
        for edit in (lambda r: r["vector_families"].pop("coarse"),
                     lambda r: r["vector_families"].update(extra={}),
                     lambda r: r["vector_families"]["coarse"]["points"].clear(),
                     lambda r: r["vector_families"]["fulltext"]["points"].append({})):
            self.reject_readback(edit)

    def test_readback_model_dimension_vector_input_metadata_and_owner_are_bound(self):
        for field, value in (("model_sha256", "f" * 64), ("dimension", True)):
            self.reject_readback(lambda r: r["vector_families"]["coarse"].update({field: value}))
        for field in ("vector_sha256", "input_text_sha256", "metadata_sha256", "owner_paper_id", "owner_revision"):
            self.reject_readback(lambda r: r["vector_families"]["coarse"]["points"][0].update({field: "wrong"}))

    def test_reuse_source_readback_must_match(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.unchanged_inputs(conn)
            job = self.stage(conn, projection, families, recipe)
            def bad(job, artifact):
                result = ready(job, artifact)
                result["vector_families"]["coarse"]["points"][0]["reuse_readback_sha256"] = "f" * 64
                return result
            with self.assertRaises(p.PublicationBlocked):
                p.activate_revision(conn, self.root, job, bad)
            self.fixture.assert_old(conn)

    def test_missing_claim_or_ownership_scope_blocks(self):
        self.reject_readback(lambda r: r.update(collision_scope=["fixture_fulltext"]))
        self.reject_readback(lambda r: r.update(ownership_admission_sha256="f" * 64))
        with writer(self.root) as conn:
            conn.execute("DELETE FROM publication_vector_claims WHERE revision=?", (self.job["revision"],))
            conn.commit()
            with self.assertRaises(p.PublicationBlocked):
                p.activate_revision(conn, self.root, self.job, ready)
            self.fixture.assert_old(conn)

    def test_recipe_fallback_is_explicit_not_live_scroll(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            projection["metadata"]["abstract"] = "short"
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)
            recipe["fallback_text"] = "An explicitly frozen fallback prose passage."
            recipe["fallback_source_sha256"] = "9" * 64
            text = p._revision_coarse_input(projection, recipe, {"coarse_fallback": "9" * 64})
            families["coarse"]["points"][0]["input_text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            self.stage(conn, projection, families, recipe, coarse_fallback="9" * 64)

    def test_recipe_parameters_cannot_change_under_same_pin(self):
        with writer(self.root) as conn:
            projection, families, recipe = self.candidate(conn)
            recipe["abstract_char_limit"] = 100
            with self.assertRaises(p.PublicationBlocked):
                self.stage(conn, projection, families, recipe)


if __name__ == "__main__":
    unittest.main()
