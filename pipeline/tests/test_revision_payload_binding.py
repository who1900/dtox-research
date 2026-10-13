"""Only changed chunk/source-URL readback mechanism; fake HTTP, no sockets."""
import copy
import unittest

from pipeline import publication as p
from pipeline.tests.test_revision_readback import ReadbackTests, sha


class ChunkPayloadBindingTests(unittest.TestCase):
    def setUp(self):
        self.case = ReadbackTests("runTest")
        self.case.setUp()

    def row(self, artifact, name):
        family = artifact["vector_families"][name]
        return self.case.rows[family["collection"]][family["points"][0]["point_id"]]

    def test_correct_target_and_parent_chunk_and_url_bindings(self):
        self.assertEqual(self.case.invoke(), p.revision_readiness_expected("p", self.case.job["revision"], self.case.target))
        for artifact in (self.case.target, self.case.parent):
            chunk = artifact["projection"]["chunks"][0]
            payload = self.row(artifact, "fulltext")["payload"]
            self.assertEqual(payload["section_type"], chunk["section_type"])
            self.assertEqual(payload["element_type"], chunk["element_type"])
            self.assertEqual(payload["source_url"], artifact["projection"]["metadata"]["source_url"])

    def test_wrong_or_missing_chunk_fields_target_and_reuse_parent(self):
        for artifact in (self.case.target, self.case.parent):
            payload = self.row(artifact, "fulltext")["payload"]
            original = copy.deepcopy(payload)
            for key in ("section_type", "element_type"):
                for missing in (False, True):
                    payload.clear()
                    payload.update(copy.deepcopy(original))
                    if missing:
                        del payload[key]
                    else:
                        payload[key] = "wrong_but_text_model_vector_and_metadata_unchanged"
                    with self.subTest(parent=artifact is self.case.parent, key=key, missing=missing):
                        with self.assertRaises(p.PublicationBlocked) as error:
                            self.case.invoke()
                        self.assertEqual(error.exception.code, "api_payload_snapshot_mismatch")
                        self.assertEqual({k: v for k, v in payload.items() if k != key},
                                         {k: v for k, v in original.items() if k != key})
            payload.clear()
            payload.update(original)

    def test_nullable_chunk_field_requires_present_explicit_null(self):
        parent_projection = copy.deepcopy(self.case.parent["projection"])
        target_projection = copy.deepcopy(self.case.target["projection"])
        for projection in (parent_projection, target_projection):
            projection["chunks"][0].update(section_type=None, element_type=None)
        self.case.parent = self.case.artifact(parent_projection)
        self.case.target = self.case.artifact(target_projection, self.case.parent)
        self.case.bind()
        self.case.invoke()
        for artifact in (self.case.target, self.case.parent):
            payload = self.row(artifact, "fulltext")["payload"]
            for key in ("section_type", "element_type"):
                del payload[key]
                with self.subTest(parent=artifact is self.case.parent, key=key), self.assertRaises(p.PublicationBlocked):
                    self.case.invoke()
                payload[key] = None

    def test_source_url_wrong_missing_null_invalid_target_and_parent_both_families(self):
        for artifact in (self.case.target, self.case.parent):
            for name in ("fulltext", "coarse"):
                payload = self.row(artifact, name)["payload"]
                original = copy.deepcopy(payload)
                for value in ("https://wrong.example.invalid/", None, "not-a-url", "MISSING"):
                    payload.clear()
                    payload.update(copy.deepcopy(original))
                    payload["link"] = artifact["projection"]["metadata"]["source_url"]
                    if value == "MISSING":
                        del payload["source_url"]
                    else:
                        payload["source_url"] = value
                    with self.subTest(parent=artifact is self.case.parent, name=name, value=value):
                        with self.assertRaises(p.PublicationBlocked) as error:
                            self.case.invoke()
                        self.assertEqual(error.exception.code, "api_payload_snapshot_mismatch")
                payload.clear()
                payload.update(original)

    def test_lower_priority_url_alias_does_not_override_explicit_bound_url(self):
        self.case.payload_extras = {"link": "https://other.example.invalid/",
                                    "pdf_url": "https://other.example.invalid/p.pdf"}
        self.case.parent = self.case.artifact(self.case.parent["projection"])
        self.case.target = self.case.artifact(self.case.target["projection"], self.case.parent)
        self.case.bind()
        for artifact in (self.case.target, self.case.parent):
            for name in ("fulltext", "coarse"):
                self.row(artifact, name)["payload"].update(link="https://other.example.invalid/",
                                                         pdf_url="https://other.example.invalid/p.pdf")
        self.case.invoke()

    def test_unestablished_projection_source_url_is_unsupported_not_curated_or_id_fallback(self):
        for source in (None, "ftp://example.invalid/a", "https://example.invalid/ with-space"):
            parent_projection = copy.deepcopy(self.case.projection)
            parent_projection["metadata"]["source_url"] = source
            self.case.parent = self.case.artifact(parent_projection)
            target_projection = copy.deepcopy(self.case.target["projection"])
            target_projection["metadata"]["source_url"] = source
            self.case.target = self.case.artifact(target_projection, self.case.parent)
            self.case.bind()
            with self.subTest(source=source), self.assertRaises(p.PublicationBlocked) as error:
                self.case.invoke()
            self.assertEqual(error.exception.code, "source_url_unsupported")


if __name__ == "__main__":
    unittest.main()
