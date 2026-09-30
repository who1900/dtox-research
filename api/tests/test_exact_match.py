import unittest
from unittest.mock import patch

from api import main
from api.exact_match import TitleIndex, extract_ids

ROWS = [
    ("1803.05069", "HotStuff: BFT Consensus in the Lens of Blockchain", 900),
    ("2010.11454", "Fast-HotStuff: A Fast and Resilient HotStuff Protocol", 50),
    ("2303.11366", "Reflexion: Language Agents with Verbal Reinforcement Learning", 800),
    ("wp:flashbots-mev", "Flash Boys 2.0: Frontrunning, Transaction Reordering and Consensus Instability", 0),
    ("wp:solana", "Solana: A new architecture for a high performance blockchain", 0),
    ("1706.03762", "Attention Is All You Need", 90000),
    ("a1", "Attention: a survey of things", 1),
    ("a2", "Attention: another survey", 1),
    ("s1", "Smart Contracts: Vulnerabilities", 1),
    ("s2", "Smart Contracts: Languages", 1),
    ("eip:7702", "Set Code for EOAs", 0),
    ("2304.03442", "Generative Agents: Interactive Simulacra of Human Behavior", 4000),
    ("2105.08206", "LEWIS: Levenshtein Editing for Unsupervised Text Style Transfer", 5),
    ("l1", "Lookup Arguments: Improvements, Extensions and Applications", 3),
    ("1904.05234", "Flash Boys 2.0: Frontrunning, Transaction Reordering, and Consensus Instability", 900),
    ("oa:1", "Flash Boys 2.0: Frontrunning in Decentralized Exchanges", 800),
    ("oa:2", "HotStuff", 5000),
] + [(f"y{i}", f"Lookup tables arguments and generative agents study {i}", 1) for i in range(60)] + [(f"x{i}", f"Smart contracts and attention topic {i}", 1) for i in range(400)]


class ExtractIdsTest(unittest.TestCase):
    def test_spelled_ids(self):
        self.assertEqual(extract_ids("EIP-7702 set code"), ["eip:7702"])
        self.assertEqual(extract_ids("what does ERC-4337 do"), ["eip:4337"])
        self.assertEqual(extract_ids("SIMD-96 priority fee"), ["simd:0096"])
        self.assertEqual(extract_ids("see arXiv 1803.05069v2 please"), ["1803.05069"])
        self.assertEqual(extract_ids("iacr:2019/953 and eprint 2020/1"), ["iacr:2019/953", "iacr:2020/1"])
        self.assertEqual(extract_ids("wp:solana gh:anchor:be73"), ["wp:solana", "gh:anchor:be73"])

    def test_no_false_ids(self):
        self.assertEqual(extract_ids("version 2.0 costs 12.50 in 2019"), [])
        self.assertEqual(extract_ids("model of 3.1415926 accuracy"), [])


class TitleIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = TitleIndex(ROWS)

    def ids(self, query):
        return [d for d, _ in self.index.match(query)]

    def test_name_inside_a_long_keyword_query(self):
        self.assertEqual(self.ids("HotStuff BFT consensus with linear view change and pipelining for blockchain")[0],
                         "1803.05069")

    def test_short_name(self):
        self.assertEqual(self.ids("Reflexion verbal reinforcement")[0], "2303.11366")

    def test_multiword_name_and_whitepaper(self):
        self.assertEqual(self.ids("Flash Boys 2.0 frontrunning")[0], "1904.05234")
        self.assertEqual(self.ids("Solana whitepaper proof of history")[0], "wp:solana")

    def test_full_title(self):
        self.assertEqual(self.ids("Attention Is All You Need transformer"), ["1706.03762"])

    def test_generic_phrase_is_a_name_only_when_written_as_one(self):
        self.assertEqual(self.ids("Generative Agents memory stream")[:1], ["2304.03442"])
        self.assertEqual(self.ids("generative agents in games"), [])
        self.assertEqual(self.ids("zkVM lookup arguments Jolt Lasso"), [])

    def test_rare_but_obscure_word_is_not_a_name(self):
        self.assertEqual(self.ids("retrieval augmented generation Lewis"), [])

    def test_copies_of_one_title_do_not_hide_it_and_stubs_come_last(self):
        self.assertEqual(self.ids("Flash Boys 2.0")[0], "1904.05234")
        self.assertEqual(self.ids("HotStuff protocol")[0], "1803.05069")

    def test_common_words_are_not_names(self):
        self.assertEqual(self.ids("attention"), [])
        self.assertEqual(self.ids("attention mechanisms in transformers"), [])
        self.assertEqual(self.ids("smart contracts security"), [])
        self.assertEqual(self.ids("solana consensus"), [])


class InjectExactTest(unittest.TestCase):
    def _r(self, pid, score=0.8):
        return {"arxiv_id": pid, "score": score, "niche_score": 1, "rank_score": score}

    def test_named_paper_goes_first_and_twins_collapse(self):
        found = [self._r(f"a{i}") for i in range(5)] + [self._r("eip:7702")]
        with patch.object(main, "_exact_matches", return_value=[("eip:7702", "exact id")]), \
             patch.object(main, "_collapse_twins", side_effect=lambda r: r):
            out = main._inject_exact(found, "EIP-7702", [], 4, 5)
        self.assertEqual(out[0]["arxiv_id"], "eip:7702")
        self.assertEqual(out[0]["found_via"], "exact id")
        self.assertEqual(len(out), 4)
        self.assertEqual(sum(r["arxiv_id"] == "eip:7702" for r in out), 1)

    def test_missing_paper_is_scored_with_the_callers_filters_but_not_layer(self):
        fresh = {"arxiv_id": "eip:7702", "score": 0.7, "title": "Set Code for EOAs"}
        year = {"key": "year", "range": {"gte": 2024}}
        with patch.object(main, "_exact_matches", return_value=[("eip:7702", "exact id")]), \
             patch.object(main, "_score_specific_papers", return_value=[fresh]) as scored, \
             patch.object(main, "_paper_facts", return_value={"eip:7702": 3}), \
             patch.object(main, "_collapse_twins", side_effect=lambda r: r):
            out = main._inject_exact([self._r("a")], "EIP-7702", [year], 8, 5)
        self.assertEqual([r["arxiv_id"] for r in out], ["eip:7702", "a"])
        args, kwargs = scored.call_args
        self.assertIsNone(args[2])
        self.assertEqual(kwargs["constraints"], [year])
        self.assertEqual(out[0]["niche_score"], 3)

    def test_excluded_by_filters_stays_out(self):
        with patch.object(main, "_exact_matches", return_value=[("eip:7702", "exact id")]), \
             patch.object(main, "_score_specific_papers", return_value=[]):
            out = main._inject_exact([self._r("a")], "EIP-7702", [], 8, 5)
        self.assertEqual([r["arxiv_id"] for r in out], ["a"])


class PlacementTest(unittest.TestCase):
    def test_ids_lead_and_titles_take_at_most_third_place(self):
        rest = [{"arxiv_id": f"r{i}"} for i in range(6)]
        ident = {"arxiv_id": "eip:1", "found_via": "exact id"}
        title = {"arxiv_id": "t", "found_via": "title match"}
        with patch.object(main, "canonical_id", side_effect=lambda x: x):
            out = main._place_named([title, ident], rest)
            self.assertEqual([r["arxiv_id"] for r in out][:4], ["eip:1", "r0", "t", "r1"])
            rest[4]["arxiv_id"] = "t"
            better = main._place_named([dict(title)], [{"arxiv_id": "t"}] + rest[:3])
        self.assertEqual(better[0]["arxiv_id"], "t")


class MinScoreTest(unittest.TestCase):
    def _run(self, **kw):
        hits = [{"id": i, "score": s, "payload": {"arxiv_id": f"p{i}", "title": "t"}}
                for i, s in enumerate([0.90, 0.85, 0.80, 0.75, 0.72])]
        body = main.SearchBody(query="unit-test zzz query", limit=8, **kw)
        with patch.object(main, "embed_query", return_value=[0.0]), \
             patch.object(main, "HIER_SEARCH", False), \
             patch.object(main, "PAPER_LEXICAL", False), \
             patch.object(main, "two_phase_dense_search", return_value=(hits, [], [], [])), \
             patch.object(main, "_bm25_candidates", return_value=[]), \
             patch.object(main, "_paper_facts", return_value={}), \
             patch.object(main, "_exact_matches", return_value=[]), \
             patch.object(main, "_lookup_note", return_value=None), \
             patch.object(main, "AUTO_RELAX", False), \
             patch.object(main.search_cache, "get", return_value=None), \
             patch.object(main.search_cache, "set"):
            return main._run_search(body)

    def test_high_min_score_is_capped_and_flagged(self):
        out = self._run(layer="web3", min_score=0.9)
        self.assertEqual(out["min_score_note"]["requested"], 0.9)
        self.assertAlmostEqual(out["min_score_note"]["applied"], 0.73)
        self.assertEqual(out["count"], 4)
        flags = {r["arxiv_id"]: r.get("below_min_score") for r in out["results"]}
        self.assertTrue(flags["p2"])
        self.assertIsNone(flags["p0"])

    def test_modest_or_lower_min_score_is_honoured_silently(self):
        out = self._run(layer="web3", min_score=0.72)
        self.assertNotIn("min_score_note", out)
        out = self._run(min_score=0.6)
        self.assertNotIn("min_score_note", out)
        self.assertEqual(out["count"], 5)


class SoftLayerTest(unittest.TestCase):
    def test_partner_rules(self):
        self.assertEqual(main._soft_partner("ai-agents", "long term memory"), "llm-slm")
        self.assertEqual(main._soft_partner("llm-slm", "agent memory for LLMs"), "ai-agents")
        self.assertIsNone(main._soft_partner("llm-slm", "kv cache compression"))
        self.assertIsNone(main._soft_partner("web3", "agent memory"))
        self.assertIsNone(main._soft_partner(None, "agent memory"))

    def test_merge_interleaves_and_dedupes(self):
        a = {"results": [{"arxiv_id": "x"}, {"arxiv_id": "y"}]}
        b = {"results": [{"arxiv_id": "memgpt"}, {"arxiv_id": "y"}]}
        with patch.object(main, "_collapse_twins", side_effect=lambda r: r):
            merged = main._merge_soft_layers(a, b, 3)
        ids = [r["arxiv_id"] for r in merged]
        self.assertEqual(ids[0], "y")
        self.assertIn("memgpt", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_search_merges_the_partner_layer(self):
        calls = []

        def fake(body, *a):
            calls.append((body.layer, a[-1] if a else None))
            rows = {"ai-agents": [{"arxiv_id": "a1"}], "llm-slm": [{"arxiv_id": "memgpt"}]}
            return {"results": list(rows[body.layer]), "count": 1}
        real = main._run_search
        with patch.object(main, "_run_search", side_effect=lambda *a, **k: fake(*a) if a[-1] is False else real(*a, **k)), \
             patch.object(main, "_inject_exact", side_effect=lambda r, *a: r), \
             patch.object(main, "_collapse_twins", side_effect=lambda r: r), \
             patch.object(main.search_cache, "get", return_value=None), \
             patch.object(main.search_cache, "set"):
            out = real(main.SearchBody(query="agent memory paging", layer="ai-agents"), None, 5, 5, 5, True)
        self.assertEqual({r["arxiv_id"] for r in out["results"]}, {"a1", "memgpt"})
        self.assertEqual(out["layer_expanded"]["also_searched"], "llm-slm")
        self.assertEqual(sorted(c[0] for c in calls), ["ai-agents", "llm-slm"])


class FoundationalFallbackTest(unittest.TestCase):
    def test_pool_without_citation_data_is_not_emptied(self):
        pool = [{"arxiv_id": f"eip:{n}", "title": "t", "year": 2024, "citation_count": None,
                 "layers": ["web3"], "relevance_rank": n} for n in (1, 2, 3)]

        class Conn:
            def execute(self, sql, params):
                class R:
                    def fetchall(self_inner):
                        return [("eip:3", 4)] if "dst IN" in sql else []
                return R()
        with patch.object(main, "_ro_conn", return_value=Conn()), \
             patch.object(main, "_twins", return_value={"checked": 1e18, "mtime": 1, "members": {}, "canonical": {}}):
            out = main._foundational(pool, main.PapersBody(query="account abstraction"))
        self.assertEqual([r["arxiv_id"] for r in out], ["eip:3", "eip:1", "eip:2"])
        self.assertEqual(out[0]["cited_by_corpus"], 4)


class RouteTest(unittest.TestCase):
    def test_spec_route_takes_ids_with_a_slash(self):
        for route in main.app.routes:
            if getattr(route, "path", "") == "/v1/paper/{arxiv_id:path}/spec":
                m = route.path_regex.match("/v1/paper/iacr:2019/953/spec")
                self.assertIsNotNone(m)
                self.assertEqual(m.groupdict()["arxiv_id"], "iacr:2019/953")
                break
        else:
            self.fail("spec route missing")

    def test_section_and_card_routes_still_split_correctly(self):
        hits = {}
        for route in main.app.routes:
            regex = getattr(route, "path_regex", None)
            if regex is not None:
                for url in ("/v1/paper/iacr:2019/953", "/v1/paper/iacr:2019/953/section",
                            "/v1/paper/iacr:2019/953/similar", "/v1/paper/iacr:2019/953/spec"):
                    if regex.match(url) and url not in hits:
                        hits[url] = route.path
        self.assertEqual(hits["/v1/paper/iacr:2019/953/spec"], "/v1/paper/{arxiv_id:path}/spec")
        self.assertEqual(hits["/v1/paper/iacr:2019/953/section"], "/v1/paper/{paper_id:path}/section")
        self.assertEqual(hits["/v1/paper/iacr:2019/953/similar"], "/v1/paper/{paper_id:path}/similar")
        self.assertEqual(hits["/v1/paper/iacr:2019/953"], "/v1/paper/{paper_id:path}")


if __name__ == "__main__":
    unittest.main()
