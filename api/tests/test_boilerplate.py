import unittest
from unittest.mock import patch

from api import main
from api.boilerplate import is_boilerplate_chunk
from pipeline.extractor import should_embed

PREAMBLE = ("(#1)\n\n501em$\blacklozenge$\n=0pt=0\n\nbxn\n\nbartdef[1]\begin definition#1 "
            "end definition\n\nthmTheorem lemLemma propProposition corCorollary")
AUTHORS = ("Davide Mancino _University of Milano-Bicocca_ Milano, Italy davide.mancino@unimib.it "
           "Marco Rossi _University of Turin_ Turin, Italy marco.rossi@unito.it")
AUTHORS_TRUNC = "logie sup´erieure_ Montreal, Canada zeinab...@ens.etsmtl.ca"
ACM = ("printacmref=false, printfolios=true, @shellescape=1draftminted [CCS '23] Proceedings of the "
       "ACM 979-8-4007-0050-7/23/11 10.1145/3576915.3616579")
CHECKLIST = ("NeurIPS Paper Checklist\n\nQuestion: Do the main claims made in the abstract and "
             "introduction accurately reflect the paper's contributions?\nAnswer: [Yes]\n"
             "Justification: See Section 4.")
REFS = ("- [1] Nakamoto S 2008 Bitcoin: A peer-to-peer electronic cash system\n"
        "- [2] Czepluch J S 2015 The use of block chain technology arXiv preprint arXiv:1512.01\n"
        "- [3] Buterin V 2014 Ethereum white paper In Proceedings pp. 1-36")
MAKETITLE = ("Teams of LLM Agents can Exploit Vulnerabilities\n\nYuxuan Zhu1,\n Antony Kellermann2\n\n"
             "University of Illinois Urbana Champaign\n\nAugust 2, 2026\n" + "=" * 60)

PROSE = ("We propose a retrieval model that combines dense and lexical signals. The key idea is that "
         "the ranking of a passage depends on both the query and the surrounding document, which we "
         "show improves recall on all of the benchmarks we consider in this paper.")
PROSE_WITH_LATEX = ("Let $x \in \mathbb{R}^d$ and let $\mathcal{L}(\theta)$ denote the loss. We show "
                    "that the gradient of \emph{the loss} is bounded by $\|\nabla L\| \le C$ for "
                    "all $\theta$ in the ball, which is the assumption used in the proof.")
PROSE_WITH_EMAIL = ("Please contact us at team@example.org if you would like to reproduce these "
                    "experiments, since the code and the data are available on request from the authors.")


class DetectorTest(unittest.TestCase):
    def test_junk_is_detected(self):
        for name, text in [("preamble", PREAMBLE), ("authors", AUTHORS), ("truncated", AUTHORS_TRUNC),
                           ("acm", ACM), ("checklist", CHECKLIST), ("refs", REFS),
                           ("title block", MAKETITLE)]:
            with self.subTest(name):
                self.assertTrue(is_boilerplate_chunk(text, "full text", "other", "prose"))

    def test_junk_titles(self):
        for title in ("References", "**References**", "Bibliography", "Acknowledgments",
                      "NeurIPS Paper Checklist", "\section*{Acknowledgements}"):
            with self.subTest(title):
                self.assertTrue(is_boilerplate_chunk("Some short text.", title, "other", "prose"))

    def test_content_is_kept(self):
        for name, text in [("prose", PROSE), ("latex prose", PROSE_WITH_LATEX),
                           ("email in prose", PROSE_WITH_EMAIL)]:
            with self.subTest(name):
                self.assertFalse(is_boilerplate_chunk(text, "Method", "method", "prose"))

    def test_structured_elements_are_never_junk(self):
        eq = r"\begin{equation} \newcommand{\x}{#1} \def\y#1{#1} \mathcal{L} = \sum_i x_i \end{equation}"
        for etype in ("equation", "algorithm", "code", "table"):
            with self.subTest(etype):
                self.assertFalse(is_boilerplate_chunk(PREAMBLE, "References", "other", etype))
        self.assertFalse(is_boilerplate_chunk(eq, "Method", "method", "equation"))

    def test_regular_titles_are_kept(self):
        for title in ("Introduction", "Related Work", "Reference Architecture", "full text"):
            with self.subTest(title):
                self.assertFalse(is_boilerplate_chunk(PROSE, title, "other", "prose"))

    def test_should_embed_uses_detector(self):
        junk = {"section_type": "other", "section_title": "full text", "element_type": "prose",
                "text": PREAMBLE}
        good = dict(junk, text=PROSE)
        self.assertFalse(should_embed(junk))
        self.assertTrue(should_embed(good))


def _hit(aid, score, text):
    return {"payload": {"arxiv_id": aid, "text": text, "section_title": "full text",
                        "section_type": "other", "element_type": "prose", "title": aid},
            "score": score}


class DedupePreferContentTest(unittest.TestCase):
    def ids(self, hits, limit=10):
        return [r["arxiv_id"] for r in
                main._dedupe_preferring_content(hits, 0.0, set(), limit)]

    def test_content_chunk_replaces_junk_chunk(self):
        hits = [_hit("A", 0.9, PREAMBLE), _hit("B", 0.8, PROSE), _hit("A", 0.7, PROSE)]
        out = main._dedupe_preferring_content(hits, 0.0, set(), 10)
        self.assertEqual([r["arxiv_id"] for r in out], ["A", "B"])
        self.assertEqual(out[0]["text"], PROSE)
        self.assertEqual(out[0]["score"], 0.7)

    def test_junk_only_paper_goes_below_content_papers(self):
        hits = [_hit("A", 0.9, PREAMBLE), _hit("B", 0.8, PROSE), _hit("C", 0.7, PROSE)]
        self.assertEqual(self.ids(hits), ["B", "C", "A"])

    def test_junk_only_paper_drops_a_bounded_distance(self):
        hits = [_hit("A", 0.9, PREAMBLE)] + [_hit(c, 0.8 - i / 100, PROSE) for i, c in enumerate("BCDEFG")]
        with patch.object(main, "BOILERPLATE_DEMOTE", 3):
            self.assertEqual(self.ids(hits), ["B", "C", "D", "A", "E", "F", "G"])

    def test_junk_only_paper_is_not_removed(self):
        self.assertEqual(self.ids([_hit("A", 0.9, AUTHORS)]), ["A"])

    def test_floor_and_limit_still_apply(self):
        hits = [_hit("A", 0.9, PROSE), _hit("B", 0.2, PROSE), _hit("C", 0.8, PROSE)]
        out = main._dedupe_preferring_content(hits, 0.5, set(), 1)
        self.assertEqual([r["arxiv_id"] for r in out], ["A"])

    def test_clean_input_keeps_order(self):
        hits = [_hit("A", 0.9, PROSE), _hit("B", 0.8, PROSE), _hit("A", 0.7, PROSE)]
        self.assertEqual(self.ids(hits), ["A", "B"])


if __name__ == "__main__":
    unittest.main()
