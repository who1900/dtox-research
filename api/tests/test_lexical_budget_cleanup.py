import sqlite3
import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import Mock, patch

from api import main


class FakeFtsConnection:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.handler = None
        self.handlers = []

    def set_progress_handler(self, callback, steps):
        self.handler = callback
        self.handlers.append((callback, steps))

    def execute(self, _sql, _params):
        if self.error:
            raise self.error
        return self

    def fetchall(self):
        return self.rows


class LexicalBudgetCleanupTests(unittest.TestCase):
    def test_success_clears_progress_handler(self):
        conn = FakeFtsConnection([("paper:1", -1.0)])
        with (patch.object(main, "_ro_conn", return_value=conn),
              patch.object(main, "fts_query_any", return_value="term"),
              patch.object(main.time, "monotonic", return_value=0)):
            self.assertEqual(main._paper_bm25("term"), ["paper:1"])

        self.assertEqual(conn.handlers[-1], (None, 0))
        self.assertIsNone(conn.handler)

    def test_empty_match_is_empty_not_a_budget_timeout(self):
        with (patch.object(main, "fts_query_any", return_value=""),
              patch.object(main, "_ro_conn") as open_conn):
            self.assertEqual(main._paper_bm25("punctuation only"), [])
        open_conn.assert_not_called()

    def test_budget_interrupt_propagates_and_cleans_handler_without_retry(self):
        conn = FakeFtsConnection(error=sqlite3.OperationalError("interrupted"))
        with (patch.object(main, "_ro_conn", return_value=conn) as open_conn,
              patch.object(main, "_ro_conn_drop") as drop_conn,
              patch.object(main, "fts_query_any", return_value="term"),
              patch.object(main, "LEXICAL_QUERY_TIMEOUT", 1),
              patch.object(main.time, "monotonic", side_effect=[0, 2])):
            with self.assertRaises(FuturesTimeoutError):
                main._paper_bm25("term")

        self.assertEqual(conn.handlers[-1], (None, 0))
        drop_conn.assert_not_called()
        self.assertEqual(open_conn.call_count, 1)

    def test_shortlist_distinguishes_future_timeout_from_empty_matches(self):
        timeout_future = Mock()
        timeout_future.result.side_effect = FuturesTimeoutError()
        with self.assertRaises(FuturesTimeoutError):
            main._lexical_paper_shortlist([], "term", None, None, None,
                                          lexical_future=timeout_future)
        timeout_future.result.assert_called_once_with(timeout=main.LEXICAL_QUERY_TIMEOUT)

        empty_future = Mock()
        empty_future.result.return_value = []
        self.assertEqual(
            main._lexical_paper_shortlist([], "term", None, None, None,
                                          lexical_future=empty_future),
            ([], []),
        )
        empty_future.result.assert_called_once_with(timeout=main.LEXICAL_QUERY_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
