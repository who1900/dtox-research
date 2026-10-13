"""Optional post-commit observer must not alter batch/retry/completion semantics."""
import unittest
from unittest.mock import Mock, patch

from pipeline.publication import reconcile_publications
from pipeline.tests import test_publication_failures as publication_fixture


class PublicationProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publication_fixture.PublicationFailureTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def reconcile(self, **kwargs):
        f = self.fixture
        return reconcile_publications(f.conn, f.root, lambda *_: True, lambda *_: True,
                                      f.complete, lambda *_: True, **kwargs)

    def stage_pair(self):
        f = self.fixture
        f.job()
        f.add_paper("q")
        f.job("q")
        f.conn.execute("UPDATE paper_publications SET retry_after=1 WHERE arxiv_id='q'")
        f.conn.commit()

    def test_observer_runs_after_final_commit_and_ack(self):
        f = self.fixture
        f.job()
        observed = []
        def observe(job):
            self.assertFalse(f.conn.in_transaction)
            self.assertEqual(f.journal()["state"], "published")
            self.assertEqual(f.conn.execute("SELECT status FROM papers").fetchone()[0], "done")
            self.assertEqual(f.conn.execute("SELECT completed FROM completion_history").fetchone()[0], 1)
            observed.append(job["paper_id"])
        self.assertEqual(self.reconcile(on_success=observe), 1)
        self.assertEqual(observed, ["p"])

    def test_first_failure_keeps_later_selected_jobs_and_retry_policy(self):
        f = self.fixture
        self.stage_pair()
        attempted, observed = [], []
        def qdrant(job, _points):
            attempted.append(job["paper_id"])
            return job["paper_id"] != "p"
        with patch("time.time", return_value=100):
            count = reconcile_publications(f.conn, f.root, qdrant, lambda *_: True,
                                           f.complete, lambda *_: True, limit=2,
                                           on_success=lambda job: observed.append(job["paper_id"]))
        self.assertEqual((count, attempted, observed), (1, ["p", "q"], ["q"]))
        self.assertEqual(f.journal()["retry_after"], 130)
        self.assertEqual(f.journal()["error_code"], "qdrant_not_acknowledged")
        self.assertEqual(f.journal()["state"], "ready")
        self.assertEqual(f.journal("q")["state"], "published")

    def test_legacy_positional_limit_and_return_remain_compatible(self):
        f = self.fixture
        self.stage_pair()
        with patch("time.time", return_value=100):
            count = reconcile_publications(f.conn, f.root, lambda *_: True, lambda *_: True,
                                           f.complete, lambda *_: True, 2)
        self.assertEqual(count, 2)
        self.assertEqual(f.completions, ["p", "q"])

    def test_empty_batch_uses_one_selection_and_never_observes(self):
        f = self.fixture
        statements = []
        f.conn.set_trace_callback(statements.append)
        observer = Mock()
        self.assertEqual(self.reconcile(limit=20, on_success=observer), 0)
        self.assertEqual(len(statements), 1)
        self.assertIn("LIMIT 20", statements[0])
        observer.assert_not_called()

    def test_unacknowledged_completion_rollback_is_not_observed(self):
        f = self.fixture
        f.job()
        observer = Mock()
        count = reconcile_publications(f.conn, f.root, lambda *_: True, lambda *_: True,
                                       lambda *_args, **_kwargs: False, lambda *_: True,
                                       on_success=observer)
        self.assertEqual(count, 0)
        observer.assert_not_called()
        self.assertFalse(f.conn.in_transaction)
        self.assertEqual(f.journal()["error_code"], "completion_not_acknowledged")
        f.assert_uncompleted()

    def test_blocked_publication_is_not_observed(self):
        f = self.fixture
        f.job()
        f.conn.execute("UPDATE papers SET title='changed'")
        f.conn.commit()
        observer = Mock()
        self.assertEqual(self.reconcile(on_success=observer), 0)
        observer.assert_not_called()
        self.assertEqual(f.journal()["state"], "blocked")
        self.assertEqual(f.journal()["error_code"], "metadata_drift")

    def test_observer_failure_does_not_retry_commit_or_skip_later_jobs(self):
        f = self.fixture
        self.stage_pair()
        observer = Mock(side_effect=RuntimeError("observer failed"))
        with patch("time.time", return_value=100), self.assertLogs("pipeline.publication", level="WARNING"):
            self.assertEqual(self.reconcile(limit=2, on_success=observer), 2)
        self.assertEqual(observer.call_count, 2)
        self.assertEqual(f.completions, ["p", "q"])
        for paper in ("p", "q"):
            self.assertEqual(f.journal(paper)["state"], "published")
            self.assertIsNone(f.journal(paper)["error_code"])
