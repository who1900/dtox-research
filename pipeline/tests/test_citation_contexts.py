"""Synthetic, offline tests of raw citation binding mechanics only."""

import hashlib
import json
import unittest
from unittest.mock import patch

from pipeline.citation_contexts import extract_citation_contexts


class CitationContextTests(unittest.TestCase):
    def extract(self, source=r"Evidence \cite{k}.", bib="", path="refs.bib"):
        return extract_citation_contexts(source, [(path, bib)] if bib else ())

    def codes(self, result):
        return [item["code"] for item in result["diagnostics"]]

    def targets(self, result):
        return [lead["target_id"] for context in result["contexts"] for lead in context["resolved"]]

    def test_exact_unicode_crlf_offsets_and_hashes(self):
        source = "\u041f\u0440\u0435\u0430\u043c\u0431\u0443\u043b\u0430\r\n\r\n  \u0422\u0435\u043a\u0441\u0442 \\citep[see {x[y]}][p.~2]{ k , z }.  \r\n\r\nTail"
        bib = '@article{k, doi={10.1234/AbC}}\n@misc{z,url={https://arxiv.org/abs/2401.12345v2}}'
        result = self.extract(source, bib)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
        serialized = json.dumps([("refs.bib", bib)], ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(result["bibliography_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        context = result["contexts"][0]
        self.assertEqual(context["raw"], source[context["start"]:context["end"]])
        self.assertEqual(context["start"], source.index("\u0422\u0435\u043a\u0441\u0442"))
        self.assertEqual(context["end"], source.index(".  \r\n") + 1)
        cite = context["citations"][0]
        self.assertEqual(cite["raw"], source[cite["start"]:cite["end"]])
        self.assertEqual(cite["start"], source.index("\\citep"))
        for key in cite["keys"]:
            self.assertEqual(source[key["start"]:key["end"]], key["key"])
        self.assertEqual(self.targets(result), ["doi:10.1234/abc", "arxiv:2401.12345"])
        versioned = context["resolved"][1]["provenance"][0]
        self.assertEqual(versioned["requested_id"], "arxiv:2401.12345v2")
        self.assertEqual(versioned["requested_version"], 2)
        for lead in context["resolved"]:
            self.assertEqual(lead["association"], "lead")
            self.assertEqual(lead["citation_start"], cite["start"])
            for provenance in lead["provenance"]:
                self.assertEqual(provenance["entry_raw"], bib[provenance["entry_start"]:provenance["entry_end"]])
                self.assertEqual(provenance["origin"], "bibliography_file")

    def test_balanced_bibtex_fields_quotes_parentheses_and_ignored_titles(self):
        bib = r'''@article(k,
            title="A comma, a } bracket and an escaped \"quote\"",
            note={Nested {braces, {deeper}} and \{escaped\}},
            author={arXiv:2401.99999},
            url={https://arxiv.org/pdf/hep-th/9901001v3.pdf})'''
        result = self.extract(bib=bib)
        self.assertEqual(self.targets(result), ["arxiv:hep-th/9901001"])
        self.assertEqual(result["diagnostics"], [])

    def test_bibtex_only_explicit_identifier_fields(self):
        result = self.extract(bib=r'@misc{k,title={arXiv:2401.12345},note={doi:10.1234/a}}')
        self.assertEqual(self.targets(result), [])
        self.assertIn("unresolved_reference", self.codes(result))

    def test_numeric_eprint_requires_explicit_arxiv_archive(self):
        for archive, expected in [("", []), (",archivePrefix={IACR}", []),
                                  (",archivePrefix={arXiv}", ["arxiv:2401.12345"])]:
            with self.subTest(archive=archive):
                self.assertEqual(self.targets(self.extract(bib='@misc{k,eprint={2401.12345}' + archive + '}')), expected)
        self.assertEqual(self.targets(self.extract(bib='@misc{k,eprint={math.GT/0301001}}')),
                         ["arxiv:math.gt/0301001"])
        self.assertEqual(self.targets(self.extract(bib='@misc{k,eprint={arXiv:2401.12345}}')),
                         ["arxiv:2401.12345"])

    def test_exact_prefixed_identifiers_and_iacr_links(self):
        for identifier, target in [("arXiv:2401.1234", "arxiv:2401.1234"),
                                   ("arxiv:cs.CR/0301001", "arxiv:cs.cr/0301001"),
                                   ("DOI:10.1234/ABC", "doi:10.1234/abc"),
                                   ("https://dx.doi.org/10.1234/a", "doi:10.1234/a"),
                                   ("https://eprint.iacr.org/2024/123.pdf", "iacr:2024/123")]:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.targets(self.extract(bib=r'\bibitem{k} ' + identifier, path="refs.bbl")), [target])

    def test_distinct_identifiers_in_one_entry_are_ambiguous(self):
        for fields in ["doi={10.1234/a},url={https://arxiv.org/abs/2401.12345}",
                       "url={https://arxiv.org/abs/2401.12345},eprint={2401.12346},archivePrefix={arXiv}",
                       "url={https://eprint.iacr.org/2024/123},doi={10.1234/a}"]:
            with self.subTest(fields=fields):
                result = self.extract(bib="@misc{k," + fields + "}")
                self.assertEqual(self.targets(result), [])
                self.assertIn("ambiguous_identifiers", self.codes(result))
        result = self.extract(bib=r'\bibitem{k} DOI:10.1234/a arXiv:2401.12345', path="refs.bbl")
        self.assertEqual(self.targets(result), [])
        self.assertIn("ambiguous_identifiers", self.codes(result))

    def test_same_identifier_spelling_repeated_is_not_ambiguous(self):
        bib = '@misc{k,doi={10.1234/a},url={https://doi.org/10.1234/A}}'
        result = self.extract(bib=bib)
        self.assertEqual(self.targets(result), ["doi:10.1234/a"])
        self.assertEqual(len(result["contexts"][0]["resolved"][0]["provenance"]), 2)
        self.assertNotIn("ambiguous_identifiers", self.codes(result))

    def test_versions_are_not_equated(self):
        result = self.extract(bib='@misc{k,url={https://arxiv.org/abs/2401.12345v1},'
                                  'eprint={2401.12345v2},archivePrefix={arXiv}}')
        self.assertEqual(self.targets(result), [])
        self.assertIn("ambiguous_identifiers", self.codes(result))
        result = self.extract(bib='@misc{k,eprint={2401.12345v1},archivePrefix={arXiv}}'
                                  '@misc{k,eprint={2401.12345v2},archivePrefix={arXiv}}')
        self.assertEqual(self.targets(result), [])
        self.assertIn("conflicting_duplicate_key", self.codes(result))

    def test_conflicting_duplicate_keys_across_source_and_supplied_files(self):
        source = r'Evidence \cite{k}.\begin{thebibliography}{9}\bibitem{k} doi:10.1234/a\end{thebibliography}'
        for other in ['@misc{k,doi={10.1234/b}}', '@misc{k,title={No ID}}']:
            with self.subTest(other=other):
                result = self.extract(source, other)
                self.assertEqual(self.targets(result), [])
                self.assertIn("conflicting_duplicate_key", self.codes(result))

    def test_identical_duplicate_identifiers_keep_all_provenance(self):
        result = extract_citation_contexts(r'\cite{k}', [("b.bbl", r'\bibitem{k} arXiv:2401.12345'),
                                                        ("a.bib", '@misc{k,url={https://arxiv.org/abs/2401.12345}}')])
        self.assertEqual(self.targets(result), ["arxiv:2401.12345"])
        self.assertEqual([p["path"] for p in result["contexts"][0]["resolved"][0]["provenance"]], ["a.bib", "b.bbl"])

    def test_all_citation_commands_optional_arguments_and_nocite(self):
        source = r'\nocite{k,*} ' + ' '.join('\\' + command + r'*[see {x[y]}][p.~3]{k}'
                                            for command in ["cite", "citep", "citet", "parencite", "textcite"])
        result = self.extract(source, '@misc{k,doi={10.1234/a}}')
        self.assertEqual([c["command"] for c in result["contexts"][0]["citations"]],
                         ["cite", "citep", "citet", "parencite", "textcite"])
        self.assertEqual(len(self.targets(result)), 5)
        self.assertEqual(self.extract(r'\nocite{k,*}')["contexts"], [])

    def test_bibitem_optional_labels_and_balanced_keys(self):
        result = self.extract(r'\cite{key{nested}}', r'\bibitem[Author {x[y]}]{key{nested}} doi:10.1234/a', "refs.bbl")
        self.assertEqual(self.targets(result), ["doi:10.1234/a"])
        self.assertEqual(result["contexts"][0]["resolved"][0]["key"], "key{nested}")

    def test_multiple_keys_are_separate_leads_not_assertions(self):
        result = self.extract(r'A comparison \cite{k, unknown, z}.', '@misc{k,doi={10.1234/a}}\n@misc{z,doi={10.1234/b}}')
        context = result["contexts"][0]
        self.assertTrue(context["may_discuss_multiple_works"])
        self.assertEqual([lead["key"] for lead in context["resolved"]], ["k", "z"])
        self.assertTrue(all(lead["association"] == "lead" for lead in context["resolved"]))
        self.assertIn("unknown_reference", self.codes(result))
        self.assertFalse(self.extract(r'\cite{k}\cite{k}')["contexts"][0]["may_discuss_multiple_works"])

    def test_no_unknown_key_fallback_to_all_supplied_references(self):
        result = self.extract(r'\cite{missing}', '@misc{k,doi={10.1234/a}}\n@misc{z,doi={10.1234/b}}')
        self.assertEqual(self.targets(result), [])
        self.assertEqual(self.codes(result), ["unknown_reference"])

    def test_comments_escaped_percent_and_literal_exclusion(self):
        for env in ["verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "code", "comment"]:
            source = '% \\cite{hidden}\n' + '\\begin{' + env + r'}\cite{hidden}' + '\\end{' + env + '}\n'
            source += r'\verb|\cite{hidden}| \verb*+\cite{hidden}+ \lstinline[language={TeX}]!\cite{hidden}! ' + '\n'
            source += r'Visible \% still \cite{k}. % \cite{hidden}'
            with self.subTest(env=env):
                result = self.extract(source, '@misc{k,doi={10.1234/a}}')
                self.assertEqual(self.targets(result), ["doi:10.1234/a"])
                self.assertEqual(result["diagnostics"], [])

    def test_escaped_backslashes_do_not_create_commands_or_bibitems(self):
        self.assertEqual(self.extract(r'\\cite{k}')["contexts"], [])
        result = self.extract(bib=r'\\bibitem{k} doi:10.1234/a', path="refs.bbl")
        self.assertEqual(self.targets(result), [])

    def test_public_search_text_masks_comments_and_literals_without_changing_raw(self):
        bib = '@misc{k,doi={10.1234/a}}'
        for env in ["verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "code", "comment"]:
            paragraph = 'Visible \\citep[see][p.~2]{k}. % COMMENT_SENTINEL \\cite{hidden}\n'
            paragraph += r'Continued \verb|INLINE_SENTINEL \cite{hidden}| '
            paragraph += '\\begin{' + env + r'}ENV_SENTINEL \cite{hidden}' + '\\end{' + env + '} tail.'
            source = 'Introduction\n\n' + paragraph
            with self.subTest(env=env):
                result = self.extract(source, bib)
                context, = result["contexts"]
                self.assertEqual(context["raw"], paragraph)
                self.assertEqual(context["raw"], source[context["start"]:context["end"]])
                self.assertEqual(context["start"], len('Introduction\n\n'))
                self.assertEqual(context["end"], len(source))
                self.assertEqual(len(context["search_text"]), len(context["raw"]))
                for token in ["COMMENT_SENTINEL", "INLINE_SENTINEL", "ENV_SENTINEL"]:
                    self.assertIn(token, context["raw"])
                    self.assertNotIn(token, context["search_text"])
                self.assertIn(r'Visible \citep[see][p.~2]{k}.', context["search_text"])
                self.assertIn('Continued', context["search_text"])
                self.assertTrue(context["search_text"].endswith(' tail.'))
                cite, = context["citations"]
                self.assertEqual(cite["raw"], source[cite["start"]:cite["end"]])
                self.assertEqual([key["key"] for key in cite["keys"]], ["k"])
                self.assertEqual(self.targets(result), ["doi:10.1234/a"])
                self.assertEqual(result["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
                serialized = json.dumps([("refs.bib", bib)], ensure_ascii=False, separators=(",", ":"))
                self.assertEqual(result["bibliography_sha256"], hashlib.sha256(serialized.encode()).hexdigest())

    def test_citation_only_search_text_remains_a_lead(self):
        result = self.extract(r'\cite{k}', '@misc{k,doi={10.1234/a}}')
        context, = result["contexts"]
        self.assertEqual(context["search_text"], context["raw"])
        self.assertEqual(context["search_text"], r'\cite{k}')
        self.assertEqual(context["resolved"][0]["association"], "lead")

    def test_bibliography_body_never_becomes_query_context(self):
        source = r'Body \cite{k}.' + '\n' + r'\begin{thebibliography}{9}\bibitem{k} DOI:10.1234/a \cite{hidden}\end{thebibliography}'
        result = self.extract(source)
        self.assertEqual(len(result["contexts"]), 1)
        self.assertEqual(result["contexts"][0]["raw"], r'Body \cite{k}.')
        self.assertEqual(self.targets(result), ["doi:10.1234/a"])
        self.assertEqual(result["contexts"][0]["resolved"][0]["provenance"][0]["path"], None)
        self.assertNotIn("unknown_reference", self.codes(result))
        self.assertEqual(self.extract(r'\bibitem{k} doi:10.1234/a \cite{hidden}')["contexts"], [])

    def test_comment_and_literal_bibitems_do_not_register_references(self):
        result = self.extract(bib='% \\bibitem{k} doi:10.1234/a\n' + r'\begin{verbatim}\bibitem{k} doi:10.1234/a\end{verbatim}', path="refs.bbl")
        self.assertEqual(self.targets(result), [])
        self.assertIn("unknown_reference", self.codes(result))

    def test_macro_identifiers_and_bibtex_directives_are_not_evaluated(self):
        bib = '@string{link="https://arxiv.org/abs/2401.12345"}\n@comment{k,doi={10.1234/a}}\n@preamble{"x"}\n@misc{k,url=link}'
        result = self.extract(bib=bib)
        self.assertEqual(self.targets(result), [])
        self.assertIn("malformed_bibtex", self.codes(result))

    def test_invalid_identifier_prefixes_and_domains_are_not_partial_matches(self):
        for text in ['arXiv:2401.123456', 'arXiv:2413.12345', 'notarXiv:2401.12345',
                     'https://arxiv.org.evil/abs/2401.12345', 'https://arxiv.org/abs/2401.12345extra',
                     'https://eprint.iacr.org/2024/123extra', 'https://doi.org.evil/10.1234/a']:
            with self.subTest(text=text):
                self.assertEqual(self.targets(self.extract(bib=r'\bibitem{k} ' + text, path="refs.bbl")), [])

    def test_malformed_inputs_fail_closed_with_diagnostics(self):
        for source in [r'\cite[unclosed{k}', r'\cite{k', r'\cite', r'\cite{}', r'\cite{k,}']:
            with self.subTest(source=source):
                result = self.extract(source, '@misc{k,doi={10.1234/a}}')
                self.assertEqual(result["contexts"], [])
                self.assertIn("malformed_citation", self.codes(result))
        for bib, path in [(r'\bibitem[unclosed{k} doi:10.1234/a', "refs.bbl"),
                          ('@misc{k,doi={10.1234/a}', "refs.bib"),
                          ('@misc{k,doi={10.1234/a},doi={10.1234/b}}', "refs.bib"),
                          ('@misc{k,doi={10.1234/a}, broken}', "refs.bib")]:
            with self.subTest(bib=bib):
                result = self.extract(bib=bib, path=path)
                self.assertEqual(self.targets(result), [])
                self.assertTrue(result["diagnostics"])

    def test_unbalanced_literal_and_bibliography_are_diagnosed_and_excluded(self):
        for source, code in [(r'\begin{verbatim}\cite{k}', "unbalanced_literal"),
                             (r'\verb|\cite{k}', "unbalanced_literal"),
                             (r'\begin{thebibliography}{9}\cite{k}', "unbalanced_bibliography")]:
            with self.subTest(source=source):
                result = self.extract(source)
                self.assertEqual(result["contexts"], [])
                self.assertIn(code, self.codes(result))

    def test_explicit_par_and_blank_lines_split_contexts(self):
        source = 'One \\cite{k}.\n\nTwo \\cite{k}.\\par Three \\cite{k}.'
        result = self.extract(source)
        self.assertEqual([c["raw"] for c in result["contexts"]],
                         [r'One \cite{k}.', r'Two \cite{k}.', r'Three \cite{k}.'])

    def test_deterministic_hashes_order_and_full_serializable_result(self):
        files = [("\u0441.bib", '@misc{k,doi={10.1234/a}}'), ("a.bbl", r'\bibitem{z} arXiv:2401.12345')]
        source = r'\cite{k,z}'
        first = extract_citation_contexts(source, files)
        self.assertEqual(first, extract_citation_contexts(source, list(reversed(files))))
        self.assertEqual(first, json.loads(json.dumps(first)))
        changed = extract_citation_contexts(source, files + [files[0]])
        self.assertNotEqual(first["bibliography_sha256"], changed["bibliography_sha256"])
        self.assertEqual(self.extract("")["source_sha256"], hashlib.sha256(b"").hexdigest())

    def test_no_disk_reads_or_implicit_bibliography_consumption(self):
        with patch("builtins.open", side_effect=AssertionError("disk read")):
            result = extract_citation_contexts(r'\input{refs.bbl}\bibliography{refs}\cite{k}')
            supplied = extract_citation_contexts(r'\cite{k}', [("../../nonexistent.bib", '@misc{k,doi={10.1234/a}}')])
        self.assertEqual(self.targets(result), [])
        self.assertEqual(self.targets(supplied), ["doi:10.1234/a"])

    def test_source_byte_bound_is_not_a_character_bound_or_clipping(self):
        self.assertEqual(self.extract("x" * (8 * 1024 * 1024))["contexts"], [])
        for source in ["x" * (8 * 1024 * 1024 + 1), "\u044f" * (4 * 1024 * 1024 + 1)]:
            with self.subTest(length=len(source)), self.assertRaises(ValueError):
                self.extract(source)

    def test_input_types_and_finite_bibliography_container(self):
        for source in [None, b"text", 1, ["text"]]:
            with self.subTest(source=source), self.assertRaises(TypeError):
                extract_citation_contexts(source)
        for files in [None, "refs.bib", {"refs.bib": "text"}, iter([]),
                      [["refs.bib", "text"]], [("refs.bib",)], [(b"refs.bib", "text")],
                      [("refs.bib", None)], [("refs.bib", 12)]]:
            with self.subTest(files=files), self.assertRaises(TypeError):
                extract_citation_contexts("", files)
        self.assertEqual(extract_citation_contexts("", (("refs.bib", ""),))["contexts"], [])

    def test_bibliography_count_and_aggregate_utf8_bound(self):
        self.assertEqual(extract_citation_contexts("", [("a.bib", "")] * 1024)["contexts"], [])
        with self.assertRaises(ValueError):
            extract_citation_contexts("", [("a.bib", "")] * 1025)
        for source, files in [("", [("a.bib", "x" * (8 * 1024 * 1024))]),
                              ("x" * (4 * 1024 * 1024), [("a.bib", "\u044f" * (2 * 1024 * 1024))]),
                              ("", [("a.bib", "x" * (4 * 1024 * 1024)), ("b.bbl", "x" * (4 * 1024 * 1024))]),
                              ("", [("a" * (8 * 1024 * 1024 + 1), "")])]:
            with self.subTest(source_length=len(source)), self.assertRaises(ValueError):
                extract_citation_contexts(source, files)

    def test_repeated_large_entry_provenance_is_bounded_not_truncated(self):
        bib = r'\bibitem{k} DOI:10.1234/a ' + ("padding " * 25000)
        self.assertEqual(len(self.targets(self.extract(r'\cite{k}' * 40, bib, "refs.bbl"))), 40)
        with self.assertRaises(ValueError):
            self.extract(r'\cite{k}' * 50, bib, "refs.bbl")

    def test_output_counts_bound_many_commands_keys_and_entries(self):
        with self.assertRaises(ValueError):
            self.extract(r'\cite{k}' * 10001)
        with self.assertRaises(ValueError):
            self.extract('\\cite{' + ','.join(['k'] * 10001) + '}')
        with self.assertRaises(ValueError):
            self.extract(bib='@misc{k,title={nothing}}' * 10001)

    def test_serialized_output_budget_includes_repeated_provenance_paths(self):
        files = [("p" * (1024 * 1024) + ".bib", '@misc{k,doi={10.1234/a}}')]
        self.assertEqual(self.targets(extract_citation_contexts(r'\cite{k}', files)), ["doi:10.1234/a"])
        with self.assertRaises(ValueError):
            extract_citation_contexts(r'\cite{k}' * 9, files)

    def test_output_budget_counts_json_escaping_and_raw_context_repetition(self):
        with self.assertRaises(ValueError):
            self.extract(('"' * (5 * 1024 * 1024)) + r'\cite{k}')

    def test_serialized_output_budget_includes_new_search_text(self):
        with self.assertRaises(ValueError):
            self.extract(('x' * (4 * 1024 * 1024)) + r'\cite{k}')

    def test_context_bound_counts_even_unresolved_contexts(self):
        source = '\\cite{k}\n\n' * 10000
        self.assertEqual(len(self.extract(source)["contexts"]), 10000)
        with self.assertRaises(ValueError):
            self.extract(source + r'\cite{k}')


if __name__ == "__main__":
    unittest.main()
