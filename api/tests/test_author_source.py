import unittest
import pathlib
import sys

from api import main

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import build_twins


class AuthorSourceTests(unittest.TestCase):
    def test_author_copy_uses_only_its_stored_pdf_url(self):
        paper_id = "author:2024/1234:0123456789ab"
        url = "https://university.example.edu/papers/author-copy.pdf"
        self.assertEqual(main.source_of(paper_id), "author")
        self.assertEqual(main.SOURCE_LABELS[main.source_of(paper_id)],
                         "Author / institutional copy")
        self.assertEqual(main.source_url(paper_id, {"source_url": url}), url)
        self.assertEqual(main.source_url(paper_id, {"pdf_url": url}), url)

    def test_author_copy_without_stored_url_has_no_synthetic_link(self):
        paper_id = "author:2024/1234:0123456789ab"
        self.assertIsNone(main.source_url(paper_id))
        self.assertIsNone(main.source_url(paper_id, {"title": "A paper title"}))
        self.assertIsNone(main.source_url(paper_id, {"source_url": "not a URL"}))

    def test_author_copy_license_is_unknown_and_makes_no_cc_claim(self):
        license_info = main.license_of("author:2024/1234:0123456789ab")
        self.assertIsNone(license_info["spdx"])
        self.assertIn("unknown", license_info["terms"])
        self.assertNotIn("CC", license_info["terms"])

    def test_unknown_source_fallback_is_unchanged(self):
        self.assertEqual(main.source_of("unrecognized:paper"), "arxiv")

    def test_existing_twins_builder_groups_exact_title_iacr_copy(self):
        title = "A sufficiently long exact title for this research paper"
        grouped = build_twins.build([
            ("iacr:2024/1234", title, 2024),
            ("author:2024/1234:0123456789ab", title, 2024),
        ])
        self.assertEqual(grouped["author:2024/1234:0123456789ab"], "iacr:2024/1234")


if __name__ == "__main__":
    unittest.main()
