import pathlib
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# See test_oai_harvest.py for why pipeline/ must be on sys.path and fcntl
# stubbed -- same fixup, repeated here so this file runs standalone too.
_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *a, **kw: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

from pipeline import service  # noqa: E402


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    service.init_db(conn)
    return conn


def make_response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = ""
    resp.json.return_value = json_data or {"results": []}
    return resp


class OpenAlexBackoffTests(unittest.TestCase):
    """pipeline.service.openalex_harvest_step: persisted 429 backoff, no
    network in any of these -- session.get is a MagicMock throughout."""

    def test_429_with_retry_after_sets_persistent_pause_and_skips_next_call(self):
        conn = make_conn()
        session = MagicMock()
        session.get.return_value = make_response(429, headers={"Retry-After": "8555"})

        with patch.object(service, "openalex_gate"):
            taken = service.openalex_harvest_step(conn, session)

        self.assertEqual(taken, 0)
        self.assertEqual(session.get.call_count, 1)

        paused_until = float(service._sched_get(conn, service.OPENALEX_PAUSE_KEY))
        import time
        # 8555s clamped into [60, 86400] -> stays 8555, roughly "now + 8555"
        self.assertAlmostEqual(paused_until, time.time() + 8555, delta=5)

        # Second call must not touch the network at all: the pause is checked
        # up front and the function returns silently.
        session.get.reset_mock()
        with patch.object(service, "openalex_gate"):
            taken2 = service.openalex_harvest_step(conn, session)
        self.assertEqual(taken2, 0)
        self.assertEqual(session.get.call_count, 0)

    def test_429_without_retry_after_falls_back_to_default_window(self):
        conn = make_conn()
        session = MagicMock()
        session.get.return_value = make_response(429, headers={})

        with patch.object(service, "openalex_gate"):
            service.openalex_harvest_step(conn, session)

        paused_until = float(service._sched_get(conn, service.OPENALEX_PAUSE_KEY))
        import time
        self.assertAlmostEqual(
            paused_until, time.time() + service.OPENALEX_RETRY_AFTER_FALLBACK, delta=5)

    def test_retry_after_clamped_to_bounds(self):
        conn = make_conn()
        import time

        # Below the floor -> clamped up to OPENALEX_RETRY_AFTER_MIN.
        service._openalex_set_pause(conn, "1")
        low = float(service._sched_get(conn, service.OPENALEX_PAUSE_KEY))
        self.assertAlmostEqual(low, time.time() + service.OPENALEX_RETRY_AFTER_MIN, delta=5)

        # Above the ceiling -> clamped down to OPENALEX_RETRY_AFTER_MAX.
        service._openalex_set_pause(conn, "999999999")
        high = float(service._sched_get(conn, service.OPENALEX_PAUSE_KEY))
        self.assertAlmostEqual(high, time.time() + service.OPENALEX_RETRY_AFTER_MAX, delta=5)

    def test_api_key_added_to_params_when_env_set(self):
        conn = make_conn()
        session = MagicMock()
        session.get.return_value = make_response(200, json_data={"results": []})

        with patch.object(service, "openalex_gate"), \
             patch.object(service, "OPENALEX_API_KEY", "secret-key-123"):
            service.openalex_harvest_step(conn, session)

        self.assertEqual(session.get.call_count, 1)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"].get("api_key"), "secret-key-123")

    def test_no_api_key_omits_param_when_env_unset(self):
        conn = make_conn()
        session = MagicMock()
        session.get.return_value = make_response(200, json_data={"results": []})

        with patch.object(service, "openalex_gate"), \
             patch.object(service, "OPENALEX_API_KEY", ""):
            service.openalex_harvest_step(conn, session)

        _, kwargs = session.get.call_args
        self.assertNotIn("api_key", kwargs["params"])

    def test_fresh_cursor_picked_before_regular_cursor(self):
        conn = make_conn()
        service.openalex_seed_cursors(conn)
        # Give the regular (non-fresh) cursor for query 0 an older/empty
        # last_run_at so naive ORDER BY COALESCE(last_run_at,'') would pick it
        # first; fresh must still win regardless.
        conn.execute(
            "UPDATE harvest_cursor SET last_run_at=NULL WHERE query_key='openalex@0'")
        conn.execute(
            "UPDATE harvest_cursor SET last_run_at=NULL WHERE query_key='openalex-fresh@0'")
        conn.commit()

        session = MagicMock()
        session.get.return_value = make_response(200, json_data={"results": []})

        with patch.object(service, "openalex_gate"), \
             patch.object(service, "_poll_due", return_value=False):
            service.openalex_harvest_step(conn, session)

        row = conn.execute(
            "SELECT query_key FROM harvest_cursor WHERE last_run_at IS NOT NULL "
            "ORDER BY last_run_at LIMIT 1").fetchone()
        self.assertEqual(row["query_key"], "openalex-fresh@0")


if __name__ == "__main__":
    unittest.main()
