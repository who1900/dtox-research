"""Temporary byte fixtures and injected executors only; no PDFs, jobs or network."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops import source_extraction_benchmark as bench


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.originals = self.root / "originals"
        self.originals.mkdir()
        baseline = {"comparison_kind": "current_source_equivalent", "description": "pinned current-source ARM fixture",
                    "deployed_extractor_sha256": "c112a5df23ff588ab4dee3f46676a703a009ce0f308058bea073cd11eb50e503",
                    "common_patched_sha256": "a6ced24e14c96bb073d0fee6dc0af45b4893ec0eddb895e96ed4213a1bc6b5d9",
                    "runtime_monkeypatch": False, "internal_timeouts_seconds": {"arxiv_pdf": 90, "other_pdf": 120}}
        self.config = {
            "schema": bench.SCHEMA, "job_runtime": "infinity", "automatic_retry": False,
            "baseline_contract": baseline,
            "environment": {"architecture": "aarch64", "cpu_count": 16, "ram_bytes": 64 * 1024 ** 3,
                            "python_executable": "/runtime/python", "python_version": "3.13.0",
                            "venv": "/opt/dtox-extraction-eval-20261012/venv",
                            "packages": {"docling": "2.137.0", "pymupdf": "1.28.0", "pymupdf4llm": "1.28.0",
                                         "pymupdf-layout": "1.28.0", "torch": "2.14.1+cpu", "torchvision": "0.29.1+cpu"}},
            "sandbox": {"launcher": {"path": "/runtime/bwrap", "sha256": "a" * 64},
                        "setpriv": {"path": "/usr/bin/setpriv", "sha256": "9" * 64},
                        "os_readonly": [], "forbidden_roots": [str(self.root / "production")],
                        "readonly": [{"path": "/runtime", "kind": "tree", "sha256": "b" * 64}]},
            "engines": {name: {"role": "exact_current_baseline" if name == "pymupdf" else "candidate",
                               "argv": ["/runtime/python", "/runtime/runner.py", "{input}", "{output}", "{config}", "{request}"],
                               "description": "mock-only pinned runner", "whole_document": True,
                               "output_scope": "pages",
                               "config": {"path": "/runtime/" + name + ".json", "sha256": "c" * 64},
                               "dependency_manifest_sha256": "d" * 64,
                               "pins": [{"path": "/runtime/runner.py", "sha256": "e" * 64}]} for name in bench.ENGINES},
            "document_limits": {"label": "unlimited-whole-document", "wall_seconds": None,
                                "cpu_seconds": None, "address_space_bytes": None},
            "gates": {"expected_physical_documents": 10, "min_documents_per_class_split": 1,
                      "min_reviewed_checks_per_document": 1,
                      "min_observed_check_ratio": {"all": 1},
                      "max_healthy_regressions": 0, "max_class_regressions": 0, "min_candidate_check_gain": 1}}
        self.config["engines"]["pymupdf"]["pins"].extend([
            {"path": "/runtime/extractor.py", "sha256": baseline["deployed_extractor_sha256"]},
            {"path": "/runtime/common_util.py", "sha256": baseline["common_patched_sha256"]},
            {"path": "/runtime/pdf_memory.py", "sha256": "a" * 64}])
        baseline["source_pins"] = copy.deepcopy(self.config["engines"]["pymupdf"]["pins"][1:])
        self.manifest = {"schema": bench.SCHEMA,
                         "sampling": {"method": "preselected independent mock inventory",
                                      "selected_before_extraction": True, "success_independent": True,
                                      "inventory_sha256": "f" * 64},
                         "documents": [self.document(i) for i in range(10)]}

    def document(self, number):
        path = self.originals / str(number)
        path.write_bytes(("mock-original-" + str(number)).encode())
        original_sha = bench.file_sha(path)
        doc = {"document_id": "paper:" + str(number), "classes": list(bench.CLASSES),
                "source_uri": {"status": "established", "value": "https://example.invalid/paper/" + str(number)},
                "source_version": {"status": "established", "value": "v1"},
                "page_count": {"status": "established", "value": 2},
                "real_document": {"status": "established", "value": True}, "source_kind": "other_pdf",
                "original": {"path": str(path), "sha256": original_sha, "status": "available"},
                "exceptions": [], "references": [{"check_id": "snippet", "type": "snippet", "page": 2,
                    "content_scope": "body",
                    "locator": "page 2, paragraph 1", "normalization": "exact", "expected": "verified body",
                    "review": {"status": "verified", "method": "independent_page_review", "reviewer": "mock-reviewer",
                               "evidence_uri": "mock-only/page-2", "original_sha256": original_sha}}]}
        doc["healthy_control_review"] = {"status": "verified", "method": "independent_whole_document_review",
                                         "two_column": True,
                                         "reviewer": "mock-only", "evidence_uri": "mock-only/control-review",
                                         "original_sha256": original_sha}
        for kind in ("ordered_anchors", "formula", "table_cell", "section"):
            ref = copy.deepcopy(doc["references"][0])
            ref.update(check_id=kind, type=kind)
            if kind == "ordered_anchors":
                ref["expected"] = ["A then", "B"]
                ref["page"] = 1
                ref["locator"] = "page 1, body columns reading order"
            if kind == "table_cell":
                ref.update(table_id="T1", row=0, column=0)
            if kind == "section":
                ref["title"] = "Methods"
            doc["references"].append(ref)
        return doc

    def freeze(self):
        return bench.freeze(self.manifest, self.config)

    def image_ref(self):
        doc = self.manifest["documents"][0]
        image = self.root / "source-page.png"
        image.write_bytes(b"mock gold bytes, not an actual PNG")
        ref = next(r for r in doc["references"] if r["type"] == "table_cell")
        ref.pop("table_id"); ref.pop("row"); ref.pop("column")
        ref.update(expected_kind="source_image", expected="all table cells on this source page",
                   source_locator="physical page 2: all table cells on this source page",
                   expected_image={"path": str(image), "sha256": bench.file_sha(image)},
                   original_sha256=doc["original"]["sha256"])
        ref["review"]["image_sha256"] = ref["expected_image"]["sha256"]
        return doc, ref, image

    def test_image_gold_bounds_and_no_invented_coordinates(self):
        doc, ref, _ = self.image_ref()
        bench.validate_manifest(self.manifest)
        for field, value in (("page", 3), ("page", 0), ("page", True), ("source_locator", ""),
                             ("original_sha256", "0" * 64), ("type", "snippet"),
                             ("expected_image", {"path": "relative.png", "sha256": "a" * 64}),
                             ("expected_image", {"path": str(self.root / "x"), "sha256": "bad"})):
            with self.subTest(field=field, value=value):
                altered = copy.deepcopy(ref); altered[field] = value
                with self.assertRaises(ValueError):
                    bench.validate_source_image(altered, doc["original"]["sha256"], 2)

    def test_image_gold_seal_hashes_bytes_and_reference_binds_region(self):
        _, ref, image = self.image_ref()
        seal = self.freeze()
        self.assertEqual(bench.gold_image_pins(seal["payload"]["manifest"]), [ref["expected_image"]])
        initial = bench.reference_sha(ref)
        for field, value in (("source_locator", "other region"), ("page", 1),
                             ("expected_image", {"path": str(image), "sha256": "f" * 64}),
                             ("original_sha256", "f" * 64)):
            changed = copy.deepcopy(ref); changed[field] = value
            self.assertNotEqual(initial, bench.reference_sha(changed))
        image.write_bytes(b"changed gold")
        with self.assertRaisesRegex(ValueError, "gold image SHA mismatch"):
            self.freeze()

    def test_image_gold_never_substring_or_automatic_pass(self):
        doc, ref, _ = self.image_ref()
        with self.assertRaisesRegex(ValueError, "independent visual"):
            bench.evaluate(ref, {"text": ref["expected"]})
        self.assertEqual(bench.semantic_outcome(None, doc, ref, {})[0], bench.NOT_MEASURED)

    def test_image_gold_runtime_mount_refused_and_request_blind(self):
        _, ref, image = self.image_ref()
        payload = self.freeze()["payload"]
        payload["config"]["sandbox"]["readonly"].append(dict(ref["expected_image"]))
        with patch.object(bench.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "runtime mount exposes"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
        seal = self.freeze()
        output = self.root / "collected"; output.mkdir()
        bench.collect(seal["payload"], seal["sha256"], output, self.fake_run)
        for request in output.glob("*/*/request.json"):
            raw = request.read_text()
            self.assertNotIn(str(image), raw)
            self.assertNotIn("expected_image", raw)
            self.assertNotIn("source_locator", raw)

    def test_image_gold_visual_review_requires_complete_region_and_all_bindings(self):
        doc, ref, image = self.image_ref()
        run = {"record": {"output_sha256": "b" * 64}}
        entry = {"status": "verified", "outcome": "PASS", "method": "independent_output_page_review",
                 "reviewer": "independent", "evidence_uri": "review.json", "output_locator": "whole markdown",
                 "original_sha256": doc["original"]["sha256"], "output_sha256": "b" * 64,
                 "reference_sha256": bench.reference_sha(ref), "source_page": 2,
                 "expected_image_sha256": ref["expected_image"]["sha256"],
                 "source_locator": ref["source_locator"], "full_declared_region_reviewed": True}
        self.assertEqual(bench.semantic_outcome(entry, doc, ref, run), ("PASS", None))
        for field, value in (("full_declared_region_reviewed", False), ("expected_image_sha256", "0" * 64),
                             ("source_locator", "subset only"), ("source_page", 1),
                             ("reference_sha256", "0" * 64), ("output_sha256", "0" * 64)):
            altered = dict(entry); altered[field] = value
            self.assertEqual(bench.semantic_outcome(altered, doc, ref, run)[0], bench.NOT_MEASURED)
        image.unlink()
        self.assertEqual(bench.semantic_outcome(entry, doc, ref, run)[0], bench.NOT_MEASURED)

    def fake_run(self, argv, directory, limits):
        text = "verified body" if directory.name == "docling" else "wrong body"
        bench.write_json(directory / "extraction.json", {"pages": [{"page": 1, "text": "intro A then B"},
                                                                    {"page": 2, "text": text + " A then B",
                                                                     "formulas": [text],
                                                                     "tables": [{"id": "T1", "cells": [{"row": 0, "column": 0, "text": text}]}],
                                                                     "sections": [{"title": "Methods", "text": text}]}],
                                                       "self_grade": "PASS"})
        (directory / "stdout.log").write_text("stdout retained", encoding="utf-8")
        (directory / "stderr.log").write_text("stderr retained", encoding="utf-8")
        return {"status": "OK", "error": None, "elapsed_seconds": 0.5,
                "cpu_seconds": 0.3, "peak_rss_bytes": 12345, "exit_code": 0}

    def collection(self, executor=None):
        seal = self.freeze()
        output = bench.create_output(self.root / "collection")
        collection_sha = bench.collect(seal["payload"], seal["sha256"], output, executor or self.fake_run)
        runs = bench.load_runs(output / "collection.json", collection_sha, seal["sha256"])
        return seal, output, collection_sha, runs

    def output_reviews(self, seal, runs):
        entries = []
        for doc in seal["payload"]["manifest"]["documents"]:
            physical = seal["payload"]["assignments"][doc["document_id"]]["physical_id"]
            for ref in doc["references"]:
                if ref["type"] in bench.QUALITY_CHECKS:
                    continue
                for engine in bench.ENGINES:
                    entries.append({"document_id": doc["document_id"], "check_id": ref["check_id"],
                                    "engine": engine, "status": "verified", "outcome": "PASS",
                                    "method": "independent_output_page_review", "reviewer": "independent mock reviewer",
                                    "evidence_uri": "mock-only/review", "source_locator": ref["locator"],
                                    "output_locator": "reviewed output span", "source_page": ref["page"],
                                    "original_sha256": doc["original"]["sha256"],
                                    "output_sha256": runs[(physical, engine)]["record"]["output_sha256"],
                                    "reference_sha256": bench.reference_sha(ref)})
        return {"schema": "dtox-independent-output-reviews-1", "seal_sha256": seal["sha256"], "reviews": entries}

    def reviewed_grade(self, seal, runs):
        return bench.grade(seal["payload"], runs, self.output_reviews(seal, runs))

    def test_seal_repeatable_and_manifest_order_independent_split(self):
        first = self.freeze()
        self.assertEqual(first, self.freeze())
        self.manifest["documents"].reverse()
        second = self.freeze()
        self.assertEqual(first["payload"]["assignments"], second["payload"]["assignments"])
        self.assertEqual(first["payload"]["counts"]["physical_split_counts"], {"development": 8, "holdout": 2})
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_exact_duplicate_rows_counted_not_extracted_twice(self):
        self.manifest["documents"].append(copy.deepcopy(self.manifest["documents"][0]))
        seal, _, _, runs = self.collection()
        self.assertEqual(seal["payload"]["counts"]["duplicate_id_rows"], 1)
        self.assertEqual(seal["payload"]["counts"]["manifest_rows"], 11)
        self.assertEqual(len(runs), 20)

    def test_stratified_100_controls_holdout_and_mixed_aliases_inseparable(self):
        self.manifest["documents"] = [self.document(i) for i in range(100)]
        for doc in self.manifest["documents"][:81]:
            doc["classes"].remove("healthy")
        alias = copy.deepcopy(self.manifest["documents"][80])
        alias["document_id"] = "alias:control80"
        alias["classes"].append("healthy")
        self.manifest["documents"].append(alias)
        self.config["gates"]["expected_physical_documents"] = 100
        payload = self.freeze()["payload"]
        assignments = payload["assignments"]
        self.assertEqual(assignments["paper:80"], assignments["alias:control80"])
        docs = {d["document_id"]: d for d in self.manifest["documents"]}
        controls = {assignments[k]["physical_id"] for k, d in docs.items() if "healthy" in d["classes"]}
        physical = {a["physical_id"]: a for a in assignments.values()}
        counts = {stratum: {split: sum(a["split"] == split and (pid in controls) == (stratum == "control")
                                      for pid, a in physical.items()) for split in ("development", "holdout")}
                  for stratum in ("control", "challenge")}
        self.assertEqual(counts, {"control": {"development": 16, "holdout": 4},
                                  "challenge": {"development": 64, "holdout": 16}})
        self.assertEqual(payload["counts"]["physical_split_counts"], {"development": 80, "holdout": 20})
        self.assertEqual(payload["counts"]["physical_alias_ids"], 1)
        self.assertIn("any healthy alias", payload["split_policy"])
        self.manifest["documents"].reverse()
        self.assertEqual(assignments, self.freeze()["payload"]["assignments"])

    def test_conflicting_duplicate_id_refused(self):
        duplicate = copy.deepcopy(self.manifest["documents"][0])
        duplicate["source_version"]["value"] = "v2"
        self.manifest["documents"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.freeze()

    def test_physical_aliases_share_split_and_one_pair_of_runs(self):
        alias = copy.deepcopy(self.manifest["documents"][0])
        alias["document_id"] = "alias:0"
        self.manifest["documents"].append(alias)
        seal, _, _, runs = self.collection()
        assignments = seal["payload"]["assignments"]
        self.assertEqual(assignments["paper:0"], assignments["alias:0"])
        self.assertEqual(seal["payload"]["counts"]["physical_alias_ids"], 1)
        self.assertEqual(len(runs), 20)
        report = bench.grade(seal["payload"], runs)
        overall = next(s for s in report["summaries"] if s["class"] == s["split"] == "all" and s["engine"] == "docling")
        self.assertEqual(overall["logical_documents"], 11)
        self.assertEqual(overall["physical_documents"], 10)
        self.assertEqual(overall["observed_reviewed_checks"]["denominator"], 20)
        self.assertEqual(overall["checks_declared"], 50)

    def test_missing_original_stays_in_denominator_and_no_replacement(self):
        doc = self.manifest["documents"][0]
        Path(doc["original"]["path"]).unlink()
        calls = []
        def execute(*args):
            calls.append(args[1])
            return self.fake_run(*args)
        seal, _, _, runs = self.collection(execute)
        self.assertEqual(len(calls), 18)
        self.assertEqual(len(runs), 20)
        report = bench.grade(seal["payload"], runs)
        failed = [r for r in report["documents"] if r["document_id"] == doc["document_id"]]
        self.assertTrue(all(r["status"] == bench.UNKNOWN for r in failed))
        self.assertEqual(report["counts"]["physical_documents"], 10)
        self.assertEqual(report["decision"], "NO_WINNER")

    def test_declared_unavailable_does_not_get_promoted_if_file_exists(self):
        self.manifest["documents"][0]["original"]["status"] = "unestablished"
        seal, _, _, runs = self.collection()
        self.assertEqual(len(runs), 20)
        self.assertIn("FROZEN_INPUT_UNAVAILABLE", {r["record"]["status"] for r in runs.values()})
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")

    def test_hash_mismatch_and_unreadable_are_explicit(self):
        doc = self.manifest["documents"][0]
        Path(doc["original"]["path"]).write_bytes(b"changed")
        self.assertEqual(self.freeze()["payload"]["input_states"][doc["document_id"]]["status"], "INPUT_SHA_MISMATCH")
        with patch.object(bench, "file_sha", side_effect=PermissionError("denied")):
            self.assertEqual(bench.input_state(doc)["status"], "INPUT_UNREADABLE")

    def test_input_changed_after_sealing_not_launched(self):
        seal = self.freeze()
        Path(self.manifest["documents"][0]["original"]["path"]).write_bytes(b"changed")
        output = bench.create_output(self.root / "collection")
        executor = unittest.mock.Mock(side_effect=self.fake_run)
        bench.collect(seal["payload"], seal["sha256"], output, executor)
        self.assertEqual(executor.call_count, 18)

    def test_changed_original_after_runner_invalidates_result(self):
        def execute(argv, directory, limits):
            result = self.fake_run(argv, directory, limits)
            original = argv[argv.index("/input/original") - 1]
            Path(original).write_bytes(b"host-side mutation simulated")
            return result
        seal, _, _, runs = self.collection(execute)
        self.assertIn("ORIGINAL_CHANGED", {r["record"]["status"] for r in runs.values()})
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")

    def test_alternating_engine_order_and_no_retry(self):
        calls = []
        def execute(argv, directory, limits):
            calls.append(directory.name)
            return {"status": "PROCESS_ERROR", "error": "mock exit 2", "exit_code": 2,
                    "elapsed_seconds": 0.1, "cpu_seconds": 0.1, "peak_rss_bytes": 100}
        _, _, _, runs = self.collection(execute)
        self.assertEqual(calls[:4], ["pymupdf", "docling", "docling", "pymupdf"])
        self.assertEqual(len(calls), 20)
        self.assertTrue(all(r["record"]["error"] == "mock exit 2" for r in runs.values()))

    def test_runner_exception_preserved_and_other_engine_continues(self):
        def execute(argv, directory, limits):
            if directory.name == "docling":
                raise RuntimeError("mock candidate error")
            return self.fake_run(argv, directory, limits)
        _, _, _, runs = self.collection(execute)
        self.assertEqual(sum(r["record"]["status"] == "RUNNER_EXCEPTION" for r in runs.values()), 10)

    def test_zero_exit_without_output_not_success(self):
        _, _, _, runs = self.collection(lambda *args: {"status": "OK", "exit_code": 0})
        self.assertTrue(all(r["record"]["status"] == "OUTPUT_MISSING" for r in runs.values()))

    def test_malformed_output_inconclusive_not_silently_dropped(self):
        def execute(argv, directory, limits):
            (directory / "extraction.json").write_text("not JSON", encoding="utf-8")
            return {"status": "OK", "exit_code": 0}
        seal, _, _, runs = self.collection(execute)
        self.assertTrue(all(r["output_error"] for r in runs.values()))
        self.assertTrue(all(r["status"] == bench.UNKNOWN for r in bench.grade(seal["payload"], runs)["documents"]))

    def test_first_page_only_cannot_pass_last_page_reference(self):
        def execute(argv, directory, limits):
            bench.write_json(directory / "extraction.json", {"pages": [{"page": 1, "text": "verified body"}]})
            return {"status": "OK", "exit_code": 0}
        seal, _, _, runs = self.collection(execute)
        report = bench.grade(seal["payload"], runs)
        self.assertTrue(all(not row["whole_document_observed"] for row in report["documents"]))
        self.assertEqual(report["decision"], "NO_WINNER")

    def test_missing_unreviewed_and_reader_consensus_refs_never_pass(self):
        for state in ("missing", "unreviewed", "reader_consensus"):
            with self.subTest(state=state):
                doc = self.manifest["documents"][0]
                review = doc["references"][0]["review"]
                if state == "reader_consensus":
                    review.update(status="verified", method="reader_consensus")
                else:
                    review["status"] = state
                self.assertFalse(bench.reviewed(doc["references"][0], doc))

    def test_empty_reference_set_and_missing_metadata_block_winner(self):
        self.manifest["documents"][0]["references"] = []
        self.manifest["documents"][1]["source_uri"] = {"status": "unestablished", "value": None}
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertTrue(any("references missing" in b for b in report["blockers"]))
        self.assertTrue(any("missing identity" in b for b in report["blockers"]))

    def test_reference_original_binding_required(self):
        doc = self.manifest["documents"][0]
        doc["references"][0]["review"]["original_sha256"] = "0" * 64
        self.assertFalse(bench.reviewed(doc["references"][0], doc))

    def test_self_grade_ignored_and_healthy_regression_blocks(self):
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        baseline = next(r for r in report["documents"] if r["engine"] == "pymupdf")
        self.assertEqual(baseline["checks"][0]["outcome"], "FAIL")
        key = next(key for key in runs if key[1] == "pymupdf")
        runs[key]["pages"][2]["text"] = "verified body"
        candidate_key = (key[0], "docling")
        runs[candidate_key]["pages"][2]["text"] = "wrong"
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["healthy_regressions"], 1)
        self.assertIn("healthy-control regression gate failed", report["blockers"])

    def test_complete_mock_gates_can_pass_but_never_authorize_publication(self):
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertFalse(report["publication_authorized"])
        self.assertFalse(report["backend_self_grades_used"])
        self.assertIn("NOT full-document precision/recall", report["quality_scope"])

    def test_holdout_class_missing_or_exception_blocks(self):
        assignments = self.freeze()["payload"]["assignments"]
        for doc in self.manifest["documents"]:
            if assignments[doc["document_id"]]["split"] == "holdout":
                doc["classes"].remove("healthy")
        self.manifest["documents"][0]["exceptions"] = ["candidate layout support not established"]
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertIn("holdout/healthy: insufficient physical class coverage", report["blockers"])

    def test_small_sample_gate_no_auto_fill(self):
        self.config["gates"]["expected_physical_documents"] = 100
        seal, _, _, runs = self.collection()
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")
        self.assertEqual(seal["payload"]["counts"]["physical_documents"], 10)

    def test_checks_strict_and_normalization_explicit(self):
        page = {"text": "First A, then B. Schrödinger verified body", "formulas": ["x^2 + y^2"],
                "tables": [{"id": "T1", "cells": [{"row": 0, "column": 1, "text": "42"}]}],
                "sections": [{"title": "Methods", "text": "verified body"}]}
        common = {"normalization": "exact"}
        cases = [{"type": "snippet", "expected": "verified body"},
                 {"type": "ordered_anchors", "expected": ["First A", "then B"]},
                 {"type": "formula", "expected": "x^2 + y^2"},
                 {"type": "table_cell", "table_id": "T1", "row": 0, "column": 1, "expected": "42"},
                 {"type": "section", "title": "Methods", "expected": "verified body"}]
        for case in cases:
            if case["type"] in bench.QUALITY_CHECKS:
                self.assertTrue(bench.evaluate({**common, **case}, page))
            else:
                with self.assertRaisesRegex(ValueError, "independent output/page review"):
                    bench.evaluate({**common, **case}, page)
        self.assertFalse(bench.evaluate({**common, "type": "ordered_anchors", "expected": ["then B", "First A"]}, page))
        self.assertTrue(bench.evaluate({"type": "snippet", "expected": "verified\nbody", "normalization": "nfc_whitespace"}, page))

    def test_seal_and_config_tampering_refused(self):
        seal = self.freeze()
        path = self.root / "seal.json"
        bench.write_json(path, seal)
        self.assertEqual(bench.load_seal(path, seal["sha256"])["counts"]["physical_documents"], 10)
        seal["payload"]["config"]["gates"]["min_candidate_check_gain"] = 100
        path.write_bytes(bench.encoded(seal))
        with self.assertRaisesRegex(ValueError, "seal SHA mismatch"):
            bench.load_seal(path, seal["sha256"])

    def test_result_collection_and_output_tampering_refused_or_unknown(self):
        seal, output, collection_sha, runs = self.collection()
        run = next(iter(runs.values()))
        physical_id, engine = run["record"]["physical_id"], run["record"]["engine"]
        path = output / physical_id / engine
        (path / "extraction.json").write_bytes(b"tampered")
        loaded = bench.load_runs(output / "collection.json", collection_sha, seal["sha256"])
        self.assertIn("SHA mismatch", loaded[(physical_id, engine)]["output_error"])
        (path / "result.json").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "result SHA mismatch"):
            bench.load_runs(output / "collection.json", collection_sha, seal["sha256"])
        with self.assertRaisesRegex(ValueError, "collection SHA mismatch"):
            bench.load_runs(output / "collection.json", "0" * 64, seal["sha256"])

    def test_missing_runs_preserved_as_unknown_foreign_runs_refused(self):
        seal, _, _, runs = self.collection()
        runs.pop(next(iter(runs)))
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")
        runs[("foreign", "docling")] = {}
        with self.assertRaisesRegex(ValueError, "foreign collection"):
            bench.grade(seal["payload"], runs)

    def test_shell_retry_default_timer_and_third_engine_refused(self):
        for mutate in (lambda c: c["engines"]["docling"]["argv"].__setitem__(0, "/bin/bash"),
                       lambda c: c.__setitem__("automatic_retry", True),
                       lambda c: c.__setitem__("job_runtime", 60),
                       lambda c: c["document_limits"].pop("wall_seconds"),
                       lambda c: c["engines"].__setitem__("third", {})):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(ValueError):
                bench.validate_config(config)

    def test_sandbox_command_single_readonly_input_no_network_or_references(self):
        argv = bench.sandbox_argv(self.config, "pymupdf", "/original.pdf", "/result")
        self.assertIn("--unshare-all", argv)
        self.assertIn("--clearenv", argv)
        self.assertIn("--ro-bind", argv)
        self.assertIn("/input/original", argv)
        self.assertNotIn("--share-net", argv)
        self.assertNotIn("references", " ".join(argv))
        self.assertEqual(argv[-4:], ["/input/original", "/output/extraction.json", "/runtime/pymupdf.json", "/output/request.json"])

    def test_runtime_mount_cannot_expose_manifest_or_original(self):
        payload = self.freeze()["payload"]
        payload["config"]["sandbox"]["readonly"] = [{"path": str(self.root), "kind": "tree", "sha256": "a" * 64}]
        with patch.object(bench.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "runtime mount exposes"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")

    def test_windows_collection_refused_without_launch(self):
        with patch.object(bench.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "Linux bubblewrap"):
                bench.execution_preflight(self.freeze()["payload"], self.root / "seal.json", self.root / "output")

    def test_exclusive_output_and_original_overlap_refused(self):
        output = bench.create_output(self.root / "output")
        self.assertTrue((output / "LOCK").is_file())
        with self.assertRaises(FileExistsError):
            bench.create_output(output)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            bench.create_output(self.originals / "output", [self.originals])

    def test_duplicate_json_keys_refused(self):
        path = self.root / "duplicate.json"
        path.write_text('{"a":1,"a":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            bench.read_json(path)

    def test_document_mode_preserves_current_whole_markdown_not_fake_page_mapping(self):
        for engine in self.config["engines"].values():
            engine["output_scope"] = "document"
        def execute(argv, directory, limits):
            result = self.fake_run(argv, directory, limits)
            path = directory / "extraction.json"
            value = bench.read_json(path)
            path.write_bytes(bench.encoded({"document": value["pages"][1],
                                          "coverage": {"whole_document": True, "page_count": 2}}))
            return result
        seal, _, _, runs = self.collection(execute)
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertTrue(all(r["output_locator_scope"] == "document" for r in report["documents"]))

    def test_output_scope_cannot_be_changed_by_backend(self):
        for engine in self.config["engines"].values():
            engine["output_scope"] = "document"
        seal, _, _, runs = self.collection()
        self.assertEqual(bench.grade(seal["payload"], runs)["measurement_status"], bench.UNKNOWN)

    def test_pilot_control_label_without_independent_health_review_is_unknown(self):
        self.manifest["documents"][0].pop("healthy_control_review")
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertEqual(report["measurement_status"], bench.UNKNOWN)

    def test_class_checks_cannot_be_replaced_by_only_easy_snippets(self):
        self.manifest["documents"][0]["references"] = self.manifest["documents"][0]["references"][:1]
        seal, _, _, runs = self.collection()
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")

    def test_alias_additional_failed_reference_not_lost_in_physical_dedup(self):
        alias = copy.deepcopy(self.manifest["documents"][0])
        alias["document_id"] = "zz-alias:0"
        reference = copy.deepcopy(alias["references"][0])
        reference.update(check_id="additional", expected="not extracted")
        alias["references"].append(reference)
        self.manifest["documents"].append(alias)
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        overall = next(s for s in report["summaries"] if s["class"] == s["split"] == "all" and s["engine"] == "docling")
        self.assertEqual(overall["observed_reviewed_checks"]["denominator"], 21)
        self.assertEqual(overall["document_pass"]["numerator"], 9)
        self.assertEqual(report["decision"], "NO_WINNER")

    def test_upstream_unpinned_or_monkeypatched_current_baseline_refused(self):
        for key, value in (("comparison_kind", "upstream"), ("source_pins", []), ("runtime_monkeypatch", True)):
            config = copy.deepcopy(self.config)
            config["baseline_contract"][key] = value
            with self.assertRaisesRegex(ValueError, "baseline"):
                bench.validate_config(config)

    def test_parent_traversal_normalized_and_result_escape_refused(self):
        path = self.root / "nested" / ".." / "outside.json"
        self.assertEqual(bench.local(path), self.root / "outside.json")
        seal, output, _, runs = self.collection()
        outside = self.root / "outside.json"
        bench.write_json(outside, next(iter(runs.values()))["record"])
        index = bench.read_json(output / "collection.json")
        index["runs"][0]["record"] = "../outside.json"
        index["runs"][0]["sha256"] = bench.file_sha(outside)
        (output / "collection.json").write_bytes(bench.encoded(index))
        with self.assertRaisesRegex(ValueError, "outside collection"):
            bench.load_runs(output / "collection.json", bench.file_sha(output / "collection.json"), seal["sha256"])

    def test_explicit_wall_cpu_as_limits_and_wait4_metrics_mocked_only(self):
        directory = bench.create_output(self.root / "runner")
        limits = dict(label="explicit-test-limits", wall_seconds=0.1, cpu_seconds=7, address_space_bytes=1000000)
        resource = SimpleNamespace(RLIMIT_CPU=1, RLIMIT_AS=2, setrlimit=unittest.mock.Mock())
        process = SimpleNamespace(pid=123, returncode=None, wait=unittest.mock.Mock())
        def launch(*args, **kwargs):
            self.assertFalse(kwargs["shell"])
            kwargs["preexec_fn"]()
            return process
        usage = SimpleNamespace(ru_utime=1.25, ru_stime=0.75, ru_maxrss=42)
        with patch.dict("sys.modules", {"resource": resource}), \
             patch.object(bench.subprocess, "Popen", side_effect=launch) as popen, \
             patch.object(bench.os, "wait4", create=True, side_effect=[(0, 0, None), (123, 0, usage)]), \
             patch.object(bench.os, "WNOHANG", 1, create=True), \
             patch.object(bench.os, "waitstatus_to_exitcode", return_value=0, create=True), \
             patch.object(bench.os, "killpg", create=True) as killpg, \
             patch.object(bench.signal, "SIGKILL", 9, create=True), \
             patch.object(bench.time, "monotonic", side_effect=[0, 0.2, 0.3]), \
             patch.object(bench.time, "sleep"):
            result = bench.run_command(["/mock/never-launched"], directory, limits)
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(killpg.called)
        self.assertEqual(result["status"], "DECLARED_WALL_LIMIT")
        self.assertEqual(result["cpu_seconds"], 2)
        self.assertEqual(result["peak_rss_bytes"], 42 * 1024)
        resource.setrlimit.assert_any_call(1, (7, 7))
        resource.setrlimit.assert_any_call(2, (1000000, 1000000))

    def test_request_exposes_no_references_split_or_class_labels(self):
        _, output, _, _ = self.collection()
        request = bench.read_json(next(output.glob("*/*/request.json")))
        self.assertNotIn("references", request)
        self.assertNotIn("classes", request)
        self.assertNotIn("split", request)
        self.assertEqual(request["baseline_internal_timeout_seconds"], 120)

    def test_future_hardware_paths_and_configured_source_shas_not_hardcoded(self):
        self.config["environment"].update(architecture="x86_64", cpu_count=8, ram_bytes=32 * 1024 ** 3,
                                          venv="/future/eval-env", python_executable="/future/eval-env/bin/python")
        self.config["baseline_contract"]["source_pins"][0]["sha256"] = "0" * 64
        self.config["engines"]["pymupdf"]["pins"][1]["sha256"] = "0" * 64
        bench.validate_config(self.config)

    def test_os_identity_receipt_and_venv_symlink_target_without_whole_usr_hash(self):
        receipt_pin = {"path": "/receipts/os.json", "sha256": "9" * 64}
        self.config["sandbox"]["os_readonly"] = [{"path": "/usr", "identity_receipt": receipt_pin}]
        payload = self.freeze()["payload"]
        receipt = {"schema": "dtox-os-runtime-identity-1", "architecture": "aarch64", "mounts": ["/usr"],
                   "identity_files": [{"path": "/identity/dpkg-status", "sha256": "8" * 64}]}
        def resolve(path, *args, **kwargs):
            return Path("/usr/bin/python").absolute() if path == Path("/runtime/python").absolute() else path.absolute()
        with patch.object(bench.sys, "platform", "linux"), patch.object(bench, "verify_pin") as verify, \
             patch.object(bench, "read_json", return_value=receipt), patch.object(bench.Path, "resolve", resolve), \
             patch.object(bench, "environment_snapshot", return_value=self.config["environment"]):
            actual = bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
        self.assertEqual(actual, self.config["environment"])
        self.assertNotIn("/usr", [c.args[0]["path"] for c in verify.call_args_list])
        self.assertIn(receipt_pin, [c.args[0] for c in verify.call_args_list])

    def test_actual_environment_mismatch_and_production_runtime_mount_refused(self):
        payload = self.freeze()["payload"]
        with patch.object(bench.sys, "platform", "linux"), patch.object(bench, "verify_pin"), \
             patch.object(bench, "environment_snapshot", return_value={"architecture": "wrong"}):
            with self.assertRaisesRegex(ValueError, "actual environment differs"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
        payload["config"]["sandbox"]["readonly"].append({"path": str(self.root / "production"), "sha256": "a" * 64})
        with patch.object(bench.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "production data"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")

    def test_unknown_edition_same_sealed_bytes_evaluable_without_historical_claim(self):
        for doc in self.manifest["documents"]:
            doc["source_version"] = {"status": "unestablished", "value": None}
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["measurement_status"], "MEASURED_OBSERVED_CHECKS")
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertTrue(all(r["source_version"]["status"] == "unestablished" and
                            not r["historical_corpus_version_identity_proven"] for r in report["documents"]))

    def test_class_specific_winner_does_not_require_baseline_or_other_class_to_pass(self):
        # Same content classes overlap intentionally; one cohort is layout-only.
        assignments = self.freeze()["payload"]["assignments"]
        for split in ("development", "holdout"):
            doc = next(d for d in self.manifest["documents"] if assignments[d["document_id"]]["split"] == split)
            doc["classes"] = ["layout"]
        seal, _, _, runs = self.collection()
        affected = {seal["payload"]["assignments"][d["document_id"]]["physical_id"] for d in self.manifest["documents"] if d["classes"] == ["layout"]}
        for key, run in runs.items():
            if key[0] in affected and key[1] == "docling":
                run["pages"][2]["text"] = "bad layout"
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "CLASS_SPECIFIC_CANDIDATES")
        self.assertNotIn("layout", report["eligible_classes"])
        self.assertIn("encoding", report["eligible_classes"])
        self.assertTrue(any(r["engine"] == "pymupdf" and r["status"] == "FAIL" for r in report["documents"]))

    def test_native_schema_availability_never_creates_semantic_failure_or_gain(self):
        seal, _, _, runs = self.collection()
        for (physical, engine), run in runs.items():
            run["pages"][2]["text"] = "verified body A then B"
            if engine == "pymupdf":
                for field in ("formulas", "tables", "sections"):
                    run["pages"][2][field] = []
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["candidate_paired_check_gain"], 0)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertTrue(all(row["semantic_failures"] == 0 for row in report["documents"]))
        self.assertTrue(all(check["outcome"] == bench.NOT_MEASURED for row in report["documents"]
                            for check in row["checks"] if check["type"] not in bench.QUALITY_CHECKS))
        base = next(row for row in report["documents"] if row["engine"] == "pymupdf")
        candidate = next(row for row in report["documents"] if row["engine"] == "docling")
        self.assertFalse(base["capability"]["formulas"]["records_present"])
        self.assertTrue(candidate["capability"]["formulas"]["records_present"])
        reviewed = self.reviewed_grade(seal, runs)
        self.assertEqual(reviewed["candidate_paired_check_gain"], 0)
        self.assertTrue(all(row["status"] == "PASS" for row in reviewed["documents"]))
        self.assertEqual(reviewed["decision"], "NO_WINNER")

    def test_declared_semantic_denominator_preserved_when_unmeasured(self):
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        overall = next(s for s in report["summaries"] if s["class"] == s["split"] == "all" and s["engine"] == "docling")
        self.assertEqual(overall["quality_checks"]["declared"], 20)
        self.assertEqual(overall["semantic_checks"]["declared"], 30)
        self.assertEqual(overall["semantic_checks"]["missing"], 30)
        self.assertEqual(overall["semantic_checks"]["passed_over_declared"], bench.ratio(0, 30))
        self.assertEqual(overall["semantic_checks"]["observed_reviewed_checks"], bench.ratio(0, 0))
        self.assertEqual(overall["checks_declared"], 50)
        self.assertEqual(overall["checks_unknown"], 30)
        self.assertEqual(report["candidate_paired_check_gain"], 10)
        self.assertEqual(report["decision"], "NO_WINNER")

    def all_pass_run(self, argv, directory, limits):
        result = self.fake_run(argv, directory, limits)
        output = directory / "extraction.json"
        output.write_text(output.read_text(encoding="utf-8").replace("wrong body", "verified body"), encoding="utf-8")
        return result

    def test_independent_semantic_improvement_counts_in_paired_global_and_class_gain(self):
        seal, _, _, runs = self.collection(self.all_pass_run)
        bundle = self.output_reviews(seal, runs)
        for entry in bundle["reviews"]:
            if entry["engine"] == "pymupdf" and entry["check_id"] == "formula":
                entry["outcome"] = "FAIL"
        report = bench.grade(seal["payload"], runs, bundle)
        self.assertEqual(report["candidate_paired_check_gain"], 10)
        self.assertEqual(next(c for c in report["class_decisions"] if c["class"] == "math")["candidate_check_gain"], 10)
        self.assertEqual(report["healthy_regressions"], 0)

    def test_healthy_all_pass_no_improvement_accepts_controls_not_repair_route(self):
        self.config["gates"]["required_classes"] = ["encoding", "layout", "math", "tables"]
        seal, _, _, runs = self.collection(self.all_pass_run)
        report = self.reviewed_grade(seal, runs)
        control = next(c for c in report["class_decisions"] if c["class"] == "healthy")
        self.assertEqual(control["candidate_check_gain"], 0)
        self.assertEqual(report["healthy_gate_status"], "PASS")
        self.assertEqual(report["healthy_regressions"], 0)
        self.assertNotIn("predeclared class improvement/no-regression gate failed", control["blockers"])
        self.assertTrue(all(row["status"] == "PASS" for row in report["documents"]))
        self.assertEqual(report["required_route_status"], "NOT_ACCEPTED")

    def test_independent_semantic_failure_blocks_candidate_not_inflates_quality_gain(self):
        seal, _, _, runs = self.collection()
        bundle = self.output_reviews(seal, runs)
        for entry in bundle["reviews"]:
            if entry["engine"] == "docling" and entry["check_id"] == "formula":
                entry["outcome"] = "FAIL"
        report = bench.grade(seal["payload"], runs, bundle)
        self.assertEqual(report["candidate_paired_check_gain"], 0)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertTrue(any(row["semantic_failures"] for row in report["documents"]))
        self.assertTrue(all(row["semantic_checks"]["missing"] == 0 for row in report["documents"]))

    def test_missing_holdout_semantic_review_keeps_control_gate_closed(self):
        seal, _, _, runs = self.collection()
        bundle = self.output_reviews(seal, runs)
        holdout = next(identity for identity, a in seal["payload"]["assignments"].items() if a["split"] == "holdout")
        entry = next(e for e in bundle["reviews"] if e["document_id"] == holdout and e["engine"] == "docling")
        bundle["reviews"].remove(entry)
        report = bench.grade(seal["payload"], runs, bundle)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertTrue(all("independent healthy-control/holdout gate failed" in c["blockers"] for c in report["class_decisions"]))

    def test_semantic_receipts_bind_exact_source_reference_output_and_method(self):
        seal, _, _, runs = self.collection()
        for field, value in (("output_sha256", "0" * 64), ("original_sha256", "0" * 64),
                             ("reference_sha256", "0" * 64), ("method", "reader_consensus"),
                             ("method", "backend_self_grade"), ("source_page", 1),
                             ("output_locator", ""), ("reviewer", "")):
            with self.subTest(field=field, value=value):
                bundle = self.output_reviews(seal, runs)
                bundle["reviews"][0][field] = value
                report = bench.grade(seal["payload"], runs, bundle)
                self.assertEqual(report["decision"], "NO_WINNER")
                self.assertTrue(any(c["outcome"] == bench.NOT_MEASURED and c["error"]
                                    for row in report["documents"] for c in row["checks"]))

    def test_unreviewed_or_explicit_missing_semantic_receipt_not_pass(self):
        seal, _, _, runs = self.collection()
        for state in ("missing", "unreviewed"):
            bundle = self.output_reviews(seal, runs)
            bundle["reviews"][0].update(status=state, outcome=bench.NOT_MEASURED)
            report = bench.grade(seal["payload"], runs, bundle)
            self.assertEqual(report["decision"], "NO_WINNER")
            self.assertTrue(any(row["semantic_checks"]["missing"] for row in report["documents"]))

    def test_duplicate_foreign_or_wrong_seal_semantic_receipts_rejected(self):
        seal, _, _, runs = self.collection()
        bundle = self.output_reviews(seal, runs)
        for mutation in (lambda b: b["reviews"].append(copy.deepcopy(b["reviews"][0])),
                         lambda b: b.__setitem__("seal_sha256", "0" * 64),
                         lambda b: b["reviews"][0].__setitem__("document_id", "foreign")):
            changed = copy.deepcopy(bundle)
            mutation(changed)
            with self.assertRaises(ValueError):
                bench.grade(seal["payload"], runs, changed)

    def test_missing_required_semantic_reference_not_replaced_by_good_text(self):
        for doc in self.manifest["documents"]:
            doc["references"] = [ref for ref in doc["references"] if ref["type"] in bench.QUALITY_CHECKS]
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertTrue(all(row["missing_semantic_required_types"] == ["formula", "section", "table_cell"]
                            for row in report["documents"]))

    def test_empty_backend_structure_can_pass_only_after_independent_review(self):
        seal, _, _, runs = self.collection()
        for run in runs.values():
            for page in run["pages"].values():
                for field in ("formulas", "tables", "sections"):
                    page[field] = []
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertEqual(report["candidate_paired_check_gain"], 10)
        self.assertTrue(all(row["semantic_checks"]["missing"] == 0 for row in report["documents"]))

    def test_resource_report_keeps_unseparated_hash_cost_explicit(self):
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        scope = report["cost_scope"]
        self.assertFalse(scope["inference_only_metrics_available"])
        self.assertTrue(scope["asset_hashing"]["included_in_per_document_metrics"])
        self.assertFalse(scope["asset_hashing"]["separately_measured"])
        self.assertIsNone(scope["asset_hashing"]["elapsed_seconds"])
        self.assertEqual(scope["asset_hashing"]["status"], bench.NOT_MEASURED)
        self.assertTrue(all(row["resources"]["elapsed_seconds"] == 0.5 for row in report["documents"]))

    def test_collection_records_outer_preflight_cost_separately(self):
        seal = self.freeze()
        output = bench.create_output(self.root / "collection")
        costs = {"scope": "mock preflight", "elapsed_seconds": 4, "cpu_seconds": 2}
        bench.collect(seal["payload"], seal["sha256"], output, self.fake_run, preflight_costs=costs)
        collection = bench.read_json(output / "collection.json")
        self.assertEqual(collection["preflight_costs"], costs)

    def test_grade_cli_hash_binds_independent_reviews_and_reports_cost_scope(self):
        seal, output, collection_sha, runs = self.collection()
        seal_path, reviews_path = self.root / "seal.json", self.root / "reviews.json"
        bench.write_json(seal_path, seal)
        bench.write_json(reviews_path, self.output_reviews(seal, runs))
        argv = ["grade", "--seal", str(seal_path), "--seal-sha256", seal["sha256"],
                "--collection", str(output / "collection.json"), "--collection-sha256", collection_sha,
                "--output", str(self.root / "grade"), "--semantic-reviews", str(reviews_path),
                "--semantic-reviews-sha256", bench.file_sha(reviews_path)]
        with patch("builtins.print"):
            bench.main(argv)
        report = bench.read_json(self.root / "grade" / "quality.json")
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertEqual(report["semantic_review_file_sha256"], bench.file_sha(reviews_path))
        self.assertIn("outer_preflight", report["cost_scope"])
        argv[-1] = "0" * 64
        with self.assertRaisesRegex(ValueError, "review file SHA mismatch"):
            bench.main(argv)

    def test_fixed_parser_identity_config_is_opt_in_and_never_root(self):
        bench.validate_config(self.config)
        for identity in ({"mode": "root_setup_fixed_uid", "uid": 0, "gid": 0},
                         {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 0},
                         {"mode": "root_setup_fixed_uid"}, {"mode": "privileged"}):
            self.config["sandbox"]["execution_identity"] = identity
            with self.assertRaisesRegex(ValueError, "execution identity"):
                bench.validate_config(self.config)
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        bench.validate_config(self.config)

    def test_collector_identity_root_only_for_explicit_fixed_uid_mode(self):
        with patch.object(bench.os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(ValueError, "collector must be root only"):
                bench.check_collector_identity(self.config)
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        with patch.object(bench.os, "geteuid", return_value=1000, create=True):
            with self.assertRaisesRegex(ValueError, "collector must be root only"):
                bench.collect(self.freeze()["payload"], "0" * 64, self.root / "absent")
        with patch.object(bench.os, "geteuid", return_value=0, create=True):
            bench.check_collector_identity(self.config)

    def test_fixed_uid_bwrap_argv_drops_all_caps_keeps_all_namespaces(self):
        default = bench.sandbox_argv(self.config, "pymupdf", "/original.pdf", "/result")
        self.assertNotIn("--uid", default)
        self.assertEqual(default[default.index("--cap-drop") + 1], "ALL")
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        argv = bench.sandbox_argv(self.config, "pymupdf", "/original.pdf", "/result")
        self.assertNotIn("--uid", argv)
        self.assertNotIn("--gid", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertLess(argv.index("--cap-drop"), argv.index("--"))
        self.assertNotIn("--unshare-all", argv)
        self.assertNotIn("--unshare-user", argv)
        for namespace in ("ipc", "pid", "net", "uts", "cgroup"):
            self.assertIn("--unshare-" + namespace, argv)
        self.assertEqual([argv[i + 1] for i, value in enumerate(argv) if value == "--cap-add"],
                         ["CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP"])
        self.assertEqual(argv[argv.index("--chdir") + 1], "/")
        helper = argv.index("/usr/bin/setpriv")
        self.assertEqual(argv[helper:helper + 9], ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534",
                         "--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                         "--no-new-privs", "--"])
        self.assertIn("--unshare-all", default)
        self.assertNotIn("--cap-add", default)
        self.assertNotIn("/usr/bin/setpriv", default)
        self.assertNotIn("--share-net", argv)

    def test_root_argv_creates_synthetic_mount_parents_before_mounts(self):
        self.config["sandbox"]["os_readonly"] = [
            {"path": "/usr"}, {"path": "/etc/ld.so.cache"}, {"path": "/etc/fonts"}]
        self.config["sandbox"]["readonly"] = [
            {"path": "/opt/eval/assets/models"}, {"path": "/opt/eval/venv"},
            {"path": "/usr/lib/library"}, {"path": "/opt/eval/assets/shared"},
            {"path": "/opt/eval/assets/shared/nested/file"}]
        default = bench.sandbox_argv(self.config, "docling", "/original.pdf", "/result")
        self.assertNotIn("--perms", default)
        self.assertEqual([default[i + 1] for i, arg in enumerate(default) if arg == "--dir"], ["/input"])
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        with patch.object(bench.os, "chmod") as chmod, patch.object(bench.os, "umask") as umask:
            argv = bench.sandbox_argv(self.config, "docling", "/original.pdf", "/result")
        chmod.assert_not_called()
        umask.assert_not_called()
        expected = ["/etc", "/opt", "/opt/eval", "/opt/eval/assets"]
        positions = [i for i, arg in enumerate(argv) if arg == "--perms"]
        self.assertEqual([argv[i:i + 4] for i in positions],
                         [["--perms", "0755", "--dir", parent] for parent in expected] +
                         [["--perms", "1777", "--tmpfs", "/tmp"], ["--perms", "0755", "--dir", "/input"]])
        self.assertTrue(all(i + 3 < argv.index("--ro-bind") for i in positions[:len(expected)]))
        input_bind = next(i for i, arg in enumerate(argv) if arg == "--ro-bind" and argv[i + 1] == "/original.pdf")
        self.assertTrue(all(i + 3 < input_bind for i in positions[-2:]))
        self.assertEqual(default[default.index("--tmpfs"):default.index("--tmpfs") + 4],
                         ["--tmpfs", "/tmp", "--dir", "/input"])
        self.assertEqual([argv[i + 1] for i, arg in enumerate(argv) if arg == "--dir"], expected + ["/input"])
        self.assertEqual(argv[argv.index("--bind"):argv.index("--bind") + 3], ["--bind", "/result", "/output"])
        self.config["sandbox"].pop("execution_identity")
        self.assertEqual(bench.sandbox_argv(self.config, "docling", "/original.pdf", "/result"), default)

    def test_root_helper_requires_explicit_setpriv_sha_pin(self):
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        for helper in ({}, {"path": "/usr/bin/setpriv", "sha256": "bad"},
                       {"path": "/runtime/other", "sha256": "9" * 64}):
            self.config["sandbox"]["setpriv"] = helper
            with self.assertRaisesRegex(ValueError, "pinned /usr/bin/setpriv"):
                bench.validate_config(self.config)

    def test_root_preflight_verifies_helper_pin_mount_and_backend_identity_requirement(self):
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        self.config["sandbox"]["readonly"].append({"path": "/usr", "kind": "tree", "sha256": "7" * 64})
        payload = self.freeze()["payload"]
        with patch.object(bench.sys, "platform", "linux"), \
                patch.object(bench.os, "geteuid", return_value=0, create=True), \
                patch.object(bench, "verify_pin") as verified, \
                patch.object(bench, "read_json", return_value={"execution_identity_uid": 65534}) as read, \
                patch.object(bench, "environment_snapshot", return_value=self.config["environment"]):
            bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
            self.assertIn(self.config["sandbox"]["setpriv"], [call.args[0] for call in verified.call_args_list])
            self.assertEqual(read.call_count, 2)
            read.return_value = {}
            with self.assertRaisesRegex(ValueError, "backend execution_identity_uid"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
            read.return_value = {"execution_identity_uid": 65534}
            verified.side_effect = lambda pin: bench.need(pin != self.config["sandbox"]["setpriv"], "helper SHA mismatch")
            with self.assertRaisesRegex(ValueError, "helper SHA mismatch"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")
            payload["config"]["sandbox"]["readonly"].pop()
            with self.assertRaisesRegex(ValueError, "setpriv outside readonly"):
                bench.execution_preflight(payload, self.root / "seal.json", self.root / "output")

    def test_fixed_uid_request_readable_under_private_umask_without_chown(self):
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        self.manifest["documents"] = self.manifest["documents"][:1]
        observed = []
        creator_uid = os.getuid() if hasattr(os, "getuid") else 0
        def execute(argv, directory, limits):
            path = directory / "request.json"
            expected = {"document_id": "paper:0", "original_sha256": self.manifest["documents"][0]["original"]["sha256"],
                        "source_uri": self.manifest["documents"][0]["source_uri"],
                        "source_version": self.manifest["documents"][0]["source_version"],
                        "source_kind": "other_pdf", "whole_document": True, "baseline_internal_timeout_seconds": 120}
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(path.stat().st_uid, creator_uid)
            self.assertEqual(path.read_bytes(), bench.encoded(expected))
            self.assertFalse(any(Path(call.args[0]) == path for call in chown.call_args_list))
            observed.append(path)
            return self.fake_run(argv, directory, limits)
        previous = os.umask(0o077)
        try:
            with patch.object(bench.os, "geteuid", return_value=0, create=True), \
                 patch.object(bench.os, "chown", create=True) as chown, \
                 patch.object(bench, "private_root_directory"):
                self.collection(execute)
        finally:
            os.umask(previous)
        self.assertEqual(len(observed), 2)

    def test_fixed_uid_chowns_only_new_run_roots_and_reclaims_before_reads(self):
        self.config["sandbox"]["execution_identity"] = {"mode": "root_setup_fixed_uid", "uid": 65534, "gid": 65534}
        calls = []
        def ownership(path, uid, gid, **kwargs):
            calls.append((Path(path), uid, gid, kwargs))
        def execute(argv, directory, limits):
            self.assertEqual(calls[-1][:3], (directory, 65534, 65534))
            return self.fake_run(argv, directory, limits)
        with patch.object(bench.os, "geteuid", return_value=0, create=True), \
                patch.object(bench.os, "chown", side_effect=ownership, create=True), \
                patch.object(bench.os, "chmod") as chmod, \
                patch.object(bench, "private_root_directory") as private:
            seal, output, _sha, runs = self.collection(execute)
        private.assert_called_once_with(output)
        self.assertEqual(len(calls), 80)
        for path, uid, gid, kwargs in calls:
            self.assertTrue(path.is_relative_to(output))
            self.assertIn(path.name, (*bench.ENGINES, "home", "tmp"))
            self.assertEqual((uid, gid), (0, 0) if kwargs else (65534, 65534))
        self.assertEqual(chmod.call_count, 40)
        self.assertEqual(len(runs), 20)

    def test_private_outer_root_required(self):
        path = unittest.mock.Mock()
        for uid, mode in ((0, 0o700), (65534, 0o700), (0, 0o755)):
            path.stat.return_value = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)
            with patch.object(bench, "local", return_value=path):
                if uid == 0 and mode == 0o700:
                    bench.private_root_directory(self.root)
                else:
                    with self.assertRaisesRegex(ValueError, "root-owned/private"):
                        bench.private_root_directory(self.root)

    def test_safe_reads_reject_nonregular_reparse_and_symlink_before_open(self):
        path = self.root / "unsafe.json"
        path.write_text("{}", encoding="utf-8")
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == path), \
                patch.object(bench.os, "open") as opened:
            with self.assertRaisesRegex(ValueError, "linked path"):
                bench.read_json(path)
            opened.assert_not_called()
        for info in (SimpleNamespace(st_mode=stat.S_IFIFO, st_nlink=1),
                     SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_file_attributes=0x400)):
            with patch.object(bench, "local", return_value=path), \
                    patch.object(Path, "lstat", return_value=info), \
                    patch.object(bench.os, "open") as opened:
                with self.assertRaises(ValueError):
                    bench.read_json(path)
                opened.assert_not_called()

    def test_safe_reads_reject_size_and_hardlinks_before_content_read(self):
        path = self.root / "bounded.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "read bound"):
            bench.read_json(path, 1)
        alias = self.root / "linked.json"
        os.link(path, alias)
        with self.assertRaisesRegex(ValueError, "multiply-linked"):
            bench.read_json(path)

    def test_parser_output_bounds_fail_closed_preserve_all_runs(self):
        self.config["sandbox"]["output_read_limits"] = dict(bench.OUTPUT_READ_LIMITS, log_bytes=2)
        seal, _, _, runs = self.collection()
        self.assertEqual(len(runs), 20)
        self.assertTrue(all(run["record"]["status"] == "INVALID_OUTPUT" for run in runs.values()))
        self.assertTrue(all(run["record"]["output_sha256"] is None for run in runs.values()))
        self.assertEqual(bench.grade(seal["payload"], runs)["decision"], "NO_WINNER")

    def test_undeclared_nested_symlink_artifact_rejected_in_postconditions(self):
        directory = self.root / "artifacts"
        directory.mkdir()
        bad = directory / "secret-link"
        bad.write_text("fixture", encoding="utf-8")
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == bad):
            with self.assertRaisesRegex(ValueError, "linked path"):
                bench.output_inventory(directory, bench.OUTPUT_READ_LIMITS)

    def test_output_read_limits_must_be_positive_and_complete(self):
        for bounds in ({"json_bytes": 12}, dict(bench.OUTPUT_READ_LIMITS, json_bytes=0),
                       dict(bench.OUTPUT_READ_LIMITS, log_bytes=True)):
            self.config["sandbox"]["output_read_limits"] = bounds
            with self.assertRaisesRegex(ValueError, "bounded output read"):
                bench.validate_config(self.config)

    def test_sampled_body_pages_controls_suffice_without_whole_document_claim(self):
        for doc in self.manifest["documents"]:
            doc["healthy_control_review"]["method"] = "independent_sampled_source_pages_review"
            self.assertTrue(bench.healthy_reviewed(doc))
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "DOCLING_PASSES_PREDECLARED_GATES")
        self.assertEqual(report["control_scope"], "checked passages/pages NOT wholepaper healthy")
        self.assertTrue(all(row["control_body_pages"] == [1, 2] for row in report["documents"]))
        self.assertTrue(all(not row["completeness"]["text_completeness_proven"] for row in report["documents"]))

    def test_sampled_controls_require_two_reviewed_body_pages_not_titles(self):
        doc = self.manifest["documents"][0]
        doc["healthy_control_review"]["method"] = "independent_sampled_source_pages_review"
        for update in ({"page": 2}, {"content_scope": "title"}, {"content_scope": "other"},
                       {"review": {"status": "unreviewed"}}):
            changed = copy.deepcopy(doc)
            changed["references"][1].update(update)
            self.assertFalse(bench.healthy_reviewed(changed))
        missing_layout = copy.deepcopy(doc)
        missing_layout["healthy_control_review"].pop("two_column")
        self.assertFalse(bench.healthy_reviewed(missing_layout))

    def test_two_column_controls_need_independent_read_order_anchors(self):
        doc = self.manifest["documents"][0]
        doc["references"][1].update(type="snippet", expected="other body")
        self.assertFalse(bench.healthy_reviewed(doc))
        doc["healthy_control_review"]["two_column"] = False
        self.assertTrue(bench.healthy_reviewed(doc))

    def test_missing_sampled_control_ref_closes_all_control_gates(self):
        for doc in self.manifest["documents"]:
            doc["healthy_control_review"]["method"] = "independent_sampled_source_pages_review"
        self.manifest["documents"][0]["references"][1]["content_scope"] = "title"
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(report["decision"], "NO_WINNER")
        self.assertEqual(report["required_route_status"], "NOT_ACCEPTED")

    def test_frozen_required_classes_allow_bounded_route_not_absent_class_acceptance(self):
        self.config["gates"]["required_classes"] = ["encoding", "layout", "healthy"]
        for doc in self.manifest["documents"]:
            doc["classes"] = ["encoding", "layout", "healthy"]
            doc["references"] = [r for r in doc["references"] if r["type"] in bench.QUALITY_CHECKS]
        seal, _, _, runs = self.collection()
        report = bench.grade(seal["payload"], runs)
        self.assertEqual(report["required_route_status"], "PASS")
        self.assertEqual(report["decision"], "CLASS_SPECIFIC_CANDIDATES")
        self.assertFalse(report["universal_winner"])
        for decision in report["class_decisions"]:
            if decision["class"] not in self.config["gates"]["required_classes"]:
                self.assertEqual(decision["decision"], "NOT_ACCEPTED")
                self.assertEqual(decision["measurement_status"], bench.NOT_MEASURED)
                self.assertEqual(decision["exception"], "CLASS_ABSENT_FROM_FROZEN_SAMPLE")
        absent = [s for s in report["summaries"] if s["class"] == "math"]
        self.assertTrue(all(s["measurement_status"] == bench.NOT_MEASURED and s["quality_checks"]["declared"] == 0 for s in absent))

    def test_default_all_required_classes_does_not_accept_absent_class(self):
        for doc in self.manifest["documents"]:
            doc["classes"].remove("scans")
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        self.assertEqual(set(report["required_classes"]), set(bench.CLASSES))
        self.assertEqual(report["required_route_status"], "NOT_ACCEPTED")
        self.assertFalse(report["universal_winner"])
        absent = next(d for d in report["class_decisions"] if d["class"] == "scans")
        self.assertEqual(absent["measurement_status"], bench.NOT_MEASURED)

    def test_semantic_coverage_counts_are_configured_not_hardcoded(self):
        self.config["gates"]["min_semantic_checks_per_class_split"] = {"math": 3}
        seal, _, _, runs = self.collection()
        report = self.reviewed_grade(seal, runs)
        math = next(d for d in report["class_decisions"] if d["class"] == "math")
        self.assertIn("holdout: predeclared semantic coverage gate failed", math["blockers"])
        self.assertEqual(report["required_route_status"], "NOT_ACCEPTED")
        self.assertFalse(report["universal_winner"])

    def test_optional_required_class_and_semantic_coverage_config_validation(self):
        for required in ([], ["math", "math"], ["unknown"]):
            self.config["gates"]["required_classes"] = required
            with self.assertRaisesRegex(ValueError, "required_classes"):
                bench.validate_config(self.config)
        self.config["gates"].pop("required_classes")
        for counts in ({"math": 0}, {"math": True}, {"layout": 2}):
            self.config["gates"]["min_semantic_checks_per_class_split"] = counts
            with self.assertRaisesRegex(ValueError, "semantic coverage counts"):
                bench.validate_config(self.config)


if __name__ == "__main__":
    unittest.main()
