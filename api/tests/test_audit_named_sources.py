import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from api import main


def named_hit(pid="eip:4337", score=0.62):
    return {"arxiv_id": pid, "score": score, "title": "ERC-4337 EntryPoint",
            "year": 2023, "section_type": "method", "section_title": "Security",
            "text": "The EntryPoint is a central trust point.", "terms": [],
            "layers": ["web3"], "url": "https://eips.ethereum.org/EIPS/eip-4337",
            "evidence": {"fragment_scope": "indexed_chunk", "fragment_hash": "test-hash",
                         "source": "qdrant"}}


class NamedSourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(main, "JUDGMENTS_DB_PATH",
                                              os.path.join(temp, "judgments.db")))
        values = {
            "auth_and_limit": ("test", {}),
            "_corpus_stats": {"papers_indexed": 100, "by_layer": {"web3": 20}},
            "_paper_facts": {},
            "_twins": {"canonical": {}},
            "trends": {},
            "_graph_coverage": {},
            "_topic_coverage": (5, {"web3": 5}, 0.8),
            "_foreign_scope_domain": None,
        }
        for name, value in values.items():
            self.stack.enter_context(patch.object(main, name, return_value=value))
        for name in ("prime_query_embeddings", "prime_qdrant_searches"):
            self.stack.enter_context(patch.object(main, name))
        self.stack.enter_context(patch.object(main, "_hydrate_evidence",
                                              side_effect=lambda hits: hits))
        self.stack.enter_context(patch.object(main, "_run_search",
                                              return_value={"results": []}))
        self.stack.enter_context(patch.object(main.requests, "get",
                                              side_effect=AssertionError("offline")))
        self.stack.enter_context(patch.object(main.requests, "post",
                                              side_effect=AssertionError("offline")))

    def report(self, claims, depth="fast", evidence_per_claim=4):
        with patch.object(main, "_score_specific_papers", return_value=[named_hit()]) as score:
            result = main.validate_project(main.ValidateBody(
                idea="ERC-4337 account abstraction security",
                claims=claims, layer="web3", depth=depth,
                evidence_per_claim=evidence_per_claim))
        return result, score

    def test_named_id_source_survives_fast_and_full_audits(self):
        for depth in ("fast", "full"):
            with self.subTest(depth=depth):
                with patch.object(main, "_score_specific_papers", return_value=[named_hit()]), \
                     patch.object(main, "paper_spec", return_value={"sections": []}), \
                     patch.object(main, "qdrant_scroll_by_arxiv", return_value=[
                         {"payload": {"section_type": "limitations", "text": "Trusted entry point."}}
                     ]):
                    result = main.validate_project(main.ValidateBody(
                        idea="ERC-4337 account abstraction security",
                        claims=["ERC-4337 EntryPoint is a central trust point"],
                        layer="web3", depth=depth))
                claim = result["claims"][0]
                self.assertEqual(claim["named_sources"][0]["id"], "eip:4337")
                self.assertTrue(claim["named_sources"][0]["found"])
                self.assertTrue(claim["named_sources"][0]["verification_required"])
                self.assertEqual(claim["named_sources"][0]["url"], named_hit()["url"])
                if depth == "full":
                    self.assertEqual(claim["known_limitations"][0]["found_via"],
                                     "explicitly named ID")
                    self.assertFalse(claim["known_limitations"][0]["from_cited_paper"])

    def test_weak_named_source_does_not_promote_verdict_or_band(self):
        result, _ = self.report(["ERC-4337 EntryPoint central trust point"])
        claim = result["claims"][0]
        self.assertEqual(claim["named_sources"][0]["band"], "weak")
        self.assertEqual(claim["verdict"], "weak_signal_only")
        self.assertEqual(claim["named_sources"][0]["score"], 0.62)
        self.assertIn("not proof", claim["named_sources"][0]["note"])
        self.assertTrue(claim["verification_required"])

    def test_evidence_cap_cannot_drop_named_source_record(self):
        def several(query, *args, **kwargs):
            return [named_hit(score=0.91)]
        with patch.object(main, "_score_specific_papers", side_effect=several), \
             patch.object(main, "_run_search", return_value={"results": [
                 {**named_hit("ordinary", 0.91), "arxiv_id": "ordinary"},
                 {**named_hit("other", 0.90), "arxiv_id": "other"},
             ]}):
            result = main.validate_project(main.ValidateBody(
                idea="ERC-4337 account abstraction security",
                claims=["ERC-4337 EntryPoint central trust point"],
                layer="web3", depth="fast", evidence_per_claim=1))
        claim = result["claims"][0]
        self.assertTrue(any(s["id"] == "eip:4337" for s in claim["named_sources"]))

    def test_missing_id_is_reported_but_not_injected_as_prior_art(self):
        with patch.object(main, "_score_specific_papers", return_value=[]) as score:
            result = main.validate_project(main.ValidateBody(
                idea="account abstraction security",
                claims=["ERC-4337 central trust point"], layer="web3", depth="fast"))
        self.assertEqual(score.call_args.args[2], "web3")
        claim = result["claims"][0]
        self.assertEqual(claim["named_sources"][0]["status"],
                         "not_returned_by_indexed_lookup")
        self.assertEqual(claim["verdict"], "no_match_in_corpus")
        self.assertEqual(claim["evidence"], [])

    def test_idea_id_is_fallback_only_and_claims_do_not_inherit_sibling_ids(self):
        idea = "account abstraction security"
        claims = ["ERC-4337 EntryPoint trust point", "unrelated mechanism claim"]
        with patch.object(main, "_score_specific_papers", return_value=[named_hit()]) as score:
            result = main.validate_project(main.ValidateBody(
                idea=idea, claims=claims, layer="web3", depth="fast"))
        self.assertEqual(score.call_count, 1)
        self.assertEqual(result["claims"][0]["named_sources"][0]["id"], "eip:4337")
        self.assertEqual(result["claims"][1]["named_sources"], [])

    def test_multiclaim_audit_does_not_inherit_multiple_idea_ids(self):
        with patch.object(main, "_score_specific_papers", return_value=[named_hit()]) as score:
            result = main.validate_project(main.ValidateBody(
                idea="Compare ERC-4337 with ERC-7579 account abstraction",
                claims=["first security claim", "second security claim"],
                layer="web3", depth="fast"))
        score.assert_not_called()
        self.assertEqual([c["named_sources"] for c in result["claims"]], [[], []])

    def test_existing_search_hit_is_reused_and_layer_filter_is_respected(self):
        with patch.object(main, "_run_search", return_value={"results": [named_hit()]}), \
             patch.object(main, "_score_specific_papers", return_value=[]) as score:
            result = main.validate_project(main.ValidateBody(
                idea="account abstraction security",
                claims=["ERC-4337 EntryPoint central trust point"],
                layer="web3", depth="fast"))
        score.assert_not_called()
        source = result["claims"][0]["named_sources"][0]
        self.assertEqual(source["retrieval_path"], "ordinary_claim_retrieval")
        self.assertTrue(source["provenance"]["fragment_hash"])

    def test_no_ids_means_no_explicit_lookup_and_layer_is_passed_through(self):
        with patch.object(main, "_score_specific_papers", return_value=[named_hit()]) as score:
            result = main.validate_project(main.ValidateBody(
                idea="abstract account security", claims=["a new mechanism"],
                layer="web3", depth="fast"))
        score.assert_not_called()
        self.assertEqual(result["claims"][0]["named_sources"], [])

    def test_lookup_error_is_not_novelty_evidence(self):
        with patch.object(main, "_score_specific_papers", side_effect=RuntimeError("offline")):
            result = main.validate_project(main.ValidateBody(
                idea="account abstraction security",
                claims=["ERC-4337 central trust point"], layer="web3", depth="fast"))
        claim = result["claims"][0]
        self.assertEqual(claim["named_sources"][0]["status"], "lookup_unavailable")
        self.assertEqual(claim["verdict"], "no_match_in_corpus")


if __name__ == "__main__":
    unittest.main()
