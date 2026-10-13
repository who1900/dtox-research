import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ops import plan_missing_prose_repair as repair


def prose(label, count=30):
    return " ".join(f"{label}word{number}" for number in range(count)) + "."


def chunk(title, text, index=0, **fields):
    return {"chunk_index": index, "section_title": title, "element_type": "prose",
            "text": text, **fields}


def source_for(*sections):
    return "\n".join("\\section{" + title + "}\n" + text
                     for title, text in sections) + "\n\\end{document}"


class MissingProseRepairPlanTests(unittest.TestCase):
    def setUp(self):
        self.structured = [
            chunk("Method", r"\begin{equation}x=1\end{equation}", 75, element_type="equation"),
            chunk("Algorithm", "stable algorithm", 76, element_type="code"),
        ]
        self.calls = []

    def plan(self, source=None, old=None, bounded=None, full=None,
             full_mode="pylatexenc_recovered", bounded_mode="pylatexenc", **kwargs):
        if source is None:
            source = source_for(("Discussions", prose("limitation")))
        if old is None:
            old = copy.deepcopy(self.structured)
        sections, _errors = repair._source_sections(source)
        mapping = {item["bounded_source"]: [chunk(item["title"], item["body"])]
                   for item in sections}
        if bounded is not None:
            mapping.update(bounded)
        if full is None:
            full = [copy.deepcopy(item) for item in old if repair._is_preserved_structured(item)]
        self.calls = []

        def parse(text, section_sink=None):
            self.calls.append(text)
            if text == source:
                return copy.deepcopy(full), full_mode
            parsed = copy.deepcopy(mapping[text])
            if section_sink is not None:
                section_sink.extend({"section_title": item.get("section_title", ""),
                                     "section_type": item.get("section_type", "other"), "text": item["text"]}
                                    for item in parsed)
            return parsed, bounded_mode

        with patch.object(repair.extractor, "build_chunks", side_effect=parse):
            return repair.build_plan("fixture", source, old, **kwargs)

    def assert_blocked(self, result, message):
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["additions"], [])
        self.assertTrue(any(message in reason for reason in result["blockers"]), result["blockers"])

    def test_missing_entire_section_does_not_require_cached_target(self):
        result = self.plan()
        self.assertEqual(result["status"], "dry_run_only")
        self.assertFalse(result["apply_supported"])
        self.assertEqual(result["additions"][0]["chunk_index"], 77)
        self.assertEqual(result["additions"][0]["text"], prose("limitation"))
        self.assertEqual(result["section_results"][0]["state"], "missing")
        self.assertEqual(result["old_structured_hashes"], result["new_structured_hashes"])

    def test_default_none_scans_multiple_arbitrary_sections(self):
        source = source_for(("Data cleaning and categorization", prose("cleaning")),
                            ("Impact of Ponzi schemes", prose("impact")))
        result = self.plan(source)
        self.assertIsNone(result["section"])
        self.assertEqual([item["section_title"] for item in result["additions"]],
                         ["Data cleaning and categorization", "Impact of Ponzi schemes"])
        self.assertEqual([item["chunk_index"] for item in result["additions"]], [77, 78])
        self.assertEqual(len(self.calls), 3)

    def test_explicit_discussions_preserves_backward_compatible_shape(self):
        source = source_for(("Discussions", prose("limitation")), ("Other", prose("other")))
        result = self.plan(source, section="Discussions")
        self.assertEqual(result["section"], "Discussions")
        self.assertEqual(len(result["additions"]), 1)
        self.assertEqual(result["target_parser_mode"], "pylatexenc")
        self.assertEqual(result["source_boundary"]["body_char_count"], len(prose("limitation")))

    def test_all_keyword_and_none_are_equivalent(self):
        self.assertEqual(self.plan(section="all"), self.plan(section=None))

    def test_healthy_recovered_content_is_no_change(self):
        text = prose("healthy")
        result = self.plan(source_for(("Methods", text)), [chunk("Methods", text)])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["additions"], [])
        self.assertEqual(result["parser_mode"], "pylatexenc_recovered")

    def test_macro_case_whitespace_unicode_representation_is_healthy(self):
        retained = prose("healthy") + " Fiancée inference parameters"
        recovered = prose("healthy").upper() + r" \unknown{Fian" + "ce\u0301e} " + r"\textbf{inference} parameters"
        result = self.plan(source_for(("Methods", recovered)), [chunk("Methods", retained)])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["additions"], [])

    def test_small_macro_growth_is_not_loss_proof(self):
        retained = prose("before") + " " + prose("after")
        recovered = prose("before") + r" \unknown{macro display of small new parameters} " + prose("after")
        result = self.plan(source_for(("Methods", recovered)), [chunk("Methods", retained)])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["section_results"][0]["ignored_representation_spans"], 1)

    def test_partial_middle_appends_only_missing_span(self):
        before, missing, after = prose("before"), prose("missing"), prose("after")
        result = self.plan(source_for(("Methods", before + " " + missing + " " + after)),
                           [chunk("Methods", before + " " + after)])
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual(result["additions"][0]["text"], missing)
        self.assertEqual(result["section_results"][0]["state"], "partial")

    def test_substantive_preserved_macro_argument_is_not_partial_prose_loss(self):
        retained = prose("before") + " " + prose("after")
        recovered = prose("before") + r" \unknown{" + prose("macro") + "} " + prose("after")
        result = self.plan(source_for(("Methods", recovered)), [chunk("Methods", retained)])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["additions"], [])

    def test_partial_prefix_and_suffix_do_not_reappend_retained_paragraphs(self):
        retained, missing = prose("retained"), prose("missing")
        for at_start in (True, False):
            with self.subTest(at_start=at_start):
                text = missing + " " + retained if at_start else retained + " " + missing
                result = self.plan(source_for(("Methods", text)), [chunk("Methods", retained)])
                self.assertEqual(result["status"], "dry_run_only")
                self.assertEqual([item["text"] for item in result["additions"]], [missing])

    def test_substantive_replacement_fails_closed(self):
        result = self.plan(source_for(("Methods", prose("new"))), [chunk("Methods", prose("old"))])
        self.assert_blocked(result, "substitution or deletion")

    def test_missing_span_requires_retained_word_anchors(self):
        retained = "tiny retained anchor"
        result = self.plan(source_for(("Methods", retained + " " + prose("missing"))),
                           [chunk("Methods", retained)])
        self.assert_blocked(result, "retained-word anchors")

    def test_small_spans_are_not_automatically_repaired(self):
        for text in (" ".join(["longspelling"] * 19), " ".join(["a"] * 30)):
            with self.subTest(text=text):
                result = self.plan(source_for(("Methods", text)), [])
                self.assertEqual(result["status"], "no_change")
                self.assertEqual(result["additions"], [])

    def test_comments_literals_and_escaped_headers_do_not_create_boundaries(self):
        source = (
            "% \\section{Discussions}\n"
            r"\verb|\section{Discussions} \appendix|" + "\n"
            r"\begin{verbatim}\section{Discussions}\appendix\end{verbatim}" + "\n"
            r"\\section{Discussions}" + "\n"
            r"\section{Discussions}" + "\n" + prose("real") + "\n"
            r"\verb|\bibliography{fake}|" + "\n"
            r"\section{Next}" + "\n" + prose("next") + "\n" + r"\end{document}"
        )
        bounded, error, boundary = repair._bounded_discussions_source(source, "Discussions")
        self.assertIsNone(error)
        self.assertIn(prose("real"), bounded)
        self.assertIn(r"\verb|\bibliography{fake}|", bounded)
        self.assertEqual(boundary["end_token"], r"\section")

    def test_reference_and_unheaded_appendix_do_not_bleed_into_prose(self):
        for end in (r"\bibliographystyle{plain}", r"\bibliography{refs}", r"\appendix",
                    r"\begin{thebibliography}{9}"):
            with self.subTest(end=end):
                source = r"\section{Discussions}" + "\n" + prose("real") + "\n" + end + "\nMedusa material"
                result = self.plan(source, section="Discussions")
                self.assertEqual([item["text"] for item in result["additions"]], [prose("real")])
                self.assertNotIn("Medusa", self.calls[1])

    def test_explicit_appendix_heading_has_appendix_type_not_parent_label(self):
        source = source_for(("Discussions", prose("real"))).replace(r"\end{document}", "") + (
            r"\appendix" + "\n" + r"\section{Medusa experiments}" + "\n" +
            prose("medusa") + "\n" + r"\end{document}")
        result = self.plan(source)
        self.assertEqual([item["section_title"] for item in result["additions"]],
                         ["Discussions", "Medusa experiments"])
        self.assertEqual(result["additions"][1]["section_type"], "appendix")
        self.assertNotIn("medusa", result["additions"][0]["text"])

    def test_title_appendix_is_repaired_with_its_own_label(self):
        result = self.plan(source_for(("Appendix", prose("appendix"))))
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual(result["additions"][0]["section_type"], "appendix")

    def test_reference_heading_is_excluded(self):
        result = self.plan(source_for(("Methods", prose("method")), ("References", prose("references"))))
        self.assertEqual([item["section_title"] for item in result["additions"]], ["Methods"])

    def test_nested_headings_are_exclusive_and_keep_source_titles(self):
        source = (r"\section{Methods}" + "\n" + prose("method") + "\n" +
                  r"\subsection{Address clustering}" + "\n" + prose("clustering") + "\n" +
                  r"\subsubsection{Mechanized Proofs}" + "\n" + prose("proofs") + "\n" +
                  r"\section{Results}" + "\n" + prose("result") + "\n" + r"\end{document}")
        result = self.plan(source)
        self.assertEqual([item["section_title"] for item in result["additions"]],
                         ["Methods", "Address clustering", "Mechanized Proofs", "Results"])
        self.assertEqual([item["text"] for item in result["additions"]],
                         [prose("method"), prose("clustering"), prose("proofs"), prose("result")])

    def test_duplicate_titles_are_unresolved_without_canceling_unique_section(self):
        source = source_for(("Repeat", prose("one")), ("Repeat", prose("two")), ("Unique", prose("unique")))
        result = self.plan(source)
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual([item["section_title"] for item in result["additions"]], ["Unique"])
        self.assertTrue(result["partial_scope"])
        self.assertEqual([item["section_title"] for item in result["unresolved_sections"]],
                         ["Repeat", "Repeat"])
        self.assertTrue(all("duplicate source section title" in item["blockers"][0]
                            for item in result["unresolved_sections"]))
        result = self.plan(source, section="Unique")
        self.assertEqual(result["status"], "dry_run_only")
        self.assertFalse(result["partial_scope"])
        self.assert_blocked(self.plan(source, section="Repeat"), "duplicate source section title")

    def test_duplicate_only_selection_without_additions_is_blocked_not_healthy(self):
        source = source_for(("Repeat", prose("one")), ("Repeat", prose("two")))
        result = self.plan(source)
        self.assert_blocked(result, "duplicate source section title")
        self.assertTrue(result["partial_scope"])

    def test_partial_repair_survives_unrelated_complex_heading(self):
        retained, missing, after = prose("retained"), prose("missing"), prose("after")
        source = (r"\section{Methods}" + "\n" + retained + " " + missing + " " + after + "\n" +
                  r"\section{Unsafe boundary}" + "\n" + prose("unsafe") + "\n" +
                  r"\section{Bad \macro{heading}}" + "\n" + prose("bad") + "\n" +
                  r"\end{document}")
        old = copy.deepcopy(self.structured) + [chunk("Methods", retained + " " + after, 77)]
        before = copy.deepcopy(old)
        result = self.plan(source, old)
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual([item["text"] for item in result["additions"]], [missing])
        self.assertEqual(result["additions"][0]["chunk_index"], 78)
        self.assertTrue(result["partial_scope"])
        self.assertTrue(any(item["scope"] == "source_structure" for item in result["unresolved_sections"]))
        self.assertTrue(any(item["section_title"] == "Unsafe boundary"
                            for item in result["unresolved_sections"]))
        self.assertEqual(old, before)

    def test_unrelated_unsupported_header_before_unique_section_is_unresolved(self):
        source = r"\chapter{Unsupported}" + "\n" + source_for(("Unique", prose("unique")))
        result = self.plan(source)
        self.assertEqual(result["status"], "dry_run_only")
        self.assertTrue(result["partial_scope"])
        self.assertEqual([item["section_title"] for item in result["additions"]], ["Unique"])

    def test_unique_starred_short_heading_is_supported(self):
        source = r"\section*[Short]{Developer Study}" + "\n" + prose("study") + "\n" + r"\end{document}"
        result = self.plan(source)
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual(result["additions"][0]["section_title"], "Developer Study")

    def test_complex_title_and_unsupported_boundary_fail_closed(self):
        for source in (r"\section{Study \macro{nested}} body \end{document}",
                       r"\section{Study} body \chapter{Next} \end{document}"):
            with self.subTest(source=source):
                self.assertEqual(self.plan(source)["status"], "blocked")

    def test_macro_definition_cannot_supply_unique_source_section(self):
        source = r"\newcommand{\fake}{\section{Fake}" + prose("fake") + "}" + r"\end{document}"
        self.assertEqual(self.plan(source)["status"], "blocked")

    def test_missing_header_or_end_fails_closed(self):
        for source in ("plain body", r"\section{Discussions} " + prose("missing")):
            with self.subTest(source=source):
                self.assertEqual(self.plan(source)["status"], "blocked")

    def test_cache_prose_with_unknown_label_is_unresolved_without_canceling_safe_addition(self):
        result = self.plan(old=[chunk("Unknown label", prose("retained"))])
        self.assertEqual(result["status"], "dry_run_only")
        self.assertTrue(result["partial_scope"])
        self.assertEqual(result["unresolved_sections"][0]["scope"], "cache_mapping")
        self.assertEqual(result["unresolved_sections"][0]["section_title"], "Unknown label")
        self.assertEqual([item["section_title"] for item in result["additions"]], ["Discussions"])

    def test_unmapped_cache_without_addition_cannot_report_healthy(self):
        text = prose("retained")
        result = self.plan(source_for(("Methods", text)),
                           [chunk("Methods", text), chunk("Unknown", prose("unknown"), 1)])
        self.assert_blocked(result, "cannot be mapped to source")
        self.assertTrue(result["partial_scope"])

    def test_prose_retained_under_another_label_is_not_added(self):
        text = prose("retained")
        result = self.plan(source_for(("Current", text), ("Old", "")),
                           [chunk("Old", text)], section="Current")
        self.assert_blocked(result, "different cache section metadata")

    def test_partial_retention_under_other_label_is_not_double_added(self):
        shared = prose("shared")
        source = source_for(("Current", prose("missing") + " " + shared), ("Old", ""))
        old = [chunk("Old", shared + " " + prose("unrelated"))]
        self.assert_blocked(self.plan(source, old, section="Current"), "different cache section metadata")

    def test_partial_candidate_overlap_with_structured_cache_is_blocked(self):
        shared = prose("shared")
        old = [chunk("Methods", prose("retained")),
               chunk("Code", shared + " " + prose("unrelated"), 1, element_type="code")]
        source = source_for(("Methods", prose("retained") + " " + shared + " " + prose("missing")))
        self.assert_blocked(self.plan(source, old), "already retained in a cache chunk")

    def test_structured_global_text_type_and_algorithm_flags_are_guarded(self):
        for field, value in (("text", "changed"), ("element_type", "algorithm"), ("has_algorithm", True)):
            with self.subTest(field=field):
                full = copy.deepcopy(self.structured)
                full[0][field] = value
                self.assert_blocked(self.plan(full=full), "equation/algorithm/code sequence differs")

    def test_structured_mismatch_is_fatal_even_with_safe_candidates_and_unresolved_sections(self):
        source = source_for(("Repeat", prose("one")), ("Repeat", prose("two")), ("Unique", prose("unique")))
        full = copy.deepcopy(self.structured)
        full[0]["text"] = "changed equation"
        result = self.plan(source, full=full)
        self.assert_blocked(result, "equation/algorithm/code sequence differs")
        self.assertTrue(result["partial_scope"])

    def test_duplicate_additions_remain_globally_fatal(self):
        source = source_for(("First", prose("same")), ("Second", prose("same")))
        self.assert_blocked(self.plan(source), "duplicate missing prose candidates")

    def test_invalid_cache_text_is_globally_fatal(self):
        self.assert_blocked(self.plan(old=[chunk("Methods", None)]), "invalid text")

    def test_algorithm_marked_as_prose_is_not_appended(self):
        old = [chunk("Discussions", prose("algorithm"), has_algorithm=True)]
        result = self.plan(source_for(("Discussions", prose("algorithm"))), old,
                           bounded={r"\section{Discussions}" + "\n" + prose("algorithm"): old})
        self.assertEqual(result["status"], "no_change")

    def test_normal_full_parser_mode_does_not_require_recovery(self):
        self.assertEqual(self.plan(full_mode="pylatexenc")["status"], "dry_run_only")

    def test_legacy_full_or_bounded_parser_is_blocked(self):
        self.assert_blocked(self.plan(full_mode="legacy"), "full source requires pylatexenc")
        self.assert_blocked(self.plan(bounded_mode="legacy"), "bounded target requires pylatexenc")

    def test_max_additions_and_configuration_hard_bound(self):
        source = source_for(*[(f"Study {number}", prose(f"unique{number}")) for number in range(21)])
        self.assert_blocked(self.plan(source), "candidate count 21 exceeds limit 20")
        self.assert_blocked(self.plan(source, max_new_chunks=2), "exceeds limit 2")
        for limit in (0, 21, True):
            self.assert_blocked(self.plan(max_new_chunks=limit), "max_new_chunks")

    def test_duplicate_cache_indexes_are_rejected(self):
        old = copy.deepcopy(self.structured)
        old[1]["chunk_index"] = old[0]["chunk_index"]
        self.assert_blocked(self.plan(old=old), "duplicate chunk_index")

    def test_plan_is_deterministic_and_old_chunks_exactly_unchanged(self):
        old = copy.deepcopy(self.structured)
        before = copy.deepcopy(old)
        first = self.plan(old=old)
        second = self.plan(old=old)
        self.assertEqual(first, second)
        self.assertEqual(old, before)
        self.assertEqual(old + first["additions"], before + second["additions"])

    def test_overlapping_continuations_are_compared_without_double_coverage(self):
        first, overlap, last = prose("first"), prose("overlap"), prose("last")
        text = first + " " + overlap + " " + last
        old = [chunk("Methods", first + " " + overlap, 0, part_index=0, part_total=2),
               chunk("Methods", overlap + " " + last, 1, part_index=1, part_total=2)]
        result = self.plan(source_for(("Methods", text)), old)
        self.assertEqual(result["status"], "no_change")

    def test_empty_source_prose_does_not_invent_a_repair(self):
        result = self.plan(source_for(("Methods", "")), [])
        self.assertEqual(result["status"], "no_change")

    def test_windows_deadline_override_covers_every_parse_and_is_restored(self):
        original = repair.extractor._parse_deadline
        observed = []
        source = source_for(("Methods", prose("method")), ("Study", prose("study")))

        def parse(text, section_sink=None):
            observed.append(repair.extractor._parse_deadline is original)
            if text == source:
                return [], "pylatexenc_recovered"
            title = text.split("{", 1)[1].split("}", 1)[0]
            if section_sink is not None:
                section_sink.append({"section_title": title, "section_type": "other",
                                     "text": text.split("\n", 1)[1]})
            return [chunk(title, text.split("\n", 1)[1])], "pylatexenc_recovered"

        with patch.object(repair, "signal", SimpleNamespace()), \
                patch.object(repair.extractor, "build_chunks", side_effect=parse):
            result = repair.build_plan("fixture", source, [])
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual(observed, [False, False, False])
        self.assertIs(repair.extractor._parse_deadline, original)

    def test_deadline_is_restored_when_parsing_raises(self):
        original = repair.extractor._parse_deadline
        with patch.object(repair, "signal", SimpleNamespace()), \
                patch.object(repair.extractor, "build_chunks", side_effect=RuntimeError("fixture")):
            result = repair.build_plan("fixture", source_for(("Methods", prose("method"))), [])
        self.assertEqual(result["status"], "blocked")
        self.assertIs(repair.extractor._parse_deadline, original)


if __name__ == "__main__":
    unittest.main()
