import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from unittest.mock import Mock, patch

from api import exact_match, main
from api.exact_match import TitleIndex


class TitleIndexCheckpointTests(unittest.TestCase):
    def test_complete_state_and_matches_are_unchanged(self):
        rows = [
            ("1803.05069", "HotStuff: BFT Consensus in the Lens of Blockchain", 900),
            ("oa:hotstuff", "HotStuff", 5000),
            ("wp:solana", "Solana: A new architecture for a high performance blockchain", 0),
            ("2303.11366", "Reflexion: Language Agents with Verbal Reinforcement Learning", 800),
            ("eip:7702", "Set Code for EOAs", 0),
            ("simd:0096", "Priority fees", 0),
            ("gh:anchor", "Anchor documentation", 0),
            ("empty", "", 0),
            ("punctuation", "!!!", None),
        ] + [(f"f{i}", f"Generative Agents filler study {i}", 1) for i in range(300)]
        original = TitleIndex(rows)
        checkpoint = Mock()
        cooperative = TitleIndex(iter(rows), checkpoint=checkpoint)
        self.assertEqual(vars(cooperative), vars(original))
        self.assertEqual(checkpoint.call_count, 6)
        for query in ("HotStuff consensus", "Solana whitepaper", "Reflexion reinforcement",
                      "Generative Agents filler study 128", "generative agents", "!!!"):
            with self.subTest(query=query):
                self.assertEqual(cooperative.match(query), original.match(query))

    def test_checkpoint_starts_each_loop_and_runs_every_128_rows(self):
        rows = [(f"p{i}", f"Named paper title {i}", 1) for i in range(300)]
        calls = []
        with patch.object(exact_match, "tokens", wraps=exact_match.tokens) as tokenize:
            TitleIndex(rows, checkpoint=lambda: calls.append(tokenize.call_count))
        self.assertEqual(calls, [0, 128, 256, 300, 428, 556])

    def test_empty_constructor_still_checkpoints_both_loops(self):
        checkpoint = Mock()
        self.assertEqual(vars(TitleIndex([], checkpoint=checkpoint)), vars(TitleIndex([])))
        self.assertEqual(checkpoint.call_count, 2)


class TitleBuildPriorityTests(unittest.TestCase):
    def setUp(self):
        self.condition = threading.Condition()
        for name, value in (("TITLE_BUILD_PRIORITY", True),
                            ("_TITLE_BUILD_PRIORITY_CONDITION", self.condition),
                            ("_ACTIVE_LEXICAL_CALLS", 0)):
            patcher = patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_counter_and_notification_reset_on_all_exit_paths(self):
        for outcome in ("success", "empty", "sqlite_error", "error", "timeout"):
            with self.subTest(outcome=outcome):
                conn = Mock()
                conn.execute.return_value.fetchall.return_value = [("p1", -2), ("p2", -1)]
                if outcome == "sqlite_error":
                    conn.execute.side_effect = sqlite3.DatabaseError("failed")
                elif outcome == "error":
                    conn.execute.side_effect = RuntimeError("failed")
                elif outcome == "timeout":
                    conn.execute.side_effect = FuturesTimeoutError()

                def match(_query):
                    self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 1)
                    return "" if outcome == "empty" else '"term"'

                with (patch.object(main, "fts_query_any", side_effect=match),
                      patch.object(main, "_ro_conn", return_value=conn),
                      patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", False),
                      patch.object(main.time, "monotonic", return_value=0),
                      patch.object(self.condition, "notify_all", wraps=self.condition.notify_all) as notify):
                    if outcome in ("error", "timeout"):
                        error = RuntimeError if outcome == "error" else FuturesTimeoutError
                        with self.assertRaises(error):
                            main._paper_bm25("term")
                    else:
                        expected = ["p1", "p2"] if outcome == "success" else []
                        self.assertEqual(main._paper_bm25("term"), expected)
                    self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 0)
                    notify.assert_called_once_with()
                    if outcome == "empty":
                        conn.execute.assert_not_called()
                    else:
                        self.assertEqual(conn.set_progress_handler.call_args_list[0].args[1], 10000)
                        self.assertEqual(conn.set_progress_handler.call_args_list[-1].args, (None, 0))

    def test_disabled_flag_bypasses_tracking(self):
        with (patch.object(main, "TITLE_BUILD_PRIORITY", False),
              patch.object(main, "fts_query_any", return_value=""),
              patch.object(self.condition, "notify_all") as notify):
            self.assertEqual(main._paper_bm25(""), [])
        self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 0)
        notify.assert_not_called()

    def test_concurrent_calls_decrement_independently(self):
        entered = {query: threading.Event() for query in ("first", "second")}
        release = {query: threading.Event() for query in entered}

        def match(query):
            entered[query].set()
            if not release[query].wait(timeout=1):
                raise AssertionError("lexical test did not release worker")
            return ""

        with patch.object(main, "fts_query_any", side_effect=match):
            with ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    first = pool.submit(main._paper_bm25, "first")
                    second = pool.submit(main._paper_bm25, "second")
                    self.assertTrue(entered["first"].wait(timeout=1))
                    self.assertTrue(entered["second"].wait(timeout=1))
                    with self.condition:
                        self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 2)
                    release["first"].set()
                    self.assertEqual(first.result(timeout=1), [])
                    with self.condition:
                        self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 1)
                    self.assertFalse(second.done())
                    release["second"].set()
                    self.assertEqual(second.result(timeout=1), [])
                    self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 0)
                finally:
                    for event in release.values():
                        event.set()

    def test_checkpoint_wait_is_bounded_even_when_lexical_stays_active(self):
        for active in (0, 1, 2):
            with self.subTest(active=active):
                main._ACTIVE_LEXICAL_CALLS = active

                def wait(predicate, *, timeout):
                    self.assertEqual(timeout, .05)
                    return predicate()

                with patch.object(self.condition, "wait_for", side_effect=wait) as wait_for:
                    main._yield_title_build()
                wait_for.assert_called_once()
                self.assertEqual(main._ACTIVE_LEXICAL_CALLS, active)

    def test_builder_preserves_default_constructor_contract(self):
        rows = [("p1", "Named paper title", 1)]
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                conn = Mock()
                conn.execute.return_value.fetchall.return_value = rows
                state = {"index": None, "built": 0, "loading": True}
                with (patch.object(main, "TITLE_BUILD_PRIORITY", enabled),
                      patch.object(main, "_ro_conn", return_value=conn),
                      patch.object(main, "_TITLE_INDEX", state),
                      patch.object(main, "TitleIndex") as constructor):
                    main._build_title_index()
                    if enabled:
                        constructor.assert_called_once_with(rows, checkpoint=main._yield_title_build)
                    else:
                        constructor.assert_called_once_with(rows)
                    self.assertIs(state["index"], constructor.return_value)
                    self.assertFalse(state["loading"])

    def test_sql_rank_order_filters_and_limits_are_unchanged(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id, title, abstract, layers, year)")
        conn.executemany("INSERT INTO papers_fts VALUES (?, ?, ?, ?, ?)", [
            ("p1", "Relative position attention", "transformer study", "llm-slm", 2020),
            ("p2", "Transformer study", "relative position attention", "llm-slm", 2021),
            ("p3", "Relative attention", "position study", "llm-slm", 2022),
            ("p4", "Relative position attention", "transformer study", "web3", 2023),
        ])
        with (patch.object(main, "_ro_conn", return_value=conn),
              patch.object(main, "PAPER_LEXICAL_SOURCE_BALANCE", False)):
            for filters in ({}, {"layer": "llm-slm", "limit": 2},
                            {"year_from": 2021, "year_to": 2022, "limit": 1}):
                with self.subTest(filters=filters):
                    expected = main._paper_bm25.__wrapped__("relative position attention", **filters)
                    self.assertTrue(expected)
                    for enabled in (False, True):
                        with patch.object(main, "TITLE_BUILD_PRIORITY", enabled):
                            self.assertEqual(main._paper_bm25("relative position attention", **filters), expected)
                    self.assertEqual(main._ACTIVE_LEXICAL_CALLS, 0)
        self.assertEqual(main._paper_bm25.__name__, "_paper_bm25")


if __name__ == "__main__":
    unittest.main()
