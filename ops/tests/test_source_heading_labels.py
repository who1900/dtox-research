import unittest

from ops import source_heading_labels


def cached(index, title, text, kind="other"):
    return {"chunk_index": index, "section_title": title, "section_type": kind,
            "text": text, "element_type": "prose", "has_math": False,
            "has_algorithm": False}


class SourceHeadingLabelTests(unittest.TestCase):
    def test_same_source_headings_map_labels_even_when_cached_chunking_differs(self):
        source = "# **4. Methodology**\nAlpha beta"
        cache = [cached(0, "4. Methodology", "Alpha"),
                 cached(1, "4. Methodology", "beta")]

        result = source_heading_labels.candidate(cache, source)

        self.assertTrue(result["source_heading_verified"])
        self.assertFalse(result["exact_text_ids"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["full_label_map"], [
            {"chunk_index": 0, "section_title": "Methodology", "section_type": "method"},
            {"chunk_index": 1, "section_title": "Methodology", "section_type": "method"},
        ])
        self.assertEqual([chunk["text"] for chunk in cache], ["Alpha", "beta"])
        self.assertEqual([chunk["chunk_index"] for chunk in cache], [0, 1])

    def test_exact_chunk_identity_resolves_repeated_heading_by_source_order(self):
        source = ("# Introduction\n## Misc\nOne.\n"
                  "# Limitations\n## Misc\nTwo.")
        result = source_heading_labels.candidate(
            [cached(0, "Misc", "One."), cached(1, "Misc", "Two.")], source)
        self.assertTrue(result["source_heading_verified"])
        self.assertTrue(result["exact_text_ids"])
        self.assertEqual([(item["section_title"], item["section_type"])
                          for item in result["full_label_map"]],
                         [("Misc", "introduction"), ("Misc", "limitations")])

    def test_ambiguous_repeated_heading_with_nonmatching_chunks_stays_unresolved(self):
        source = ("# Introduction\n## Misc\nOne.\n"
                  "# Limitations\n## Misc\nTwo.")
        result = source_heading_labels.candidate(
            [cached(0, "Misc", "One. plus cached material")], source)
        self.assertFalse(result["source_heading_verified"])
        self.assertEqual(result["status"], "unmatched_or_ambiguous_cached_heading")
        self.assertEqual(result["full_label_map"], [])

    def test_exact_chunk_identity_replaces_cached_title_from_source(self):
        result = source_heading_labels.candidate(
            [cached(0, "Invented Heading", "Some text")], "# 4. Methodology\nSome text")
        self.assertTrue(result["source_heading_verified"])
        self.assertTrue(result["exact_text_ids"])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["full_label_map"][0], {
            "chunk_index": 0, "section_title": "Methodology", "section_type": "method"})

    def test_exact_long_paper_heading_is_kept_in_full(self):
        long_title = "A source heading " + " ".join(["with a long exact title"] * 25)
        source = f"# {long_title}\nBody text."
        result = source_heading_labels.candidate(
            [cached(0, long_title, "Body text.")], source)

        self.assertTrue(result["source_heading_verified"])
        self.assertTrue(result["exact_text_ids"])
        self.assertIn(result["status"], {"candidate", "no_label_changes"})
        self.assertEqual(result["full_label_map"][0]["section_title"], long_title)


if __name__ == "__main__":
    unittest.main()
