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


# Two records kept (title+abstract present, not deleted) + one deleted record
# that must never reach upsert_discovered, + a resumptionToken (list not done).
OAI_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-09-26T00:00:00Z</responseDate>
  <ListRecords>
    <record>
      <header>
        <identifier>oai:arXiv.org:2609.00001</identifier>
        <datestamp>2026-09-20</datestamp>
        <setSpec>cs</setSpec>
      </header>
      <metadata>
        <arXiv:arXiv xmlns:arXiv="http://arxiv.org/OAI/arXiv/">
          <arXiv:id>2609.00001</arXiv:id>
          <arXiv:created>2026-09-20</arXiv:created>
          <arXiv:categories>cs.CL cs.LG</arXiv:categories>
          <arXiv:title>  A   Study of
          Language Models  </arXiv:title>
          <arXiv:abstract>  We study   language models
          in depth.  </arXiv:abstract>
        </arXiv:arXiv>
      </metadata>
    </record>
    <record>
      <header status="deleted">
        <identifier>oai:arXiv.org:2609.00002</identifier>
        <datestamp>2026-09-20</datestamp>
        <setSpec>cs</setSpec>
      </header>
    </record>
    <record>
      <header>
        <identifier>oai:arXiv.org:2609.00003</identifier>
        <datestamp>2026-09-20</datestamp>
        <setSpec>cs</setSpec>
      </header>
      <metadata>
        <arXiv:arXiv xmlns:arXiv="http://arxiv.org/OAI/arXiv/">
          <arXiv:id>2609.00003</arXiv:id>
          <arXiv:created>2026-09-20</arXiv:created>
          <arXiv:categories>cs.CR</arXiv:categories>
          <arXiv:title>Agentic Blockchain Consensus</arXiv:title>
          <arXiv:abstract>Zero knowledge proof rollup for smart contracts.</arXiv:abstract>
        </arXiv:arXiv>
      </metadata>
    </record>
    <resumptionToken cursor="0" completeListSize="5">abc123token</resumptionToken>
  </ListRecords>
</OAI-PMH>
"""

# Final page of the same day: one more record, empty resumptionToken (no text)
# signals the list is complete.
OAI_FINAL_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:arXiv.org:2609.00004</identifier>
        <datestamp>2026-09-20</datestamp>
      </header>
      <metadata>
        <arXiv:arXiv xmlns:arXiv="http://arxiv.org/OAI/arXiv/">
          <arXiv:id>2609.00004</arXiv:id>
          <arXiv:created>2026-09-20</arXiv:created>
          <arXiv:title>Another Paper</arXiv:title>
          <arXiv:abstract>Some abstract.</arXiv:abstract>
        </arXiv:arXiv>
      </metadata>
    </record>
    <resumptionToken cursor="3" completeListSize="5"/>
  </ListRecords>
</OAI-PMH>
"""

OAI_NO_RECORDS = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <request verb="ListRecords">https://oaipmh.arxiv.org/oai</request>
  <error code="noRecordsMatch">No records match</error>
</OAI-PMH>
"""

OAI_BAD_TOKEN = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <error code="badResumptionToken">token expired</error>
</OAI-PMH>
"""


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    service.init_db(conn)
    return conn


class OaiParseTests(unittest.TestCase):
    """pipeline.service._arxiv_oai_parse_records, no network."""

    def test_extracts_id_year_title_abstract_and_skips_deleted(self):
        entries, token, restart = service._arxiv_oai_parse_records(OAI_FIXTURE.encode())
        self.assertFalse(restart)
        self.assertEqual(token, "abc123token")
        ids = [e["arxiv_id"] for e in entries]
        self.assertEqual(ids, ["2609.00001", "2609.00003"])  # deleted record skipped

        first = entries[0]
        self.assertEqual(first["year"], 2026)
        self.assertEqual(first["title"], "A Study of Language Models")
        self.assertEqual(first["abstract"], "We study language models in depth.")

    def test_year_comes_from_id_not_oai_created(self):
        # arXiv's OAI returned <created>2026-09-23 for 1110.0569 (a 2011 paper)
        self.assertEqual(service._year_from_arxiv_id("1110.0569"), 2011)
        self.assertEqual(service._year_from_arxiv_id("2609.00001"), 2026)
        self.assertEqual(service._year_from_arxiv_id("cs/9901001"), 1999)
        self.assertEqual(service._year_from_arxiv_id("math.NA/0501001"), 2005)
        self.assertIsNone(service._year_from_arxiv_id("oa:W123"))

    def test_no_resumption_token_means_page_is_final(self):
        entries, token, restart = service._arxiv_oai_parse_records(OAI_FINAL_PAGE.encode())
        self.assertFalse(restart)
        self.assertIsNone(token)
        self.assertEqual([e["arxiv_id"] for e in entries], ["2609.00004"])

    def test_no_records_match_is_a_genuinely_empty_day_not_an_error(self):
        entries, token, restart = service._arxiv_oai_parse_records(OAI_NO_RECORDS.encode())
        self.assertEqual(entries, [])
        self.assertIsNone(token)
        self.assertFalse(restart)

    def test_bad_resumption_token_signals_restart_not_data_loss(self):
        entries, token, restart = service._arxiv_oai_parse_records(OAI_BAD_TOKEN.encode())
        self.assertEqual(entries, [])
        self.assertIsNone(token)
        self.assertTrue(restart)


class OaiHarvestStepTests(unittest.TestCase):
    """Exercises the real harvest_step dispatch for oai: cursors, with
    pick_next_query and fetch_arxiv_oai_page mocked (no network, no scheduler
    randomness) so only the code path under test runs."""

    def test_oai_cursor_advances_across_two_pages_and_closes_on_completion(self):
        conn = make_conn()
        query_key = "oai:cs@2026-09-20"
        service.get_cursor(conn, query_key, layer=service.ARXIV_OAI_LAYER)

        page1 = service._arxiv_oai_parse_records(OAI_FIXTURE.encode())
        page2 = service._arxiv_oai_parse_records(OAI_FINAL_PAGE.encode())

        with patch.object(service, "fetch_arxiv_oai_page", side_effect=[page1]), \
             patch.object(service, "pick_next_query",
                          side_effect=[(service.ARXIV_OAI_LAYER, query_key), None]):
            new_count_1 = service.harvest_step(conn)

        self.assertEqual(new_count_1, 2)  # page 1: 2 kept, 1 deleted skipped
        next_start, done = service.get_cursor(conn, query_key)
        self.assertEqual(done, 0)  # more pages queued
        self.assertEqual(service.get_resume_token(conn, query_key), "abc123token")

        with patch.object(service, "fetch_arxiv_oai_page", side_effect=[page2]), \
             patch.object(service, "pick_next_query",
                          side_effect=[(service.ARXIV_OAI_LAYER, query_key), None]):
            new_count_2 = service.harvest_step(conn)

        self.assertEqual(new_count_2, 1)
        next_start, done = service.get_cursor(conn, query_key)
        self.assertEqual(done, 1)  # day complete: final page carried no resumptionToken
        self.assertIsNone(service.get_resume_token(conn, query_key))

        ids = {r["arxiv_id"] for r in conn.execute("SELECT arxiv_id FROM papers").fetchall()}
        self.assertEqual(ids, {"2609.00001", "2609.00003", "2609.00004"})

    def test_bad_resumption_token_restarts_day_without_marking_done(self):
        conn = make_conn()
        query_key = "oai:cs@2026-09-19"
        service.get_cursor(conn, query_key, layer=service.ARXIV_OAI_LAYER)
        service.set_oai_cursor(conn, query_key, 2, 0, "stale-token")

        with patch.object(service, "fetch_arxiv_oai_page",
                          return_value=([], None, True)), \
             patch.object(service, "pick_next_query",
                          side_effect=[(service.ARXIV_OAI_LAYER, query_key), None]):
            service.harvest_step(conn)

        next_start, done = service.get_cursor(conn, query_key)
        self.assertEqual(done, 0)  # restarted, not closed
        self.assertIsNone(service.get_resume_token(conn, query_key))

    def test_transient_failure_leaves_cursor_and_token_untouched(self):
        conn = make_conn()
        query_key = "oai:cs@2026-09-18"
        service.get_cursor(conn, query_key, layer=service.ARXIV_OAI_LAYER)
        service.set_oai_cursor(conn, query_key, 2, 0, "keep-me")

        with patch.object(service, "fetch_arxiv_oai_page", return_value=None), \
             patch.object(service, "pick_next_query",
                          side_effect=[(service.ARXIV_OAI_LAYER, query_key), None]):
            service.harvest_step(conn)

        next_start, done = service.get_cursor(conn, query_key)
        self.assertEqual((next_start, done), (2, 0))
        self.assertEqual(service.get_resume_token(conn, query_key), "keep-me")


class ExportApiDisabledTests(unittest.TestCase):
    """ARXIV_EXPORT_API_ENABLED=0 must skip export-API cursors untouched while
    leaving IACR/OAI (and everything else) running."""

    def test_export_query_left_untouched_when_disabled(self):
        conn = make_conn()
        query_key = "bq:llm-slm:0@2020"
        service.get_cursor(conn, query_key, layer="llm-slm")
        service.set_cursor(conn, query_key, 42, 0)  # pretend progress was already made

        def boom(*a, **kw):
            raise AssertionError("fetch_page must not run while the export API is disabled")

        with patch.object(service, "ARXIV_EXPORT_API_ENABLED", False), \
             patch.object(service, "fetch_page", side_effect=boom), \
             patch.object(service, "pick_next_query",
                          side_effect=[("llm-slm", query_key), None]):
            new_count = service.harvest_step(conn)

        self.assertEqual(new_count, 0)
        next_start, done = service.get_cursor(conn, query_key)
        self.assertEqual(next_start, 42)  # offset untouched
        self.assertEqual(done, 0)          # not marked done, so it resumes once re-enabled

    def test_iacr_and_oai_cursors_still_run_when_export_api_disabled(self):
        conn = make_conn()
        iacr_key = "iacr@2020-01"
        oai_key = "oai:cs@2026-09-20"
        service.get_cursor(conn, iacr_key, layer="web3")
        service.get_cursor(conn, oai_key, layer=service.ARXIV_OAI_LAYER)
        oai_page = service._arxiv_oai_parse_records(OAI_FINAL_PAGE.encode())

        with patch.object(service, "ARXIV_EXPORT_API_ENABLED", False), \
             patch.object(service, "fetch_iacr_month", return_value=[
                 {"arxiv_id": "iacr:2020/1", "title": "T", "year": 2020, "abstract": "a"}
             ]) as fake_iacr, \
             patch.object(service, "fetch_arxiv_oai_page", return_value=oai_page) as fake_oai, \
             patch.object(service, "pick_next_query",
                          side_effect=[("web3", iacr_key), (service.ARXIV_OAI_LAYER, oai_key), None]):
            new_count = service.harvest_step(conn)

        self.assertEqual(fake_iacr.call_count, 1)
        self.assertEqual(fake_oai.call_count, 1)
        self.assertEqual(new_count, 2)
        ids = {r["arxiv_id"] for r in conn.execute("SELECT arxiv_id FROM papers").fetchall()}
        self.assertIn("iacr:2020/1", ids)
        self.assertIn("2609.00004", ids)


if __name__ == "__main__":
    unittest.main()
