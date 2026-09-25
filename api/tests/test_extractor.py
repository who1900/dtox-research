import unittest

from pipeline.extractor import (CHUNK_CHAR_LIMIT, classify_section,
                                should_embed, split_long_text, strip_comments)


class ExtractorTests(unittest.TestCase):
    def test_proofs_are_not_mislabeled_as_introduction(self):
        self.assertEqual(classify_section("Proofs of Theorem 1", 0, 5)[0], "appendix")

    def test_unknown_specific_heading_stays_other(self):
        self.assertEqual(classify_section("Maximum likelihood estimator", 0, 5)[0], "other")

    def test_long_chunks_respect_embedding_limit(self):
        parts = split_long_text("word " * 2000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= CHUNK_CHAR_LIMIT for part in parts))

    def test_only_low_value_figures_are_skipped(self):
        self.assertFalse(should_embed({"section_type": "appendix", "element_type": "figure"}))
        self.assertTrue(should_embed({"section_type": "appendix", "element_type": "equation"}))

    def test_strip_comments_preserves_escaped_percent(self):
        self.assertEqual(strip_comments("value \\% kept % removed"), "value \\% kept ")


if __name__ == "__main__":
    unittest.main()
