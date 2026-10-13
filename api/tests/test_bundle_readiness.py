"""Offline production contracts: bounded evidence is not source-wide absence."""
import json
import unittest

from api.evidence import bounded_evidence_page, provenance, resolution_reasons
from api.research_bundle import BundleTimeBudgetExceeded, ResearchBundleBody, build_bundle


URL = "https://sources.example.test/paper"


def hit(source="latex", parser_mode=None):
    metadata = {"fulltext_source": source, "source_url": URL}
    if parser_mode is not None:
        metadata["parser_mode"] = parser_mode
    evidence = provenance("record:generic", {}, metadata)
    return {"arxiv_id": "record:generic", "title": "Исследование α", "url": URL,
            "fulltext": evidence["fulltext"], "evidence": evidence}


def bundle(points, selected=None, **options):
    return build_bundle(ResearchBundleBody(query="UTF8 α", **options), {},
                        [selected or hit()], lambda _pid: points)


class BundleReadinessTests(unittest.TestCase):
    def test_latex_pdf_markdown_preserve_every_structured_category_and_utf8(self):
        fragments = [
            {"text": r"\sum_{i=1}^{n} α_i = β", "element_type": "equation"},
            {"text": "Алгоритм: выбрать источник → проверить", "element_type": "algorithm"},
            {"text": "| Метод | Точность |\n| α | 0.95 |", "element_type": "table"},
            {"text": "Ограничения: неполный корпус 📚", "section_type": "limitations"},
            {"text": "verify(α, β)", "element_type": "code"}]
        for source, parser, extraction, exact in (
                ("latex", "pylatexenc", "latex_source", True),
                ("latex", "pylatexenc_recovered", "latex_source", True),
                ("whitepaper-pdf", None, "pdf_text", False),
                ("oa-mirror-pdf", "markdown", "pdf_text", False),
                ("spec-markdown", "markdown", "markdown_source", False),
                ("latex", "markdown", "markdown_source", False)):
            with self.subTest(source=source, parser=parser):
                out = bundle([{"payload": f} for f in fragments], hit(source, parser))
                self.assertFalse(out["partial"])
                self.assertEqual(out["partial_reasons"], [])
                for category, fragment in zip(("equations", "algorithms", "tables", "limitations", "code"), fragments):
                    row = out[category][0]
                    self.assertEqual(row["text"], fragment["text"])
                    self.assertEqual(row["evidence"]["extraction_type"], extraction)
                    self.assertIs(row["evidence"]["exact_latex"], exact)
                    self.assertEqual(row["evidence"]["parse_completeness"], "unknown")
                    expected = provenance("record:generic", fragment, scope="indexed_chunk")
                    self.assertEqual(row["evidence"]["fragment_hash"], expected["fragment_hash"])
                self.assertEqual(json.loads(json.dumps(out, ensure_ascii=False)), out)

    def test_character_bound_keeps_first_equation_and_only_reports_budget(self):
        raw = "α📚" * 900
        out = bundle([{"payload": {"text": raw, "element_type": "equation"}}], max_chars=500)
        row = out["equations"][0]
        self.assertEqual(row["text"], raw[:500])
        self.assertTrue(row["clipped"])
        self.assertTrue(out["partial"])
        self.assertEqual(out["partial_reasons"], ["character_budget"])
        self.assertEqual(row["evidence"]["fragment_chars"], len(raw))
        self.assertEqual(row["evidence"]["returned_chars"], 500)
        self.assertEqual(out["papers"][0]["missing"]["algorithms"], "not_found_in_retrieved_chunks")

    def test_known_paper_without_objects_is_not_retrieval_failure(self):
        out = bundle([{"payload": {"text": "Method prose", "element_type": "paragraph"}}])
        self.assertFalse(out["partial"])
        self.assertEqual(set(out["papers"][0]["missing"].values()), {"not_found_in_retrieved_chunks"})
        empty = bundle([])
        self.assertEqual(empty["partial_reasons"], ["no_indexed_chunks"])
        self.assertNotIn("retrieval_error", empty["papers"][0])

    def test_abstract_fallback_and_acquired_latex_do_not_prove_indexed_fulltext(self):
        points = [{"payload": {"section_type": "abstract", "text": "Краткий обзор"}}]
        for selected in (hit("abstract-fallback"), hit("latex")):
            with self.subTest(acquired=selected["fulltext"]):
                out = bundle(points, selected)
                self.assertEqual(out["papers"][0]["fulltext"], selected["fulltext"])
                self.assertEqual(out["papers"][0]["indexed_content"], "abstract_only")
                self.assertEqual(out["papers"][0]["indexed_content_scope"], "retrieved_chunks")
                self.assertEqual(out["partial_reasons"], ["abstract_only"])
                self.assertEqual(out["papers"][0]["missing"]["equations"], "abstract_only")

    def test_timeout_is_failure_unless_explicit_local_budget_exhaustion(self):
        for error, reason in ((TimeoutError("transport timeout"), "retrieval_failed"),
                              (RuntimeError("private detail"), "retrieval_failed"),
                              (BundleTimeBudgetExceeded("budget"), "time_budget")):
            def retrieve(_pid):
                raise error
            out = build_bundle(ResearchBundleBody(query="q"), {}, [hit()], retrieve)
            self.assertEqual(out["partial_reasons"], [reason])
            self.assertEqual(out["papers"][0]["missing"]["equations"], reason)
            self.assertNotIn("private detail", json.dumps(out))

    def test_envelope_errors_cannot_hide_behind_partial_false_or_budget(self):
        envelope = {"sources": [{"id": "record:generic", "metadata": {"fulltext_source": "latex"},
                                  "passport": {"url": URL},
                                  "points": [{"payload": {"text": "x" * 800, "element_type": "equation"}}]}],
                    "resolution": {"partial": False, "errors": [{"error": "TimeoutError"}]}}
        out = bundle(envelope, max_chars=500)
        self.assertEqual(out["partial_reasons"], ["character_budget", "retrieval_failed"])
        self.assertTrue(out["partial"])
        self.assertTrue(out["equations"])

    def test_invalid_ownership_and_malformed_points_are_not_silent_absence(self):
        for points, reason in (([{"payload": {"arxiv_id": "other", "text": "x=1", "element_type": "equation"}}], "invalid_provenance"),
                               ([None, {"payload": {"text": 123}}], "retrieval_failed"),
                               ({"unexpected": "shape"}, "retrieval_failed")):
            with self.subTest(reason=reason):
                out = bundle(points)
                self.assertEqual(out["equations"], [])
                self.assertTrue(out["partial"])
                self.assertIn(reason, out["partial_reasons"])
                self.assertEqual(out["papers"][0]["missing"]["equations"], reason)

    def test_invalid_source_url_cannot_claim_verified_provenance(self):
        selected = hit()
        selected["url"] = "javascript:alert(1)"
        selected["evidence"]["source_url_status"] = "verified_live"
        out = bundle([{"payload": {"text": "x=1", "element_type": "equation"}}], selected)
        evidence = out["equations"][0]["evidence"]
        self.assertTrue(out["partial"])
        self.assertEqual(out["partial_reasons"], ["invalid_provenance"])
        self.assertEqual(evidence["provenance_reason"], "source_url_invalid")
        self.assertEqual(evidence["source_url_status"], "source_missing")
        self.assertIsNone(evidence["source_url_origin"])


class CommonEvidenceHelpersTests(unittest.TestCase):
    def rows(self):
        fragments = [{"text": "α📚" * 600, "element_type": "equation"},
                     {"text": "Алгоритм → проверка", "element_type": "algorithm"}]
        return [{**fragment, "evidence": provenance("record:generic", fragment,
                 {"fulltext_source": "latex", "source_url": URL}, scope="indexed_chunk")}
                for fragment in fragments]

    def test_missing_wanted_type_is_not_retrieval_failed(self):
        self.assertEqual(resolution_reasons(missing_elements={"algorithm"}), ["not_found_in_retrieved_chunks"])
        self.assertEqual(resolution_reasons(), [])
        self.assertEqual(resolution_reasons(errors=[{}], missing_elements={"algorithm"}, time_budget_exhausted=True),
                         ["not_found_in_retrieved_chunks", "retrieval_failed", "time_budget"])
        self.assertEqual(resolution_reasons(has_indexed_chunks=False), ["no_indexed_chunks"])

    def test_oversized_first_element_has_lossless_utf8_continuation(self):
        rows = self.rows()
        before = json.dumps(rows, ensure_ascii=False)
        text, cursor, pages = [], None, []
        while True:
            page = bounded_evidence_page(rows, 500, cursor)
            pages.append(page)
            self.assertGreater(page["returned"], 0)
            self.assertLessEqual(page["chars"], 500)
            text.extend(row["text"] for row in page["sections"])
            cursor = page["continuation"]
            if cursor is None:
                break
            self.assertLess(len(pages), 10)
        self.assertEqual("".join(text), "".join(row["text"] for row in rows))
        self.assertTrue(pages[0]["truncated"])
        self.assertEqual(pages[0]["available"], 2)
        self.assertEqual(pages[0]["sections"][0]["evidence"]["fragment_hash"], rows[0]["evidence"]["fragment_hash"])
        self.assertEqual(pages[1]["sections"][0]["evidence"]["returned_offset"], 500)
        self.assertFalse(pages[-1]["truncated"])
        self.assertEqual(json.dumps(rows, ensure_ascii=False), before)

    def test_cursor_rejects_changed_reordered_or_invalid_snapshot(self):
        rows = self.rows()
        cursor = bounded_evidence_page(rows, 500)["continuation"]
        changed = [{**rows[0], "text": "different"}, rows[1]]
        for data, continuation in ((changed, cursor), (list(reversed(rows)), cursor),
                                   (rows, {**cursor, "offset": -1}), (rows, {**cursor, "fragment_index": True})):
            with self.subTest(cursor=continuation), self.assertRaises(ValueError):
                bounded_evidence_page(data, 500, continuation)
        self.assertEqual(bounded_evidence_page([], 500)["continuation"], None)
        for budget in (0, -1, True):
            with self.assertRaises(ValueError):
                bounded_evidence_page(rows, budget)


if __name__ == "__main__":
    unittest.main()
