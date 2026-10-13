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

    def test_duplicate_cached_heading_with_different_source_labels_is_ambiguous(self):
        source = ("# Introduction\n## Misc\nOne.\n"
                  "# Limitations\n## Misc\nTwo.")
        result = source_heading_labels.candidate([cached(0, "Misc", "One.")], source)
        self.assertFalse(result["source_heading_verified"])
        self.assertEqual(result["status"], "unmatched_or_ambiguous_cached_heading")
        self.assertEqual(result["full_label_map"], [])

    def test_cached_heading_must_match_a_source_heading(self):
        result = source_heading_labels.candidate(
            [cached(0, "Invented Heading", "Some text")], "# 4. Methodology\nSome text")
        self.assertFalse(result["source_heading_verified"])
        self.assertEqual(result["status"], "unmatched_or_ambiguous_cached_heading")


if __name__ == "__main__":
    unittest.main()
