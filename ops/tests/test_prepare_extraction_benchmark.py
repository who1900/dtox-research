"""Source JSON fixtures only: no readers, PDFs, subprocesses or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops import prepare_extraction_benchmark as prepare


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.selection_pin = self.pin("selection", "a" * 64)
        self.transfer_pin = self.pin("transfer", "b" * 64)
        self.review_pin = self.pin("review", "c" * 64)
        self.annotation_pin = self.pin("annotations", "d" * 64)
        self.sha = "e" * 64
        self.selection = {"protocol": {"selection_before_extractor_results": True},
                          "input_inventory": {"sha256": "f" * 64}, "candidates": [
                              {"document_id": "2302.07191", "cohort": "html_potential_control", "edition": "UNKNOWN"}]}
        self.transfer = {"selection_sha256": self.selection_pin["sha256"], "content_hashes_verified": True,
                         "documents": [{"document_id": "2302.07191", "sha256": self.sha,
                                        "source_uri": "https://arxiv.org/pdf/2302.07191", "edition": "UNKNOWN"}]}
        self.review = {"schema": prepare.CONTROLS, "reviewer": "independent-reader", "extractor_outputs_seen": False,
                       "documents": [{"document_id": "2302.07191", "original_sha256": self.sha,
                                      "pdfinfo_exit_code": 0, "page_count": 10,
                                      "pages": [self.page(1), self.page(5)],
                                      "body_passages": [{"page": 1, "text": "Abstract sentence", "region": "abstract"},
                                                        {"page": 5, "text": "True body sentence", "region": "left_column_body"}],
                                      "reading_order_anchors": [{"page": 5, "first": "left first", "then": "right next",
                                                                 "first_region": "left_column_body", "then_region": "right_column_body"}],
                                      "formula_table_source_notes": [{"page": 5, "note": "Table visible; formula present"}]}]}
        self.annotations = {"schema": prepare.ANNOTATIONS, "extractor_outputs_seen": False,
                            "frozen_gates": {"required_classes": ["encoding", "healthy"]},
                            "documents": [{"document_id": "2302.07191", "original_sha256": self.sha,
                                           "status": "verified", "reviewer": "source-class-reviewer", "evidence_uri": "source-only/notes",
                                           "classes": ["encoding", "healthy"], "two_column": True}]}

    def pin(self, name, sha):
        return {"path": str(self.root / (name + ".json")), "sha256": sha}

    def page(self, number):
        return {"page": number, "personally_viewed": True, "image_path": "source/page" + str(number) + ".png",
                "image_sha256": "1" * 64}

    def build(self):
        return prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)],
                             [(self.review, self.review_pin)], (self.annotations, self.annotation_pin))

    def test_actual_control_shape_abstract_not_body_and_notes_not_semantic(self):
        result = self.build()
        doc = result["manifest"]["documents"][0]
        self.assertEqual(doc["references"][0]["content_scope"], "other")
        self.assertEqual({r["type"] for r in doc["references"]}, {"snippet", "ordered_anchors"})
        self.assertEqual(doc["healthy_control_review"]["status"], "missing")
        self.assertFalse(result["report"]["source_reference_ready"])
        self.assertIn("MISSING_TWO_TRUE_BODY_PAGES_OR_LAYOUT_REVIEW", doc["exceptions"])

    def test_additive_true_body_page_closes_only_source_control_gap(self):
        self.review["documents"][0]["body_passages"].append({"page": 1, "text": "Introduction body", "region": "body"})
        doc = self.build()["manifest"]["documents"][0]
        self.assertEqual(doc["healthy_control_review"]["status"], "verified")
        self.assertEqual(doc["exceptions"], [])
        self.assertEqual(doc["source_version"], prepare.state())
        self.assertEqual(doc["historical_original_text_edition"], prepare.state())

    def test_missing_passage_region_retained_as_other_not_invented_body(self):
        self.review["documents"][0]["body_passages"][1].pop("region")
        doc = self.build()["manifest"]["documents"][0]
        self.assertEqual(doc["references"][1]["content_scope"], "other")
        self.assertEqual(doc["healthy_control_review"]["status"], "missing")

    def test_mixed_locator_from_to_anchor_is_preserved_unadapted_not_guessed(self):
        order = {"page": 5, "from": "left bottom: It contains 997 long", "to": "right top: video from QaEgo4D"}
        self.review["documents"][0]["reading_order_anchors"] = [order]
        result = self.build()
        doc = result["manifest"]["documents"][0]
        self.assertEqual(doc["unadapted_source_observations"][0]["record"], order)
        self.assertEqual(doc["unadapted_source_observations"][0]["status"], "NOT_A_DECLARED_CHECK")
        self.assertNotIn("UNADAPTED_READING_ORDER_EXPECTATIONS", doc["exceptions"])
        self.assertEqual(result["report"]["unadapted_reference_records"], 1)
        self.assertEqual(len(doc["references"]), 2)

    def test_missing_raw_transfer_keeps_row_and_source_refs_not_available(self):
        self.transfer["documents"] = []
        result = self.build()
        doc = result["manifest"]["documents"][0]
        self.assertEqual(doc["original"], {"status": "missing", "sha256": self.sha, "path": None})
        self.assertEqual(len(doc["references"]), 3)
        self.assertEqual(result["report"]["rows"], 1)
        self.assertFalse(result["report"]["source_reference_ready"])

    def test_unverified_transfer_never_promoted(self):
        self.transfer["content_hashes_verified"] = False
        self.assertEqual(self.build()["manifest"]["documents"][0]["original"]["status"], "unestablished")

    def test_available_visible_version_does_not_fabricate_historical_edition(self):
        self.review["documents"][0]["saved_pdf_visible_version"] = "v3"
        doc = self.build()["manifest"]["documents"][0]
        self.assertEqual(doc["source_version"], prepare.state("v3"))
        self.assertEqual(doc["historical_original_text_edition"], prepare.state())

    def test_no_class_inference_and_required_semantic_ref_missing(self):
        self.annotations["documents"] = []
        doc = self.build()["manifest"]["documents"][0]
        self.assertEqual(doc["classes"], [])
        self.assertIn("MISSING_SOURCE_CLASS_ANNOTATION", doc["exceptions"])
        self.annotations["documents"] = [{"document_id": "2302.07191", "original_sha256": self.sha,
                                          "status": "verified", "reviewer": "reader", "evidence_uri": "source/notes",
                                          "classes": ["tables", "math", "healthy"], "two_column": True}]
        doc = self.build()["manifest"]["documents"][0]
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:formula", doc["exceptions"])
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:table_cell", doc["exceptions"])

    def test_no_annotation_file_produces_explicit_unsealable_draft(self):
        result = prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)],
                               [(self.review, self.review_pin)])
        self.assertIsNone(result["manifest"]["preparation_inputs"]["annotations"])
        self.assertIsNone(result["frozen_gates"])
        self.assertFalse(result["report"]["source_reference_ready"])
        with self.assertRaisesRegex(ValueError, "invalid classes"):
            prepare.bench.validate_manifest(result["manifest"])

    def test_exact_semantic_refs_preserved_without_prose_inference(self):
        ref = {"check_id": "equation-5", "type": "formula", "page": 5, "locator": "equation 5",
               "expected": "x = y + z", "normalization": "exact", "content_scope": "body",
               "review": {"status": "verified", "method": "independent_page_review", "reviewer": "reader",
                          "evidence_uri": "source/equation-page.png", "original_sha256": self.sha}}
        exact = {"schema": prepare.EXACT, "reviewer": "reader", "extractor_outputs_seen": False,
                 "documents": [{"document_id": "2302.07191", "original_sha256": self.sha, "references": [ref]}]}
        adapted = prepare.adapt_review(exact, self.review_pin)
        self.assertEqual(adapted[0]["references"], [ref])
        table = dict(ref, check_id="table-1-cell", type="table_cell", expected="17.2", table_id="T1", row=0, column=2)
        exact["documents"][0]["references"].append(table)
        self.assertEqual(prepare.adapt_review(exact, self.review_pin)[0]["references"][1], table)
        exact["documents"][0]["references"][0]["review"]["status"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "independent verified source"):
            prepare.adapt_review(exact, self.review_pin)

    def test_source_image_exact_adapter_is_source_json_only(self):
        ref = {"check_id": "page-tables", "type": "table_cell", "page": 5,
               "locator": "physical page 5", "source_locator": "physical page 5: all table cells",
               "expected_kind": "source_image", "expected": "all table cells on this source page",
               "expected_image": {"path": str(self.root / "not-opened.png"), "sha256": "f" * 64},
               "original_sha256": self.sha, "normalization": "exact", "content_scope": "body",
               "review": {"status": "verified", "method": "independent_page_review", "reviewer": "reader",
                          "evidence_uri": "source/page5", "original_sha256": self.sha, "image_sha256": "f" * 64}}
        bundle = {"schema": prepare.EXACT, "reviewer": "reader", "extractor_outputs_seen": False,
                  "documents": [{"document_id": "2302.07191", "original_sha256": self.sha,
                                 "page_count": 8, "references": [ref]}]}
        with patch.object(Path, "open", side_effect=AssertionError("adapter must not read images")):
            result = prepare.adapt_review(bundle, self.review_pin)
        self.assertEqual(result[0]["references"], [ref])
        bundle["documents"][0]["references"][0]["page"] = 9
        with self.assertRaisesRegex(ValueError, "physical page outside"):
            prepare.adapt_review(bundle, self.review_pin)

    def test_main8_schema_preserves_title_only_scope(self):
        main = {"schema": prepare.MAIN, "reviewer": "main", "extractor_outputs_seen": False,
                "documents": [{"document_id": "2302.07191", "original_sha256": self.sha, "page": 1,
                               "image_path": "source/cover.png", "image_sha256": "1" * 64,
                               "visually_readable_in_scope": True, "body_claimed": False,
                               "neutral_checks": {"snippets": ["Cover title"], "reading_order": []}}]}
        refs = prepare.adapt_review(main, self.review_pin)[0]["references"]
        self.assertEqual(refs[0]["expected"], "Cover title")
        self.assertNotEqual(refs[0]["content_scope"], "body")

    def test_duplicate_rows_and_physical_aliases_counted_without_replacement(self):
        self.selection["candidates"] *= 2
        result = self.build()
        self.assertEqual(result["report"]["rows"], 2)
        self.assertEqual(result["report"]["duplicate_id_rows"], 1)
        self.assertEqual(result["report"]["known_physical_pdfs"], 1)
        self.assertEqual(len(result["manifest"]["documents"]), 2)

    def test_source_only_binding_and_foreign_inputs_fail_closed(self):
        for mutation in ("outputs", "sha", "foreign", "selection", "unknown_schema", "unviewed"):
            review, transfer = copy.deepcopy(self.review), copy.deepcopy(self.transfer)
            if mutation == "outputs": self.review["extractor_outputs_seen"] = True
            if mutation == "sha": self.review["documents"][0]["original_sha256"] = "0" * 64
            if mutation == "foreign": self.review["documents"][0]["document_id"] = "foreign"
            if mutation == "selection": self.transfer["selection_sha256"] = "0" * 64
            if mutation == "unknown_schema": self.review["schema"] = "extractor-output"
            if mutation == "unviewed": self.review["documents"][0]["pages"][0]["personally_viewed"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.build()
            self.review, self.transfer = review, transfer

    def test_reproducible_draft_and_frozen_gates_not_tuned(self):
        self.assertEqual(self.build(), self.build())
        self.assertEqual(self.build()["frozen_gates"], self.annotations["frozen_gates"])
        self.assertFalse(self.build()["report"]["quality_measured"])

    def test_100_physical_80_20_source_ready_is_not_quality_acceptance(self):
        self.selection["candidates"], self.transfer["documents"], self.review["documents"], self.annotations["documents"] = [], [], [], []
        for index in range(100):
            identity, sha = str(index), prepare.bench.sha(str(index).encode())
            control = index >= 80
            self.selection["candidates"].append({"document_id": identity,
                "cohort": "html_potential_control" if control else "cached_pdf_challenge"})
            self.transfer["documents"].append({"document_id": identity, "sha256": sha, "source_uri": "https://example.invalid/" + identity})
            self.review["documents"].append({"document_id": identity, "original_sha256": sha, "pdfinfo_exit_code": 0,
                "page_count": 10, "pages": [self.page(1), self.page(5)], "body_passages": [
                    {"page": 1, "text": "body first", "region": "body"}, {"page": 5, "text": "body second", "region": "body"}]})
            self.annotations["documents"].append({"document_id": identity, "original_sha256": sha, "status": "verified",
                "classes": ["encoding", "healthy"] if control else ["encoding"], "reviewer": "source-reader",
                "evidence_uri": "source/notes", "two_column": False})
        report = self.build()["report"]
        self.assertTrue(report["source_reference_ready"])
        self.assertEqual(report["known_physical_pdfs"], 100)
        self.assertFalse(report["quality_measured"])
        self.assertEqual(report["gate_status"], "NOT_EVALUATED_NO_EXTRACTION_OUTPUTS")
        description = {"page": 5, "anchors": ["left references", "right references"]}
        self.review["documents"][0]["reading_order_anchors"] = [description]
        self.annotations["documents"][0]["classes"].append("layout")
        missing = self.build()
        doc = missing["manifest"]["documents"][0]
        self.assertFalse(missing["report"]["source_reference_ready"])
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:ordered_anchors", doc["exceptions"])
        self.assertEqual(doc["unadapted_source_observations"], [{"reason": "UNADAPTED_READING_ORDER_EXPECTATIONS",
                         "status": "NOT_A_DECLARED_CHECK", "record": description}])
        self.review["documents"][0]["reading_order_anchors"].append({"page": 5, "first": "body second",
            "then": "independently verified continuation", "first_region": "body", "then_region": "body"})
        covered = self.build()
        self.assertTrue(covered["report"]["source_reference_ready"])
        self.assertEqual(covered["manifest"]["documents"][0]["unadapted_source_observations"], doc["unadapted_source_observations"])
        self.assertEqual(covered["report"]["unadapted_reference_records"], 1)
        self.transfer["documents"][-1]["sha256"] = self.transfer["documents"][-2]["sha256"]
        self.review["documents"][-1]["original_sha256"] = self.transfer["documents"][-1]["sha256"]
        self.annotations["documents"][-1]["original_sha256"] = self.transfer["documents"][-1]["sha256"]
        report = self.build()["report"]
        self.assertEqual(report["physical_alias_ids"], 1)
        self.assertFalse(report["source_reference_ready"])

    def test_cli_only_pinned_json_exclusive_write_no_raw_reads(self):
        argv = []
        for role, data in (("selection", self.selection), ("transfer", self.transfer),
                           ("review", self.review), ("annotations", self.annotations)):
            if role == "transfer":
                data["selection_sha256"] = prepare.bench.file_sha(self.root / "selection.json")
            path = self.root / (role + ".json")
            path.write_bytes(prepare.bench.encoded(data))
            argv.extend(["--" + role, str(path), prepare.bench.file_sha(path)])
        output = self.root / "prepared.json"
        argv.extend(["--output", str(output)])
        original_open = Path.open
        reads = []
        def opened(path, mode="r", *args, **kwargs):
            if "r" in mode:
                reads.append(path)
                self.assertTrue(path.suffix == ".json" or path in
                                {Path(prepare.__file__), Path(prepare.bench.__file__)})
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", opened):
            self.assertEqual(prepare.main(argv), 0)
            with self.assertRaises(FileExistsError): prepare.main(argv)
        self.assertTrue(output.is_file())
        self.assertFalse(any(path.suffix in (".pdf", ".png") for path in reads))

    def test_input_pin_mismatch_and_output_schema_refused(self):
        path = self.root / "input.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            prepare.load_pin(str(path), "0" * 64)

    def scope_input(self):
        doc = self.review["documents"][0]
        return {"schema": prepare.CONTROL_SCOPES, "reviewer": "luna", "extractor_outputs_seen": False,
                "source_review": self.review_pin, "documents": [{
                    "document_id": doc["document_id"], "original_sha256": self.sha, "two_column": True,
                    "reviewed_page_layouts": [dict(self.page(page), visible_layout="two_column_body") for page in (1, 5)],
                    "reference_annotations": [{"source_pointer": "/documents/0/body_passages/" + str(i),
                        "page": passage["page"], "text": passage["text"], "image_path": self.page(passage["page"])["image_path"],
                        "image_sha256": "1" * 64, "original_sha256": self.sha, "personally_viewed": True,
                        "scope": "other" if i == 0 else "body", "counts_as_body": i != 0,
                        "previous_region": passage["region"]} for i, passage in enumerate(doc["body_passages"])],
                    "additional_source_references": [{"page": 1, "text": "Author Introduction", "scope": "body", "region": "body",
                        "image_sha256": "1" * 64, "original_sha256": self.sha, "personally_viewed": True,
                        "counts_as_body": True, "extractor_outputs_seen": False}],
                    "reading_order_annotations": [{"page": 5, "anchors": ["left bottom: Left Literal,", "right top: Right Literal!"],
                        "original_sha256": self.sha, "image_sha256": "1" * 64, "personally_viewed": True, "scope": "body"}]}]}

    def test_actual_scope_merge_is_additive_bound_and_does_not_edit_old_review(self):
        scope, scope_pin = self.scope_input(), self.pin("scope", "2" * 64)
        old = copy.deepcopy(self.review)
        inputs = [(self.review, self.review_pin), (scope, scope_pin)]
        prepared = prepare.scoped_reviews(inputs)
        self.assertEqual(self.review, old)
        self.assertEqual(len(prepared), 1)
        adapted = prepare.adapt_review(*prepared[0])[0]
        self.assertEqual(adapted["references"][0]["content_scope"], "other")
        self.assertEqual(adapted["references"][2]["content_scope"], "body")
        order = adapted["references"][3]
        self.assertEqual(order["expected"], ["Left Literal,", "Right Literal!"])
        self.assertEqual(order["normalization"], "nfc_whitespace")
        self.assertIn("left bottom", order["locator"])
        self.assertIn("legacy_record", order["review"]["scope_annotation"])
        self.assertEqual(prepare.derive_annotations((self.selection, self.selection_pin), inputs)["documents"][0]["classes"], ["healthy", "layout"])

    def test_scope_wrong_sha_pointer_text_page_or_image_is_refused(self):
        for mutation in ("sha", "pointer", "text", "page", "image", "scope"):
            scope = self.scope_input()
            item = scope["documents"][0]["reference_annotations"][0]
            if mutation == "sha": scope["source_review"] = dict(self.review_pin, sha256="0" * 64)
            if mutation == "pointer": item["source_pointer"] = "/other"
            if mutation == "text": item["text"] = "Changed text"
            if mutation == "page": item["page"] = 5
            if mutation == "image": item["image_sha256"] = "0" * 64
            if mutation == "scope": item["scope"] = "body"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                prepare.scoped_reviews([(self.review, self.review_pin), (scope, self.pin("scope", "2" * 64))])

    def challenge_inputs(self):
        chosen = {"document_id": "challenge:one", "cohort": "cached_pdf_challenge",
                  "inventory_record": {"status_provenance": {"status": "NEEDS_GLYPH_VERIFICATION", "payload_sha256": "4" * 64}}}
        self.selection["candidates"] = [chosen]
        self.transfer["documents"][0]["document_id"] = chosen["document_id"]
        challenge = {"schema": prepare.CHALLENGES, "reviewer": "luna", "extractor_outputs_seen": False,
                     "selection_sha256": self.selection_pin["sha256"], "documents": [{
                         "document_id": chosen["document_id"], "original_sha256": self.sha, "page_count": 10,
                         "source_observed_classes": ["two_column", "display_formula", "numeric_table", "source_prompt_example"],
                         "old_failure_evidence": {"status": "NEEDS_GLYPH_VERIFICATION", "payload_sha256": "4" * 64},
                         "pages": [dict(self.page(page), visually_viewed=True, layout="two_column_body") for page in (1, 5)],
                         "body_passages": [{"page": 1, "text": "Literal α, Case!", "scope": "body", "section": "Introduction"},
                                           {"page": 5, "text": "Literal conclusion.", "scope": "body", "section": "Conclusion"}],
                         "reading_order": [{"page": 5, "anchors": ["left: Left Literal,", "right top: Right Literal!"]}]}]}
        return [(challenge, self.pin("challenge", "3" * 64))]

    def test_actual_challenge_classes_keep_hard_dimensions_and_old_flag_provenance(self):
        inputs = self.challenge_inputs()
        result = prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)], inputs, derive=True)
        label = result["source_annotations"]["documents"][0]
        self.assertEqual(label["classes"], ["encoding", "layout", "math", "tables"])
        self.assertTrue(label["class_provenance"]["encoding_old_selection_flag"]["not_current_extraction_failure"])
        self.assertNotIn("source", label["classes"])
        self.assertEqual(result["frozen_gates"]["required_classes"], ["encoding", "layout", "math", "tables"])
        self.assertEqual(result["frozen_gates"]["min_reviewed_checks_per_document"], 2)
        doc = result["manifest"]["documents"][0]
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:formula", doc["exceptions"])
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:table_cell", doc["exceptions"])
        self.assertEqual(result["report"]["semantic_missing_documents"]["math"], ["challenge:one"])
        self.assertEqual(result["report"]["class_document_counts"]["tables"], 1)
        self.assertFalse(result["report"]["source_reference_ready"])
        self.assertEqual(doc["references"][0]["expected"], "Literal α, Case!")
        self.assertTrue(all(ref["normalization"] == "nfc_whitespace" for ref in doc["references"]))

    def test_old_encoding_flag_requires_matching_selection_payload(self):
        inputs = self.challenge_inputs()
        inputs[0][0]["documents"][0]["old_failure_evidence"]["payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "old-selection evidence mismatch"):
            prepare.derive_annotations((self.selection, self.selection_pin), inputs)

    def test_gold_source_input_pins_and_declared_classes_are_bound(self):
        inputs = self.challenge_inputs()
        inputs[0][0]["documents"][0]["source_observed_classes"] = ["two_column"]
        ref = {"check_id": "gold-tables", "type": "table_cell", "page": 5, "locator": "page 5",
               "source_locator": "entire physical page 5: all table cells", "expected_kind": "source_image",
               "expected": "all table cells on this source page", "normalization": "exact",
               "original_sha256": self.sha,
               "expected_image": {"path": str(self.root / "never-read.png"), "sha256": "f" * 64},
               "review": {"status": "verified", "method": "independent_page_review", "reviewer": "luna",
                          "evidence_uri": "source/page5", "image_sha256": "f" * 64, "original_sha256": self.sha}}
        gold = {"schema": prepare.EXACT, "reviewer": "luna", "extractor_outputs_seen": False,
                "source_inputs": [copy.deepcopy(inputs[0][1])], "selection_sha256": self.selection_pin["sha256"],
                "documents": [{"document_id": "challenge:one", "original_sha256": self.sha, "page_count": 10,
                               "references": [ref]}]}
        inputs.append((gold, self.pin("gold", "6" * 64)))
        result = prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)], inputs, derive=True)
        self.assertEqual(result["frozen_gates"]["required_classes"], ["encoding", "layout", "tables"])
        self.assertNotIn("MISSING_REQUIRED_SOURCE_REFERENCE:table_cell", result["manifest"]["documents"][0]["exceptions"])
        gold["source_inputs"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "gold source input pin"):
            prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)], inputs, derive=True)

    def test_known_spatial_prefixes_only_and_no_fuzzy_case_or_punctuation_repair(self):
        self.assertEqual(prepare.anchors_from_source(["left bottom: A—B,", "right top: α Case!"]), ["A—B,", "α Case!"])
        for prefix in prepare.SPATIAL_PREFIXES:
            self.assertEqual(prepare.anchors_from_source([prefix + ": hpx: α—B,", "right: Case!"]),
                             ["hpx: α—B,", "Case!"])
        self.assertIsNone(prepare.anchors_from_source(["hpx: runtime", "right: Other"]))
        self.assertIsNone(prepare.anchors_from_source(["left Results: Actual", "right: Other"]))
        self.assertIsNone(prepare.anchors_from_source(["full-width Table 2", "right: Other"]))
        self.assertIsNone(prepare.anchors_from_source(["arbitrary: body", "right: Other"]))

    def test_descriptive_challenge_order_retained_not_promoted_to_literal(self):
        inputs = self.challenge_inputs()
        inputs[0][0]["documents"][0]["reading_order"][0]["anchors"] = ["full-width Figure 2", "left references"]
        result = prepare.build((self.selection, self.selection_pin), [(self.transfer, self.transfer_pin)], inputs, derive=True)
        self.assertEqual(result["report"]["unadapted_reference_records"], 1)
        self.assertEqual(len(result["manifest"]["documents"][0]["references"]), 2)
        self.assertIn("MISSING_REQUIRED_SOURCE_REFERENCE:ordered_anchors", result["manifest"]["documents"][0]["exceptions"])

    def test_common_exact_input_refs_are_frozen_as_whitespace_only_not_semantic(self):
        refs = [{"check_id": "literal", "type": "snippet", "page": 1, "locator": "source paragraph",
                 "content_scope": "body", "expected": "Literal α, Case!", "normalization": "exact",
                 "review": {"status": "verified", "method": "independent_page_review", "reviewer": "reader",
                            "evidence_uri": "source/page.png", "original_sha256": self.sha}}]
        bundle = {"schema": prepare.EXACT, "reviewer": "reader", "extractor_outputs_seen": False,
                  "documents": [{"document_id": "2302.07191", "original_sha256": self.sha, "references": refs}]}
        adapted = prepare.adapt_review(bundle, self.review_pin)[0]["references"]
        self.assertEqual(adapted[0]["normalization"], "nfc_whitespace")
        self.assertEqual(adapted[0]["expected"], "Literal α, Case!")
        self.assertEqual(refs[0]["normalization"], "exact")


if __name__ == "__main__":
    unittest.main()
