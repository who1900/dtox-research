import pathlib
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

# pipeline/service.py imports its sibling modules (extractor, niche_filter) as
# bare top-level names, so it only imports cleanly when pipeline/ itself is on
# sys.path (how the deployed service is run) -- pipeline.service is not enough
# on its own. Test-only path fixup; nothing under pipeline/ is changed.
_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

# pipeline/service.py does `import fcntl` at module scope for its single-instance
# lock (used only in main(), never in the code paths under test here). fcntl is
# POSIX-only, so importing pipeline.service on a Windows dev box fails before
# any of our code even runs. This installs a no-op stub so the module can be
# imported for testing on any platform; on a real POSIX host the real fcntl is
# already present and this branch is skipped entirely.
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


class SpecsAreAlwaysWeb3Tests(unittest.TestCase):
    TITLE = "Fee market change for ETH 1.0 chain"
    ABSTRACT = "A transaction pricing mechanism with a fixed per-block fee that is burned."

    def _row(self, conn, pid):
        return conn.execute("SELECT status, layers FROM papers WHERE arxiv_id=?", (pid,)).fetchone()

    def test_eip_without_niche_words_is_admitted_as_web3(self):
        conn = make_conn()
        service.upsert_discovered(conn, "eip:1559", self.TITLE, 2019, "web3", abstract=self.ABSTRACT)
        row = self._row(conn, "eip:1559")
        self.assertEqual(row["status"], "discovered")
        self.assertIn("web3", row["layers"].split(","))

    def test_off_niche_eip_is_resurrected_on_reread(self):
        conn = make_conn()
        conn.execute("INSERT INTO papers (arxiv_id, title, layers, status) VALUES ('simd:0085','x','','off_niche')")
        service.upsert_discovered(conn, "simd:0085", "Fee collector constraints", 2023, "web3",
                                  abstract="Validators must keep the collector account rent exempt.")
        self.assertEqual(self._row(conn, "simd:0085")["status"], "discovered")

    def test_arxiv_paper_still_goes_through_the_gate(self):
        conn = make_conn()
        service.upsert_discovered(conn, "2401.99999", "Cooking pasta at altitude", 2024, "web3",
                                  abstract="We measure boiling points of water in mountain kitchens.")
        self.assertEqual(self._row(conn, "2401.99999")["status"], "off_niche")


if __name__ == "__main__":
    unittest.main()
