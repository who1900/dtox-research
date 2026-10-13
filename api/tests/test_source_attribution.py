import unittest
from unittest.mock import patch
from api import main
from api.evidence import provenance
from api.source_attribution import curated_source, normalize_title, valid_source_url


class SourceAttributionTests(unittest.TestCase):
    def setUp(self):
        main._source_url_for_id.cache_clear()

    def test_stored_metadata_urls_resolve_for_paper_and_pdf_fields(self):
        for field in ("source_url", "link", "pdf_url"):
            with self.subTest(field=field):
                url = "https://papers.example.org/whitepaper.pdf"
                rows = {"wp:example": {field: url}}
                output = main._hydrate_evidence([{"arxiv_id": "wp:example", "text": "chunk"}], rows)[0]
                self.assertEqual(output["url"], url)
                self.assertEqual(output["evidence"]["source_url"], url)
                self.assertFalse(output["provenance_missing"])

    def test_url_validation_rejects_poisoned_and_malformed_values(self):
        for value in ("javascript:alert(1)", "file:///etc/passwd", "https://", "https://x:bad/a",
                      " https://example.org", "https://bad host/path"):
            with self.subTest(value=value):
                self.assertIsNone(valid_source_url(value))
        out = main._hydrate_evidence(
            [{"arxiv_id": "wp:unverified", "url": "javascript:alert(1)", "text": "chunk"}],
            {"wp:unverified": {"source_url": "javascript:alert(2)"}})[0]
        self.assertIsNone(out["url"])
        self.assertEqual(out["source"], "Industry whitepaper")
        self.assertEqual(out["source_url_status"], "source_missing")
        self.assertTrue(out["evidence"]["provenance_missing"])

    def test_poisoned_stored_url_falls_back_to_valid_payload_url(self):
        result = {"arxiv_id": "wp:example", "url": "https://papers.example.org/current.pdf"}
        out = main._hydrate_evidence([result], {"wp:example": {"source_url": "javascript:bad()"}})[0]
        self.assertEqual(out["url"], result["url"])
        self.assertEqual(out["evidence"]["source_url"], result["url"])
        self.assertEqual(out["source_url_origin"], "payload")

    def test_unknown_ids_do_not_receive_synthetic_arxiv_urls(self):
        self.assertIsNone(main.source_url("wp:mastercoin"))
        self.assertIsNone(main.source_url("invented-paper-id"))
        self.assertEqual(main.source_url("2401.12345"), "https://arxiv.org/abs/2401.12345")

    def test_payload_resolution_does_not_use_per_id_metadata_reads(self):
        main._source_url_for_id.cache_clear()
        output = main._hydrate_evidence(
            [{"arxiv_id": "wp:stored", "text": "chunk"}],
            {"wp:stored": {"pdf_url": "https://papers.example.org/a.pdf"}})[0]
        self.assertEqual(output["url"], "https://papers.example.org/a.pdf")
        self.assertEqual(main._source_url_for_id.cache_info().misses, 0)

    def test_catalog_requires_matching_id_and_normalized_title(self):
        self.assertEqual(normalize_title("Uniswap v4 Core"), "uniswapv4core")
        self.assertIsNotNone(curated_source("wp:uniswap-v4", "Uniswap v4 Core"))
        self.assertIsNone(curated_source("wp:uniswap-v4", "Wrong title"))
        chainlink = curated_source("wp:chainlink-v2", "Chainlink 2.0: Next Steps in the Evolution of Decentralized Oracle Networks")
        self.assertEqual(chainlink["source_url_status"], "inconclusive")

    def test_curated_whitepaper_urls_and_legacy_alias(self):
        cases = (
            ("wp:chainlink-v1", "Chainlink: A Decentralized Oracle Network", "https://research.chain.link/whitepaper-v1.pdf"),
            ("wp:ethereum-yellowpaper", "Ethereum: A Secure Decentralised Generalised Transaction Ledger (Yellow Paper)", "https://ethereum.github.io/yellowpaper/paper.pdf"),
            ("wp:uniswap-v4", "Uniswap v4 Core", "https://app.uniswap.org/whitepaper-v4.pdf"),
            ("wp:mastercoin", "Tendermint: Consensus without Mining", "https://tendermint.com/static/docs/tendermint.pdf"),
        )
        for pid, title, url in cases:
            with self.subTest(pid=pid):
                rows = {pid: {"title": title, "source_url": None}}
                hit = {"arxiv_id": pid, "title": title, "text": "fragment"}
                hydrated = main._hydrate_evidence([hit], rows)[0]
                self.assertEqual(hydrated["url"], url)
                self.assertEqual(hydrated["evidence"]["source_url_origin"], "curated_historical_list")
                self.assertFalse(hydrated["evidence"]["provenance_missing"])
                if pid == "wp:mastercoin":
                    self.assertIn("legacy ID", hydrated["legacy_id_alias"])

    def test_unknown_stale_whitepaper_title_has_no_catalog_fallback(self):
        hydrated = main._hydrate_evidence(
            [{"arxiv_id": "wp:uniswap-v4", "title": "Different work", "text": "fragment"}], {})[0]
        self.assertIsNone(hydrated["url"])
        self.assertEqual(hydrated["source"], "Industry whitepaper")
        self.assertTrue(hydrated["evidence"]["provenance_missing"])

    def test_hydration_and_provenance_share_the_same_safe_url(self):
        rows = {"wp:consistent": {"source_url": "https://papers.example.org/a"}}
        result = {"arxiv_id": "wp:consistent", "text": "body"}
        hydrated = main._hydrate_evidence([result], rows)[0]
        self.assertEqual(hydrated["url"], hydrated["evidence"]["source_url"])
        self.assertEqual(provenance("wp:consistent", result, rows["wp:consistent"], hydrated["url"])
                         ["source_url"], hydrated["url"])

    def test_all_catalog_title_keys_are_computed_from_titles(self):
        from api.source_attribution import _catalog
        for entry in _catalog().values():
            with self.subTest(paper_id=entry["id"]):
                actual = curated_source(entry["id"], entry["title"])
                if entry.get("url"):
                    self.assertEqual(actual["url"], entry["url"])
                else:
                    self.assertIsNone(actual)

    def test_non_whitepaper_ids_do_not_load_catalog(self):
        with patch("api.source_attribution._catalog", side_effect=AssertionError("catalog read")):
            self.assertIsNone(curated_source("acl:paper", "Paper title"))

    def test_catalog_failure_degrades_to_missing_provenance(self):
        with patch("api.source_attribution._catalog", return_value={}):
            hydrated = main._hydrate_evidence(
                [{"arxiv_id": "wp:unavailable", "title": "Unknown work"}], {})[0]
        self.assertIsNone(hydrated["url"])
        self.assertTrue(hydrated["evidence"]["provenance_missing"])

    def test_passport_spec_and_compare_keep_matching_source_provenance(self):
        pid = "wp:uniswap-v4"
        title = "Uniswap v4 Core"
        payload = {"arxiv_id": pid, "title": title, "text": "equation fragment",
                   "element_type": "equation", "section_type": "experiments",
                   "section_title": "Results"}
        rows = {pid: {"title": title, "source_url": None}}
        with (patch.object(main, "auth_and_limit"),
              patch.object(main, "_evidence_rows", return_value=rows),
              patch.object(main, "qdrant_scroll_by_arxiv",
                           side_effect=[[{"payload": payload}], [{"payload": payload}], [{"payload": payload}]])):
            spec = main.paper_spec(pid)
            compared = main.compare(pid, pid)
        expected_url = "https://app.uniswap.org/whitepaper-v4.pdf"
        for response in (spec, compared["a"], compared["b"]):
            self.assertEqual(response["url"], expected_url)
            self.assertEqual(response["source_url_status"], "verified_live")
            self.assertEqual(response["evidence"]["source_url"], expected_url)
            self.assertEqual(response["evidence"]["source_url_status"], "verified_live")
        self.assertEqual(spec["sections"][0]["evidence"]["source_url"], expected_url)
        self.assertEqual(compared["a"]["results_chunks"][0]["evidence"]["source_url"], expected_url)

    def test_hit_hydration_passport_and_section_preserve_catalog_attribution(self):
        pid = "wp:uniswap-v4"
        title = "Uniswap v4 Core"
        payload = {"arxiv_id": pid, "title": title, "text": "fragment",
                   "section_type": "experiments", "element_type": "equation"}
        rows = {pid: {"title": title, "source_url": None}}
        hit = main._result_from_hit({"payload": payload, "score": 0.9})
        hydrated = main._hydrate_evidence([hit], rows)[0]
        passport = main._paper_passport(pid, payload, rows)
        section_evidence = provenance(
            pid, payload, main._passport_source_metadata(pid, rows, passport), passport["url"],
            scope="indexed_chunk")
        self.assertEqual(hydrated["url"], passport["url"])
        for evidence in (hydrated["evidence"], passport["evidence"], section_evidence):
            self.assertEqual(evidence["source_url"], passport["url"])
            self.assertEqual(evidence["source_url_origin"], "curated_historical_list")
            self.assertEqual(evidence["source_url_status"], "verified_live")

    def test_compact_candidates_preserve_source_provenance_fields(self):
        full = {"results": [], "source_candidates": [{
            "arxiv_id": "wp:x", "source": "Industry whitepaper", "url": None,
            "source_url_origin": None, "source_url_status": "source_missing",
            "provenance_missing": True, "legacy_id_alias": "legacy",
            "evidence": {"provenance_missing": True},
        }]}
        compact = main._compact_search(full)["source_candidates"][0]
        for key in ("source_url_origin", "source_url_status", "provenance_missing", "legacy_id_alias"):
            self.assertIn(key, compact)


if __name__ == "__main__":
    unittest.main()
