import importlib.util
import unittest
from unittest.mock import patch

from pipeline import extractor


class LatexCommentRegressionTests(unittest.TestCase):
    def test_literal_modulo_in_all_code_environments(self):
        for environment in ("verbatim", "Verbatim", "lstlisting", "minted"):
            with self.subTest(environment=environment):
                block = (f"\\begin{{{environment}}}\n"
                         "int rem = value % 2;\n% literal line\n"
                         f"\\end{{{environment}}}")
                source = "Before % remove\n" + block + "\nAfter % remove"
                self.assertEqual(extractor.strip_comments(source),
                                 "Before \n" + block + "\nAfter ")

    def test_inline_verbatim_percent_and_trailing_comment(self):
        for inline in (r"\verb|value % 2|", r"\verb*+50%+",
                       r"\Verb!value % 2!", r"\lstinline[language=C]|x % 2|"):
            with self.subTest(inline=inline):
                self.assertEqual(extractor.strip_comments(inline + " % remove\nNext"),
                                 inline + " \nNext")

    def test_escaped_percent_comments_and_math_are_not_rewritten(self):
        source = "value \\% kept % removed\n$rate = 50\\%$\n" + r"line \\% removed"
        expected = "value \\% kept \n$rate = 50\\%$\n" + "line \\\\"
        self.assertEqual(extractor.strip_comments(source), expected)

    def test_commented_literal_openers_do_not_protect_following_comments(self):
        source = "% \\begin{verbatim}\nVisible % removed\n% \\verb|literal|"
        self.assertEqual(extractor.strip_comments(source), "\nVisible \n")

    def test_ordinary_macro_prefix_is_not_mistaken_for_inline_verbatim(self):
        self.assertEqual(extractor.strip_comments(r"\verbose{word % remove word}"),
                         r"\verbose{word ")

    def test_code_with_section_commands_stays_in_its_real_section(self):
        block = "\\begin{lstlisting}\n\\section{Fake} int x = n % 2;\n\\end{lstlisting}"
        source = "\\section{Method}\nBefore\n" + block + "\nAfter"
        self.assertEqual(extractor.split_sections_raw(extractor.strip_comments(source)),
                         [("Method", "Before\n" + block + "\nAfter")])

    def test_code_is_emitted_raw_before_prose_conversion(self):
        block = "\\begin{minted}{c}\nint rem = value % 2;\n\\end{minted}"
        with patch.object(extractor, "_extract_elements_latex",
                          side_effect=lambda text: [("prose", text.strip())]):
            elements = extractor.extract_elements("Before\n" + block + "\nAfter")
        self.assertEqual(elements, [("prose", "Before"), ("code", block), ("prose", "After")])

    def test_algorithm_with_inline_verbatim_stays_one_ordered_container(self):
        algorithm = ("\\begin{algorithm}\nBefore step\n"
                     "\\verb|x % 2|\nAfter step\n\\end{algorithm}")
        with patch.object(extractor, "_extract_elements_latex",
                          side_effect=lambda text: [("prose", text.strip())]):
            elements = extractor.extract_elements("Before\n" + algorithm + "\nAfter")
        self.assertEqual(elements, [("prose", "Before"), ("algorithm", algorithm), ("prose", "After")])

    def test_table_with_nested_code_stays_one_ordered_container(self):
        code = "\\begin{lstlisting}\nx % 2\n\\end{lstlisting}"
        table = "\\begin{table}\n\\begin{tabular}{l}\n" + code + "\n\\end{tabular}\n\\end{table}"
        with patch.object(extractor, "_extract_elements_latex",
                          side_effect=lambda text: [("prose", text.strip())]):
            elements = extractor.extract_elements("Before\n" + table + "\nAfter")
        self.assertEqual(elements, [("prose", "Before"), ("table", extractor.table_essence(table)),
                                    ("prose", "After")])
        self.assertIn(code, elements[1][1])

    def test_equation_with_inline_literal_is_not_split_or_reescaped(self):
        equation = "\\begin{equation}x = \\verb|x % 2| + 50\\%\\end{equation}"
        with patch.object(extractor, "_extract_elements_latex",
                          side_effect=lambda text: [("prose", text.strip())]):
            elements = extractor.extract_elements(equation)
        self.assertEqual(elements, [("equation", equation)])

    def test_nested_algorithm_and_fake_end_in_code_keep_outer_container(self):
        code = "\\begin{lstlisting}\nx % 2; \\end{algorithm}\n\\end{lstlisting}"
        algorithm = ("\\begin{algorithm}\\begin{algorithmic}\n" + code
                     + "\n\\end{algorithmic}\\end{algorithm}")
        self.assertEqual(extractor.extract_elements(algorithm), [("algorithm", algorithm)])

    def test_chunk_metadata_and_order_for_protected_containers(self):
        algorithm = "\\begin{algorithm}\\verb|x % 2|\\end{algorithm}"
        table = "\\begin{tabular}{l}\\verb|x % 2|\\end{tabular}"
        source = "\\section{Method}\nBefore\n" + algorithm + "\nBetween\n" + table + "\nAfter"
        with patch.object(extractor, "_extract_elements_latex",
                          side_effect=lambda text: [("prose", text.strip())]):
            chunks, mode = extractor.build_chunks(source)
        self.assertEqual(mode, "pylatexenc")
        self.assertEqual([c["text"] for c in chunks], ["Before", algorithm, "Between", table, "After"])
        self.assertEqual([c["element_type"] for c in chunks], ["prose", "algorithm", "prose", "table", "prose"])
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(5)))
        self.assertTrue(all(c["part_index"] == 0 and c["part_total"] == 1 for c in chunks))

    @unittest.skipUnless(importlib.util.find_spec("pylatexenc"), "pylatexenc is not installed")
    def test_real_parser_preserves_code_and_inline_verbatim(self):
        for block in ("\\begin{verbatim}\nint rem = value % 2;\n\\end{verbatim}",
                      "\\begin{Verbatim}\nint rem = value % 2;\n\\end{Verbatim}",
                      "\\begin{lstlisting}\nint rem = value % 2;\n\\end{lstlisting}",
                      "\\begin{minted}{c}\nint rem = value % 2;\n\\end{minted}",
                      r"\verb|int rem = value % 2;|"):
            with self.subTest(block=block), patch.object(extractor, "_parse_deadline"):
                chunks, mode = extractor.build_chunks("\\section{Method}\nBefore\n" + block + "\nAfter")
                self.assertEqual(mode, "pylatexenc")
                self.assertIn(block, [c["text"] for c in chunks if c["element_type"] == "code"])
                self.assertEqual([c["element_type"] for c in chunks], ["prose", "code", "prose"])


class MarkdownRegressionTests(unittest.TestCase):
    def test_crlf_heading_titles_do_not_keep_closing_hashes(self):
        chunks, _ = extractor.build_chunks_markdown("## Security Considerations ##\r\n"
                                                    "### Storage ###\r\nRisk details")
        self.assertEqual(chunks[0]["section_title"], "Storage")
        self.assertEqual(chunks[0]["section_type"], "limitations")

    def test_security_context_wins_over_child_implementation_keywords(self):
        chunks, _ = extractor.build_chunks_markdown("## Security Considerations\n"
                                                    "### Delegate implementation\nRisk details")
        self.assertEqual(chunks[0]["section_type"], "limitations")

    def test_nested_security_headers_inherit_taxonomy_and_reset_at_siblings(self):
        source = ("## Security Considerations\nParent\n"
                  "### Delegation\nChild\n#### Storage\nGrandchild\n"
                  "##### Calls\nDeep child\n###### Nonces\nDeepest child\n"
                  "### Replay\nSibling\n## Specification\nMethod\n"
                  "### Authorization\nMethod child\n## Unclassified\nOther")
        chunks, mode = extractor.build_chunks_markdown(source)
        self.assertEqual(mode, "markdown")
        self.assertEqual([c["section_type"] for c in chunks],
                         ["limitations"] * 6 + ["method", "method", "other"])
        self.assertEqual([c["section_title"] for c in chunks],
                         ["Security Considerations", "Delegation", "Storage", "Calls",
                          "Nonces", "Replay", "Specification", "Authorization", "Unclassified"])

    def test_alternating_fenced_blocks_tables_and_prose_keep_source_order(self):
        table = "| Input | Result |\n| --- | --- |\n| 5 | 1 |"
        source = ("## Specification\nBefore\n```python\na = 5 % 2\n```\n"
                  + table + "\n```text\n# Not a heading\n```\nAfter")
        chunks, _ = extractor.build_chunks_markdown(source)
        self.assertEqual([c["text"] for c in chunks],
                         ["Before", "```python\na = 5 % 2\n```", table,
                          "```text\n# Not a heading\n```", "After"])
        self.assertEqual([c["element_type"] for c in chunks],
                         ["prose", "code", "prose", "code", "prose"])
        self.assertTrue(all(c["section_title"] == "Specification" for c in chunks))
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(5)))

    def test_tilde_and_long_fences_do_not_split_at_embedded_headings(self):
        blocks = ["~~~~rust\n## Not security\nx % 2\n~~~~",
                  "````text\n```\n## Not a heading\n````"]
        chunks, _ = extractor.build_chunks_markdown("## Specification\nBefore\n"
                                                    + "\nBetween\n".join(blocks) + "\nAfter")
        self.assertEqual([c["text"] for c in chunks],
                         ["Before", blocks[0], "Between", blocks[1], "After"])

    def test_limits_and_part_metadata_for_long_prose_and_code(self):
        prose = "before " * 600
        code = "```text\n" + "x % 2;\n" * 600 + "```"
        chunks, _ = extractor.build_chunks_markdown("## Security Considerations\n"
                                                    + prose + "\n" + code + "\nAfter")
        self.assertTrue(all(len(c["text"]) <= extractor.CHUNK_CHAR_LIMIT for c in chunks))
        self.assertEqual([c["chunk_index"] for c in chunks], list(range(len(chunks))))
        for element in ("prose", "code"):
            parts = [c for c in chunks if c["element_type"] == element and c["text"] != "After"]
            self.assertGreater(len(parts), 1)
            self.assertEqual([c["part_index"] for c in parts], list(range(len(parts))))
            self.assertTrue(all(c["part_total"] == len(parts) for c in parts))
        self.assertEqual(chunks[-1]["text"], "After")


if __name__ == "__main__":
    unittest.main()
