import unittest

from pipeline import extractor


class ScientificMarkdownProfileTests(unittest.TestCase):
    def test_explicit_structures_remain_typed_and_code_is_not_reinterpreted(self):
        equation = "$$\nx = \\sum_i y_i\n$$"
        table = "<table><tr><td>Model</td><td>Score</td></tr></table>"
        gfm = "| Model | Score |\n| --- | --- |\n| A | 9 |\n"
        code = "```html\n<table><tr><td>example</td></tr></table>\n```"
        text = "## Method\nBody\n\n" + equation + "\n\n" + table + "\n\n" + gfm + "\n" + code
        elements, sections = [], []
        chunks, _ = extractor.build_chunks_markdown(
            text, document_kind="paper", element_sink=elements, section_sink=sections)
        self.assertEqual([e["element_type"] for e in elements],
                         ["prose", "equation", "table", "table", "code"])
        self.assertEqual(elements[1]["text"], equation)
        self.assertEqual(elements[2]["text"], table)
        self.assertEqual(elements[-1]["text"], code)
        self.assertTrue(next(c for c in chunks if c["element_type"] == "equation")["has_math"])
        self.assertEqual(len(sections), 1)

    def test_paper_headings_are_classified_and_display_number_is_removed(self):
        text = ("# 1 Introduction\nContext\n"
                "## **4. Methodology**\nProcedure\n"
                "## 5 Results\nOutcome\n"
                "## 6.1 Limitations\nCaveat\n"
                "## References\nSources")
        chunks, mode = extractor.build_chunks_markdown(text, document_kind="paper")
        self.assertEqual(mode, "markdown")
        self.assertEqual([c["section_title"] for c in chunks],
                         ["Introduction", "Methodology", "Results", "Limitations", "References"])
        self.assertEqual([c["section_type"] for c in chunks],
                         ["introduction", "method", "experiments", "limitations", "appendix"])

    def test_title_author_preamble_stays_distinct_from_abstract(self):
        chunks, _ = extractor.build_chunks_markdown(
            "A Research Paper\nJane Doe, Example University\n"
            "# **Abstract**\nWe study the problem.\n"
            "# **2 Related Work**\nPrior approaches.",
            document_kind="paper")
        self.assertEqual([c["section_title"] for c in chunks],
                         ["Document preamble", "Abstract", "Related Work"])
        self.assertEqual([c["section_type"] for c in chunks],
                         ["other", "introduction", "related_work"])
        self.assertIn("A Research Paper", chunks[0]["text"])
        self.assertIn("Jane Doe", chunks[0]["text"])
        self.assertEqual(chunks[1]["text"], "We study the problem.")

    def test_default_spec_profile_keeps_existing_heading_behavior(self):
        chunks, _ = extractor.build_chunks_markdown("## 4. Methodology\nDetails")
        self.assertEqual(chunks[0]["section_title"], "4. Methodology")
        self.assertEqual(chunks[0]["section_type"], "other")

    def test_paper_title_does_not_classify_author_block_as_methods(self):
        chunks, _ = extractor.build_chunks_markdown(
            "# **Council of LLMs: Evaluating Large Language Models**\nJane Doe\n"
            "## **Abstract**\nOur study.\n## **4 Methods**\nOur procedure.",
            document_kind="paper")
        self.assertEqual([c["section_type"] for c in chunks],
                         ["other", "introduction", "method"])
        self.assertEqual([c["text"] for c in chunks if c["section_type"] == "method"],
                         ["Our procedure."])


if __name__ == "__main__":
    unittest.main()
