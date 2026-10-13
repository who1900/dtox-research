import unittest

from api.evidence import provenance


class EvidenceSourceClassificationTests(unittest.TestCase):
    def test_legacy_latex_conflict_uses_only_path_extension(self):
        for url in ("https://example.test/docs/guide.md",
                    "https://example.test/docs/guide.MDX?download=1"):
            with self.subTest(url=url):
                evidence = provenance("unrelated:id", {"text": r"\begin{equation}x=1\end{equation}"},
                                      {"fulltext_source": "latex", "source_url": url})
                self.assertEqual(evidence["acquisition_type"], "spec-markdown")
                self.assertEqual(evidence["stored_acquisition_type"], "latex")
                self.assertFalse(evidence["exact_latex"])

    def test_query_string_does_not_supply_markdown_extension(self):
        evidence = provenance("unrelated:id", {"text": "Markdown formula x=1"},
                              {"fulltext_source": "latex",
                               "source_url": "https://example.test/docs/guide?format=md"})
        self.assertEqual(evidence["acquisition_type"], "latex")
        self.assertTrue(evidence["exact_latex"])

    def test_parser_mode_does_not_relabel_pdf_acquisition(self):
        evidence = provenance("unrelated:id", {"text": r"\begin{equation}x=1\end{equation}"},
                              {"fulltext_source": "oa-mirror-pdf", "parser_mode": "markdown"})
        self.assertEqual(evidence["acquisition_type"], "oa-mirror-pdf")
        self.assertEqual(evidence["extraction_type"], "pdf_text")
        self.assertFalse(evidence["exact_latex"])

    def test_pdf_and_abstract_sources_are_never_exact_latex(self):
        for acquisition, expected in (("oa-mirror-pdf", "pdf_text"),
                                      ("openalex-abstract", "abstract_text")):
            with self.subTest(acquisition=acquisition):
                evidence = provenance("record:one", {"text": r"\begin{equation}x=1\end{equation}"},
                                      {"fulltext_source": acquisition})
                self.assertEqual(evidence["extraction_type"], expected)
                self.assertFalse(evidence["exact_latex"])


if __name__ == "__main__":
    unittest.main()
