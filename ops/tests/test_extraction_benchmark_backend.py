"""Mock-only adapter tests: no installed Docling, real PDF, subprocess or network."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from ops import extraction_benchmark_backend as backend


class NativeItem:
    def __init__(self, label, text="", ref="#/texts/0", children=(), **kwargs):
        self.label = NS(value=label)
        self.text = text
        self.self_ref = ref
        self.children = list(children)
        self.__dict__.update(kwargs)

    def model_dump(self, **kwargs):
        return {"label": self.label.value, "text": self.text, "self_ref": self.self_ref,
                "prov": [{"page_no": 2, "bbox": [1, 2, 3, 4]}]}


class NativeDocument:
    def __init__(self, pages=(1, 2, 3), items=()):
        self.pages = {page: {} for page in pages}
        self.items = items
        self.exports = []

    def export_to_dict(self):
        return {"pages": self.pages, "texts": [item.model_dump() for item in self.items]}

    def export_to_markdown(self, page_no=None):
        self.exports.append(page_no)
        return "# Exact\n\n full  α\n\n" if page_no is None else ("" if page_no == 2 else str(page_no))

    def iterate_items(self):
        return ((item, 0) for item in self.items)


class BackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        self.root = Path(temporary.name)
        self.original = self.root / "original"
        self.original.write_bytes(b"not a real PDF; must never reach installed readers")
        self.request = {
            "document_id": "acl:independent-control", "original_sha256": backend.file_sha(self.original),
            "source_uri": {"status": "established", "value": "https://example.invalid/paper.pdf"},
            "source_version": {"status": "unestablished", "value": None},
            "source_kind": "other_pdf", "whole_document": True,
            "baseline_internal_timeout_seconds": 120,
        }
        self.config = {"schema": backend.SCHEMA, "backend": "docling", "output_scope": "document",
                       "packages": {key: "mock-version" for key in
                                    ("docling", "docling-core", "pymupdf", "torch", "rapidocr", "onnxruntime")}}
        self.models = self.root / "models"
        self.models.mkdir()
        self.config["artifacts_path"] = str(self.models)
        self.config["model_pins"] = []
        self.config["ocr"] = {"lang": ["en"], "model_size": "small", "rec_keys_path": None}
        for name in ("det", "cls", "rec", "layout", "table"):
            path = self.models / (name + ".onnx")
            path.write_bytes(name.encode())
            self.config["model_pins"].append({"path": str(path), "sha256": backend.file_sha(path)})
            if name in ("det", "cls", "rec"):
                self.config["ocr"][name + "_model_path"] = str(path)
        self.result = {"status": "ERROR", "errors": [], "provenance": {},
                       "coverage": {"whole_document": False, "page_count": None}}

    def conversion(self, status="success", pages=(1, 2, 3), errors=(), document=None):
        return NS(status=NS(value=status), pages=[NS(page_no=page) for page in pages],
                  errors=list(errors), document=document or NativeDocument())

    def docling_run(self, conversion):
        converter = Mock()
        converter.convert.return_value = conversion
        with patch.object(backend, "make_docling_converter", return_value=converter), \
                patch.object(backend, "pdf_page_count", return_value=3):
            backend.docling_backend(self.original, self.config, self.request, self.result)
        converter.convert.assert_called_once_with(source=self.original, raises_on_error=False)
        return self.result

    def baseline_config(self):
        staged = self.root / "staged"
        staged.mkdir()
        native = self.root / "native"
        common = native / "pymupdf" / "layout" / "common_util.py"
        common.parent.mkdir(parents=True)
        sources = {"extractor": staged / "extractor.py", "pdf_memory": staged / "pdf_memory.py",
                   "common_util": common}
        for name, path in sources.items():
            path.write_bytes(name.encode())
        return {"schema": backend.SCHEMA, "backend": "pymupdf", "output_scope": "document",
                "native_build": str(native),
                "internal_timeouts_seconds": {"arxiv_pdf": 90, "other_pdf": 120},
                "source_pins": {key: {"path": str(path), "sha256": backend.file_sha(path)}
                                for key, path in sources.items()},
                "packages": {key: "mock-version" for key in ("pymupdf", "pymupdf-layout", "pymupdf4llm")}}

    def baseline_run(self, config, text="exact \r\n# Title\n $x$ | cell |", error=None):
        helper = Mock()
        helper.verify_native_build.return_value = {"revision": "test-pinned-build"}
        extractor = Mock()
        extractor.convert_pdf_isolated.return_value = text
        extractor.convert_pdf_isolated.side_effect = error
        with patch.object(backend, "load_source", side_effect=[helper, extractor]), \
                patch.object(backend, "pdf_page_count", return_value=17), \
                patch.object(sys, "path", list(sys.path)):
            backend.pymupdf_backend(self.original, config, self.request, self.result)
        helper.verify_native_build.assert_called_once_with(config["native_build"])
        return extractor

    def test_exact_baseline_entire_document_and_120_timeout(self):
        config = self.baseline_config()
        exact = "  ## Heading\r\n α  $x$ | cell |\n\n"
        extractor = self.baseline_run(config, exact)
        extractor.convert_pdf_isolated.assert_called_once_with(str(self.original), 120,
                                                               native_build=config["native_build"])
        self.assertEqual(self.result["document"]["text"], exact)
        self.assertEqual(self.result["document"]["formulas"], [])
        self.assertEqual(self.result["document"]["tables"], [])
        self.assertEqual(self.result["document"]["sections"], [])
        self.assertEqual(self.result["coverage"]["page_count"], 17)
        self.assertTrue(self.result["coverage"]["whole_document"])
        self.assertEqual(self.result["status"], "OK")

    def test_arxiv_original_90_timeout(self):
        self.request.update(source_kind="arxiv_pdf", baseline_internal_timeout_seconds=90)
        config = self.baseline_config()
        extractor = self.baseline_run(config)
        self.assertEqual(extractor.convert_pdf_isolated.call_args.args[1], 90)

    def test_baseline_request_timeout_mismatch_rejected(self):
        self.request["baseline_internal_timeout_seconds"] = 999
        with self.assertRaisesRegex(ValueError, "timeout request mismatch"):
            self.baseline_run(self.baseline_config())

    def test_baseline_config_timeout_change_rejected(self):
        config = self.baseline_config()
        config["internal_timeouts_seconds"]["other_pdf"] = 999
        with self.assertRaisesRegex(ValueError, "original 90/120"):
            self.baseline_run(config)

    def test_wrong_staged_hash_fails_before_import(self):
        config = self.baseline_config()
        config["source_pins"]["extractor"]["sha256"] = "0" * 64
        with patch.object(backend, "load_source") as loader:
            with self.assertRaisesRegex(ValueError, "SHA mismatch"):
                backend.pymupdf_backend(self.original, config, self.request, self.result)
            loader.assert_not_called()

    def test_no_fallback_after_baseline_error(self):
        with self.assertRaisesRegex(RuntimeError, "conversion_timeout"):
            self.baseline_run(self.baseline_config(), error=RuntimeError("conversion_timeout"))
        self.assertFalse(self.result["coverage"]["whole_document"])

    def test_native_build_mismatch_fails_closed(self):
        config = self.baseline_config()
        with patch.object(backend, "load_source") as loader, patch.object(sys, "path", list(sys.path)):
            loader.return_value.verify_native_build.side_effect = ValueError("wrong patch")
            with self.assertRaisesRegex(ValueError, "wrong patch"):
                backend.pymupdf_backend(self.original, config, self.request, self.result)
            self.assertEqual(loader.call_count, 1)

    def test_baseline_non_string_rejected(self):
        with self.assertRaisesRegex(ValueError, "Markdown text"):
            self.baseline_run(self.baseline_config(), text=None)

    def test_docling_full_call_and_blank_page_preserved(self):
        document = NativeDocument()
        result = self.docling_run(self.conversion(document=document))
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["coverage"]["whole_document"])
        self.assertEqual(result["document"]["text"], "# Exact\n\n full  α\n\n")
        self.assertEqual(result["native_pages"][1], {"page": 2, "text": ""})
        self.assertEqual(document.exports, [None, 1, 2, 3])
        self.assertIn("native_document", result)

    def test_docling_partial_keeps_text_errors_and_false_coverage(self):
        error = {"category": "timeout", "error_message": "stage stopped", "page_no": 3}
        result = self.docling_run(self.conversion("partial_success", (1, 2), [error], NativeDocument((1, 2))))
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["coverage"]["whole_document"])
        self.assertEqual(result["coverage"]["page_count"], 3)
        self.assertEqual(result["provenance"]["native_errors"], [error])
        self.assertIn("full", result["document"]["text"])

    def test_docling_success_label_insufficient_for_missing_or_duplicate_pages(self):
        for pages in ((1, 2), (1, 2, 2), (0, 1, 2)):
            with self.subTest(pages=pages):
                self.assertEqual(self.docling_run(self.conversion(pages=pages))["status"], "INCOMPLETE")

    def test_docling_native_document_coverage_required(self):
        result = self.docling_run(self.conversion(document=NativeDocument((1, 3))))
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_docling_native_error_with_success_is_not_ok(self):
        result = self.docling_run(self.conversion(errors=[{"error_message": "unresolved failure"}]))
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_native_structure_not_substring_inference(self):
        cell = NS(start_row_offset_idx=0, start_col_offset_idx=1, end_row_offset_idx=2,
                  end_col_offset_idx=3, text="merged")
        body = NativeItem("text", "Native child body", "#/texts/3")
        heading = NativeItem("section_header", "Methods", "#/texts/1", [NS(resolve=lambda doc: body)])
        ungrouped = NativeItem("section_header", "No inferred span", "#/texts/2")
        formula = NativeItem("formula", "x + y", "#/texts/4")
        table = NativeItem("table", ref="#/tables/0", data=NS(table_cells=[cell]))
        text = NativeItem("text", "$not_formula$ | not a table |", "#/texts/5")
        result = self.docling_run(self.conversion(document=NativeDocument(items=[heading, ungrouped, formula, table, text])))
        self.assertEqual(result["document"]["formulas"], ["x + y"])
        self.assertEqual(result["document"]["tables"][0]["id"], "#/tables/0")
        self.assertEqual(len(result["document"]["tables"][0]["cells"]), 1)
        self.assertEqual(result["document"]["sections"][0]["text"], "Native child body")
        self.assertEqual(len(result["document"]["sections"]), 1)
        self.assertEqual(len(result["native_headings"]), 2)
        self.assertEqual(result["native_headings"][0]["prov"][0]["page_no"], 2)

    def test_model_hash_and_inventory_fail_closed(self):
        self.models.joinpath("layout.onnx").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            backend.verify_models(self.config)

    def test_unpinned_model_not_silently_used(self):
        self.models.joinpath("extra.onnx").write_bytes(b"unpinned")
        with self.assertRaisesRegex(ValueError, "complete artifact tree"):
            backend.verify_models(self.config)

    def test_missing_checkpoint_and_multilanguage_rejected(self):
        for update in ({"det_model_path": None}, {"lang": ["en", "ch"]}):
            config = copy.deepcopy(self.config)
            config["ocr"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                backend.verify_models(config)

    def test_docling_options_are_explicit_offline_cpu_standard(self):
        options, rapid, accelerator, format_option, converter = [Mock() for _ in range(5)]
        modules = {}
        exports = {
            "docling.datamodel.accelerator_options": {"AcceleratorDevice": NS(CPU="cpu"),
                                                      "AcceleratorOptions": accelerator},
            "docling.datamodel.base_models": {"InputFormat": NS(PDF="pdf")},
            "docling.datamodel.pipeline_options": {"PdfPipelineOptions": options, "RapidOcrOptions": rapid},
            "docling.document_converter": {"DocumentConverter": converter, "PdfFormatOption": format_option},
            "docling.pipeline.standard_pdf_pipeline": {"StandardPdfPipeline": "standard-class"},
        }
        for name, attributes in exports.items():
            modules[name] = ModuleType(name)
            modules[name].__dict__.update(attributes)
        with patch.dict(sys.modules, modules):
            backend.make_docling_converter(self.config)
        accelerator.assert_called_once_with(device="cpu", num_threads=4)
        self.assertEqual(rapid.call_args.kwargs["backend"], "onnxruntime")
        self.assertEqual(rapid.call_args.kwargs["rec_model_path"], self.config["ocr"]["rec_model_path"])
        self.assertNotIn("rapidocr_params", rapid.call_args.kwargs)
        kwargs = options.call_args.kwargs
        for key in ("enable_remote_services", "allow_external_plugins", "do_picture_classification",
                    "do_picture_description", "do_code_enrichment", "do_formula_enrichment",
                    "do_chart_extraction", "generate_page_images", "generate_picture_images", "generate_table_images"):
            self.assertIs(kwargs[key], False, key)
        self.assertEqual(kwargs["artifacts_path"], self.models)
        self.assertIsNone(kwargs["document_timeout"])
        self.assertEqual(format_option.call_args.kwargs["pipeline_cls"], "standard-class")
        self.assertEqual(converter.call_args.kwargs["allowed_formats"], ["pdf"])

    def test_unknown_edition_same_original_evaluable(self):
        with patch.object(backend, "verify_versions", return_value={}), \
                patch.object(backend, "docling_backend") as runner:
            backend.run(self.original, self.config, self.request, self.result)
        runner.assert_called_once()
        self.assertEqual(self.result["provenance"]["request"]["source_version"]["status"], "unestablished")

    def test_bad_sha_no_backend_call(self):
        self.request["original_sha256"] = "0" * 64
        with patch.object(backend, "docling_backend") as runner, self.assertRaisesRegex(ValueError, "original SHA"):
            backend.run(self.original, self.config, self.request, self.result)
        runner.assert_not_called()

    def test_only_two_engines_and_document_scope(self):
        for update in ({"backend": "third"}, {"output_scope": "pages"}):
            config = dict(self.config, **update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                backend.run(self.original, config, self.request, self.result)

    def identity_receipt(self):
        return {"uid": 65534, "euid": 65534, "gid": 65534, "egid": 65534, "groups": [],
                "uid_map": [[0, 0, 4294967295]], "gid_map": [[0, 0, 4294967295]], "no_new_privs": 1,
                "capabilities": dict.fromkeys(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"), 0)}

    def test_real_host_identity_required_before_pdf_hash_or_backend_import(self):
        self.config["execution_identity_uid"] = 65534
        for change in ({"uid_map": [[65534, 0, 1]]}, {"gid_map": [[65534, 0, 1]]},
                       {"uid": 0}, {"euid": 0}, {"gid": 0}, {"egid": 0}, {"groups": [0]},
                       {"capabilities": dict(self.identity_receipt()["capabilities"], CapBnd=1)},
                       {"capabilities": None}, {"no_new_privs": 0}, {"uid_map": None}):
            receipt = dict(self.identity_receipt(), **change)
            with self.subTest(change=change), patch.object(backend, "execution_identity", return_value=receipt), \
                    patch.object(backend, "file_sha") as hashed, \
                    patch.object(backend, "verify_versions") as imported, self.assertRaises(ValueError):
                backend.run(self.original, self.config, self.request, self.result)
            hashed.assert_not_called()
            imported.assert_not_called()
            self.assertEqual(self.result["provenance"]["execution_identity"], receipt)

    def test_identity_provenance_and_optional_config_backcompat(self):
        receipt = self.identity_receipt()
        for declared in (False, True):
            if declared:
                self.config["execution_identity_uid"] = 65534
            with patch.object(backend, "execution_identity", return_value=receipt), \
                    patch.object(backend, "verify_versions", return_value={}), \
                    patch.object(backend, "docling_backend") as converter:
                backend.run(self.original, self.config, self.request, self.result)
            converter.assert_called_once()
            self.assertEqual(self.result["provenance"]["execution_identity"], receipt)
        for invalid in (0, True, "65534", 1000):
            with self.assertRaisesRegex(ValueError, "must be 65534"):
                backend.verify_execution_identity({"execution_identity_uid": invalid}, receipt)

    def test_linux_identity_receipt_reads_all_caps_and_mapping(self):
        status = "\n".join(key + ":\t0000000000000000" for key in self.identity_receipt()["capabilities"])
        status += "\nNoNewPrivs:\t1\n"
        def read(path, *args, **kwargs):
            return status if path.name == "status" else "         0          0 4294967295\n"
        with patch.object(backend.sys, "platform", "linux"), patch.object(Path, "read_text", read), \
                patch.object(backend.os, "getuid", return_value=65534, create=True), \
                patch.object(backend.os, "geteuid", return_value=65534, create=True), \
                patch.object(backend.os, "getgid", return_value=65534, create=True), \
                patch.object(backend.os, "getegid", return_value=65534, create=True), \
                patch.object(backend.os, "getgroups", return_value=[], create=True):
            receipt = backend.execution_identity()
        self.assertEqual(receipt, self.identity_receipt())
        backend.verify_execution_identity({"execution_identity_uid": 65534}, receipt)

    def test_cli_enters_output_directory_before_offline_and_backend(self):
        def runner(_path, _config, _request, result):
            self.assertEqual(Path.cwd(), self.root)
            result["status"] = "OK"
        code, _output, _argv = self.cli(runner)
        self.assertEqual(code, 0)

    def test_packages_exact_and_required(self):
        with patch.object(backend.importlib.metadata, "version", return_value="mock-version"):
            backend.verify_versions(self.config)
        with patch.object(backend.importlib.metadata, "version", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                backend.verify_versions(self.config)
        self.config["packages"].pop("rapidocr")
        with self.assertRaisesRegex(ValueError, "missing dependency"):
            backend.verify_versions(self.config)

    def cli(self, runner):
        config, request, output = [self.root / name for name in ("config.json", "request.json", "output.json")]
        config.write_text(json.dumps(self.config), encoding="utf-8")
        request.write_text(json.dumps(self.request), encoding="utf-8")
        argv = ["--input", str(self.original), "--output", str(output),
                "--config", str(config), "--request", str(request)]
        with patch.object(backend, "configure_offline") as offline, patch.object(backend, "run", side_effect=runner):
            code = backend.main(argv)
        offline.assert_called_once_with(self.root)
        return code, output, argv

    def test_cli_error_evidence_nonzero_and_no_retry(self):
        error = RuntimeError("native crash")
        error.reason, error.returncode = "conversion_failed", -9
        runner = Mock(side_effect=error)
        code, output, _argv = self.cli(runner)
        runner.assert_called_once()
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"][0]["returncode"], -9)
        self.assertEqual(result["errors"][0]["reason"], "conversion_failed")
        self.assertIn("native crash", result["errors"][0]["traceback"])
        self.assertFalse(result["coverage"]["whole_document"])
        self.assertEqual(self.original.read_bytes(), b"not a real PDF; must never reach installed readers")

    def test_cli_exclusive_output_and_required_flags(self):
        def success(_path, _config, _request, result):
            result.update(status="OK", document={"text": "exact"})
            result["coverage"].update(whole_document=True, page_count=3)
        code, output, argv = self.cli(success)
        self.assertEqual(code, 0)
        before = output.read_bytes()
        with patch.object(backend, "run") as runner, self.assertRaises(FileExistsError):
            backend.main(argv)
        runner.assert_not_called()
        self.assertEqual(output.read_bytes(), before)
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            backend.main(["--input", str(self.original)])

    def test_original_post_call_change_detected(self):
        def malicious(*_args):
            self.original.write_bytes(b"mutated fixture")
        with patch.object(backend, "verify_versions", return_value={}), \
                patch.object(backend, "docling_backend", side_effect=malicious), \
                self.assertRaisesRegex(ValueError, "original changed"):
            backend.run(self.original, self.config, self.request, self.result)

    def test_offline_flags_before_imports_and_scratch_inside_parent(self):
        old_tempdir, old_bytecode = tempfile.tempdir, sys.dont_write_bytecode
        self.addCleanup(setattr, tempfile, "tempdir", old_tempdir)
        self.addCleanup(setattr, sys, "dont_write_bytecode", old_bytecode)
        with patch.dict(os.environ, {}, clear=True):
            backend.configure_offline(self.root)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(os.environ["CUDA_VISIBLE_DEVICES"], "")
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "4")
            self.assertEqual(tempfile.tempdir, str(self.root))
            for key in ("HOME", "HF_HOME", "TORCH_HOME", "TMPDIR", "XDG_CACHE_HOME"):
                self.assertTrue(Path(os.environ[key]).is_relative_to(self.root))

    def test_metadata_count_only_not_per_page_extraction(self):
        pdf = Mock(is_pdf=True, needs_pass=False, page_count=41)
        module = ModuleType("pymupdf")
        module.open = Mock()
        module.open.return_value.__enter__ = Mock(return_value=pdf)
        module.open.return_value.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {"pymupdf": module}):
            self.assertEqual(backend.pdf_page_count(self.original), 41)
        module.open.assert_called_once_with(str(self.original))
        pdf.get_text.assert_not_called()
        pdf.load_page.assert_not_called()

    def test_docling_conversion_exception_no_retry(self):
        converter = Mock()
        converter.convert.side_effect = RuntimeError("native converter failed")
        with patch.object(backend, "make_docling_converter", return_value=converter), \
                patch.object(backend, "pdf_page_count", return_value=3), \
                self.assertRaisesRegex(RuntimeError, "native converter failed"):
            backend.docling_backend(self.original, self.config, self.request, self.result)
        converter.convert.assert_called_once()
        self.assertFalse(self.result["coverage"]["whole_document"])

    def test_cli_partial_returns_nonzero_without_losing_native_records(self):
        def partial(_path, _config, _request, result):
            result.update(status="INCOMPLETE", document={"text": "partial native text"},
                          native_document={"texts": ["partial native text"]})
            result["provenance"]["native_errors"] = [{"error_message": "page failure"}]
        code, output, _argv = self.cli(partial)
        self.assertEqual(code, 1)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["native_document"]["texts"], ["partial native text"])
        self.assertEqual(result["provenance"]["native_errors"][0]["error_message"], "page failure")

    def test_explicit_source_state_value_required(self):
        self.request["source_version"].pop("value")
        with self.assertRaisesRegex(ValueError, "explicit value required"):
            backend.validate_request(self.request, self.original)

    def test_no_models_means_no_backend_import(self):
        self.config["artifacts_path"] = str(self.root / "absent")
        with self.assertRaises(FileNotFoundError):
            backend.make_docling_converter(self.config)


if __name__ == "__main__":
    unittest.main()
