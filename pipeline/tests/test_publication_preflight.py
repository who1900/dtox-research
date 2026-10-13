import os
import logging
import tempfile
import uuid
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from pipeline.publication import PublicationBlocked

if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *_: None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_service_test_dir = tempfile.TemporaryDirectory()
_previous_data_dir = os.environ.get("DTOX_DATA_DIR")
_previous_handlers = list(logging.getLogger().handlers)
os.environ["DTOX_DATA_DIR"] = _service_test_dir.name
try:
    from pipeline import service
finally:
    if _previous_data_dir is None:
        os.environ.pop("DTOX_DATA_DIR", None)
    else:
        os.environ["DTOX_DATA_DIR"] = _previous_data_dir
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler not in _previous_handlers:
            root_logger.removeHandler(handler)
            handler.close()


class PublicationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.job = {"paper_id": "p", "revision": "rev-1"}
        self.points = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"p#{i}")),
                        "payload": {"arxiv_id": "p", "publication_revision": "rev-1"}}
                       for i in range(2)]
        self.session = Mock()
        self.session.post.side_effect = [
            self.response({"result": []}),
            self.response({"result": {"points": []}}),
        ]

    @staticmethod
    def response(body):
        response = Mock()
        response.json.return_value = body
        return response

    def test_preflight_retrieves_ids_then_excludes_exact_job_revision(self):
        self.assertTrue(service._preflight_chunk_snapshot(self.session, self.job, self.points))
        retrieve, scroll = self.session.post.call_args_list
        self.assertEqual(retrieve.args[0],
                         f"{service.QDRANT_URL}/collections/{service.COLLECTION_NAME}/points")
        self.assertEqual(retrieve.kwargs["json"]["ids"], [point["id"] for point in self.points])
        self.assertEqual(scroll.args[0],
                         f"{service.QDRANT_URL}/collections/{service.COLLECTION_NAME}/points/scroll")
        self.assertEqual(scroll.kwargs["json"]["filter"], {
            "must": [{"key": "arxiv_id", "match": {"value": "p"}}],
            "must_not": [{"key": "publication_revision", "match": {"value": "rev-1"}}],
        })
        self.assertEqual(scroll.kwargs["json"]["limit"], 1)

    def test_preflight_allows_exact_same_job_retry_after_qdrant_write(self):
        existing = [{"id": self.points[0]["id"],
                     "payload": {"arxiv_id": "p", "publication_revision": "rev-1"}}]
        self.session.post.side_effect = [
            self.response({"result": existing}),
            self.response({"result": {"points": []}}),
        ]
        self.assertTrue(service._preflight_chunk_snapshot(self.session, self.job, self.points))

    def test_preflight_blocks_foreign_id_and_same_paper_legacy(self):
        self.session.post.side_effect = [self.response({"result": [{
            "id": self.points[0]["id"], "payload": {"arxiv_id": "other"}}]})]
        with self.assertRaises(PublicationBlocked) as caught:
            service._preflight_chunk_snapshot(self.session, self.job, self.points)
        self.assertEqual(caught.exception.code, "legacy_uuid_collision")
        self.assertEqual(self.session.post.call_count, 1)

        self.session.reset_mock()
        self.session.post.side_effect = [self.response({"result": []}), self.response({
            "result": {"points": [{"id": "legacy", "payload": {"arxiv_id": "p"}}]}})]
        with self.assertRaises(PublicationBlocked) as caught:
            service._preflight_chunk_snapshot(self.session, self.job, self.points)
        self.assertEqual(caught.exception.code, "legacy_paper_collision")

    def test_retrieval_batches_are_bounded_at_200(self):
        self.points = [{"id": str(i), "payload": {"arxiv_id": "p", "publication_revision": "rev-1"}}
                       for i in range(201)]
        self.session.post.side_effect = [self.response({"result": []}), self.response({"result": []}),
                                         self.response({"result": {"points": []}})]
        self.assertTrue(service._preflight_chunk_snapshot(self.session, self.job, self.points))
        self.assertEqual([len(call.kwargs["json"]["ids"]) for call in self.session.post.call_args_list[:2]],
                         [200, 1])

    def test_transport_failure_propagates_for_typed_publication_retry(self):
        self.session.post.side_effect = TimeoutError("simulated timeout")
        with self.assertRaises(TimeoutError):
            service._preflight_chunk_snapshot(self.session, self.job, self.points)


if __name__ == "__main__":
    unittest.main()
