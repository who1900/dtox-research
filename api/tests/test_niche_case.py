import pathlib
import sys
import unittest

_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

import niche_filter as nf  # noqa: E402


def terms(text, layer):
    return nf.niche_score(text)[layer]["matched_terms"]


class CaseSensitiveTermsTest(unittest.TestCase):
    def test_mev_upper_case_matches_and_mev_units_do_not(self):
        self.assertIn("mev", terms("MEV extraction on Ethereum", "web3"))
        self.assertIn("mev", terms("Sandwich attacks and MEVs", "web3"))
        title = "indirect dark matter searches with MeV photons"
        self.assertNotIn("mev", terms(title, "web3"))
        self.assertEqual(nf.niche_match(title), [])
        self.assertEqual(nf.niche_match("gamma rays above 100 mev"), [])

    def test_mev_boost_and_maximal_extractable_are_untouched(self):
        self.assertIn("mev-boost", terms("mev-boost relays", "web3"))
        self.assertIn("maximal extractable", terms("Maximal Extractable Value", "web3"))

    def test_moe_matches_only_as_an_abbreviation(self):
        self.assertIn("moe", terms("Sparse MoE routing", "llm-slm"))
        self.assertNotIn("moe", terms("Moe Tsuchiya on wave motion", "llm-slm"))

    def test_other_ambiguous_names(self):
        self.assertIn("eagle", terms("EAGLE speculative decoding", "llm-slm"))
        self.assertNotIn("eagle", terms("Eagle populations in the Alps", "llm-slm"))
        self.assertIn("yarn", terms("YaRN context extension", "llm-slm"))
        self.assertNotIn("yarn", terms("Spinning cotton yarn on ring frames", "llm-slm"))
        self.assertIn("stark", terms("A STARK prover", "web3"))
        self.assertNotIn("stark", terms("a stark contrast between the two", "web3"))

    def test_snark_and_wormhole(self):
        self.assertIn("snark", terms("Succinct SNARKs from lattices", "web3"))
        self.assertNotIn("snark", terms("Snarks and edge colourings of cubic graphs", "web3"))
        self.assertIn("wormhole", terms("Bridging tokens over Wormhole", "web3"))
        self.assertNotIn("wormhole", terms("Traversable wormholes in general relativity", "web3"))

    def test_swift_needs_its_capital_and_rust_stays_case_insensitive(self):
        self.assertIn("swift", terms("Building iOS apps in Swift", "builder-tech"))
        self.assertIn("rust", terms("a rust program for SBF", "builder-tech"))
        self.assertNotIn("swift", terms("the swift decline of fish stocks", "builder-tech"))

    def test_ordinary_terms_still_match_any_case(self):
        self.assertIn("llm", terms("An llm agent benchmark", "llm-slm"))
        self.assertIn("kv cache", terms("KV-Cache compression", "llm-slm"))
        self.assertIn("rollup", terms("ROLLUP fees", "web3"))
        self.assertEqual(nf.niche_match("MEV-Boost and Proposer-Builder Separation in Ethereum"), ["web3"])

    def test_case_sensitive_terms_are_all_real_terms(self):
        known = {t for group in (nf.BROAD_TERMS, nf.SPECIFIC_TERMS)
                 for terms_ in group.values() for t in terms_}
        self.assertLessEqual(set(nf.CASE_SENSITIVE_TERMS), known)

    def test_language_only_rule_is_kept(self):
        scores = nf.niche_score("Written in Rust")
        self.assertEqual(nf.admitted_layers(scores), [])


if __name__ == "__main__":
    unittest.main()
