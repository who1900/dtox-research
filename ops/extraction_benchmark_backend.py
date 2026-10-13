"""Offline, document-scoped adapters; run inside the benchmark's read-only sandbox."""

import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback


SCHEMA = "dtox-extraction-backend-1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_path(value, directory=False):
    require(isinstance(value, str) and Path(value).is_absolute(), "absolute local path required")
    path = Path(value).resolve(strict=True)
    require(path.is_dir() if directory else path.is_file(), "wrong path kind: " + value)
    return path


def verify_pin(pin):
    path = local_path(pin["path"])
    require(file_sha(path) == pin["sha256"], "SHA mismatch: " + str(path))
    return path


def configure_offline(parent):
    # Process-local settings, before importing either backend. The outer sandbox
    # is still required: environment flags alone are not a network/write barrier.
    values = {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "",
        "HIP_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4",
        "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(parent),
        "TMPDIR": str(parent), "TMP": str(parent), "TEMP": str(parent),
        "HF_HOME": str(parent / "hf-cache"),
        "XDG_CACHE_HOME": str(parent / "cache"),
        "TORCH_HOME": str(parent / "torch-cache"),
    }
    os.environ.update(values)
    sys.dont_write_bytecode = True
    # tempfile may already have cached a host directory before adapter entry.
    import tempfile
    tempfile.tempdir = str(parent)


def verify_versions(config):
    required = ({"pymupdf", "pymupdf4llm", "pymupdf-layout"}
                if config["backend"] == "pymupdf" else
                {"docling", "docling-core", "pymupdf", "torch", "rapidocr", "onnxruntime"})
    versions = config["packages"]
    require(required <= versions.keys(), "missing dependency version pins")
    for name, expected in versions.items():
        require(isinstance(expected, str) and expected, "invalid dependency version pin")
        require(importlib.metadata.version(name) == expected, "dependency version mismatch: " + name)
    return versions


def execution_identity():
    receipt = {key: getattr(os, method, lambda: None)() for key, method in
               (("uid", "getuid"), ("euid", "geteuid"), ("gid", "getgid"), ("egid", "getegid"))}
    receipt["groups"] = getattr(os, "getgroups", lambda: None)()
    receipt.update(capabilities=None, uid_map=None, gid_map=None, no_new_privs=None)
    if sys.platform.startswith("linux"):
        fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        receipt["capabilities"] = {key: int(fields[key].strip(), 16) for key in
                                   ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")}
        receipt["no_new_privs"] = int(fields["NoNewPrivs"].strip())
        for key in ("uid_map", "gid_map"):
            receipt[key] = [[int(value) for value in line.split()] for line in
                            Path("/proc/self/" + key).read_text().splitlines()]
    return receipt


def verify_execution_identity(config, receipt):
    if "execution_identity_uid" not in config:
        return
    require(type(config["execution_identity_uid"]) is int and config["execution_identity_uid"] == 65534,
            "execution_identity_uid must be 65534")
    require(all(receipt.get(key) == 65534 for key in ("uid", "euid", "gid", "egid")) and
            receipt.get("groups") == [], "parser must have real UID/GID65534 and cleared groups")
    capabilities = receipt.get("capabilities")
    require(isinstance(capabilities, dict) and set(capabilities) ==
            {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"} and
            all(type(value) is int and value == 0 for value in capabilities.values()), "parser capabilities must all be zero")
    for key in ("uid_map", "gid_map"):
        mapping = receipt.get(key)
        require(isinstance(mapping, list) and mapping and
                all(len(row) == 3 and all(type(value) is int for value in row) and
                    row[0] == row[1] and row[2] > 0 for row in mapping) and
                any(row[0] <= 65534 < row[0] + row[2] for row in mapping), "parser requires host identity " + key)
    require(receipt.get("no_new_privs") == 1, "parser requires no_new_privs")


def validate_request(request, input_path):
    require(request.get("whole_document") is True, "only whole documents supported")
    require(isinstance(request.get("document_id"), str) and request["document_id"], "document ID required")
    require(request.get("source_kind") in ("arxiv_pdf", "other_pdf"), "source_kind required")
    require(file_sha(input_path) == request.get("original_sha256"), "original SHA mismatch")
    # Unknown edition remains provenance, not an extraction-quality veto.
    for field in ("source_uri", "source_version"):
        require(isinstance(request.get(field), dict), field + " explicit state required")
        require(request[field].get("status") in ("established", "missing", "unestablished"),
                field + " invalid state")
        require("value" in request[field], field + " explicit value required")
        if request[field]["status"] == "established":
            require(isinstance(request[field]["value"], str) and request[field]["value"],
                    field + " established value required")


def pdf_page_count(path):
    import pymupdf
    with pymupdf.open(str(path)) as document:
        require(document.is_pdf and not document.needs_pass, "readable, unencrypted PDF required")
        count = document.page_count
    require(type(count) is int and count > 0, "invalid PDF page count")
    return count


def load_source(name, path):
    require(name not in sys.modules, "staged module already imported: " + name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def pymupdf_backend(path, config, request, result):
    pins = config["source_pins"]
    extractor_path = verify_pin(pins["extractor"])
    helper_path = verify_pin(pins["pdf_memory"])
    common_path = verify_pin(pins["common_util"])
    native = local_path(config["native_build"], directory=True)
    require(helper_path == extractor_path.parent / "pdf_memory.py", "helper must be beside staged extractor")
    require(common_path == native / "pymupdf" / "layout" / "common_util.py", "wrong native source pin")
    require(not any(name in sys.modules for name in ("pymupdf", "fitz", "pymupdf4llm")),
            "native dependencies already imported")
    sys.path.insert(0, str(extractor_path.parent))
    sys.path.insert(0, str(native))
    helper = load_source("pdf_memory", helper_path)
    verification = helper.verify_native_build(str(native))
    extractor = load_source("_dtox_benchmark_staged_extractor", extractor_path)
    timeout = {"arxiv_pdf": 90, "other_pdf": 120}[request["source_kind"]]
    require(config["internal_timeouts_seconds"] == {"arxiv_pdf": 90, "other_pdf": 120},
            "current baseline requires original 90/120 timeouts")
    require(request.get("baseline_internal_timeout_seconds") == timeout, "baseline timeout request mismatch")
    result["provenance"].update(source_pins=pins, native_build=str(native),
                                native_verification=verification, internal_timeout_seconds=timeout)
    count = pdf_page_count(path)  # Metadata only; never get_text or per-page conversion.
    result["coverage"]["page_count"] = count
    markdown = extractor.convert_pdf_isolated(str(path), timeout, native_build=str(native))
    require(isinstance(markdown, str), "baseline did not return Markdown text")
    result["document"] = {"text": markdown, "formulas": [], "tables": [], "sections": []}
    result["structure_support"] = {"formulas": "not_exposed", "tables": "not_exposed",
                                   "sections": "not_exposed"}
    result["coverage"].update(whole_document=True, basis="successful exact whole-document call; PDF metadata count")
    result["status"] = "OK"


def verify_models(config):
    root = local_path(config["artifacts_path"], directory=True)
    pins = config["model_pins"]
    require(isinstance(pins, list) and pins, "explicit local model pins required")
    paths = [verify_pin(pin) for pin in pins]
    require(len(set(paths)) == len(paths), "duplicate model pin")
    require(all(path.is_relative_to(root) for path in paths), "model pin outside artifacts_path")
    entries = list(root.rglob("*"))
    require(not any(path.is_symlink() for path in entries), "model artifact symlinks not supported")
    require(set(paths) == {path.resolve() for path in entries if path.is_file()},
            "model pins must enumerate complete artifact tree")
    ocr = config["ocr"]
    require(isinstance(ocr["lang"], list) and len(ocr["lang"]) == 1 and
            isinstance(ocr["lang"][0], str) and ocr["lang"][0], "one explicit OCR language required")
    require(ocr["model_size"] in ("tiny", "small", "medium"), "explicit OCR model size required")
    for key in ("det_model_path", "cls_model_path", "rec_model_path", "rec_keys_path"):
        value = ocr[key]
        if value is None:
            require(key == "rec_keys_path", "explicit local OCR checkpoint required: " + key)
        else:
            require(local_path(value) in paths, "OCR path must be model-pinned: " + key)
    return root


def make_docling_converter(config):
    root = verify_models(config)
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    ocr = config["ocr"]
    options = PdfPipelineOptions(
        artifacts_path=root,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4),
        enable_remote_services=False, allow_external_plugins=False,
        do_ocr=True, do_table_structure=True,
        do_picture_classification=False, do_picture_description=False,
        do_chart_extraction=False,
        do_code_enrichment=False, do_formula_enrichment=False,
        generate_page_images=False, generate_picture_images=False, generate_table_images=False,
        document_timeout=None,
        ocr_options=RapidOcrOptions(
            backend="onnxruntime", lang=ocr["lang"], model_size=ocr["model_size"],
            det_model_path=ocr["det_model_path"], cls_model_path=ocr["cls_model_path"],
            rec_model_path=ocr["rec_model_path"], rec_keys_path=ocr["rec_keys_path"],
        ),
    )
    return DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_cls=StandardPdfPipeline, pipeline_options=options),
    })


def native_json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [native_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): native_json(item) for key, item in value.items()}
    # Retain unfamiliar native errors, never silently discard them.
    return {"type": type(value).__name__, "repr": repr(value)}


def docling_structure(document):
    formulas, tables, sections, headings = [], [], [], []
    for item, _level in document.iterate_items():
        label = getattr(item.label, "value", item.label)
        if label == "formula":
            formulas.append(item.text)
        elif label == "table":
            tables.append({"id": item.self_ref, "cells": [
                {"row": cell.start_row_offset_idx, "column": cell.start_col_offset_idx,
                 "text": cell.text, "end_row": cell.end_row_offset_idx,
                 "end_column": cell.end_col_offset_idx}
                for cell in item.data.table_cells
            ]})
        elif label == "section_header":
            headings.append(native_json(item))
            # Only explicit native hierarchy gives us a section body. Do not
            # infer heading spans by scanning Markdown or neighbouring items.
            if item.children:
                texts, seen = [], set()

                def visit(node):
                    require(node.self_ref not in seen, "cyclic/duplicate native section descendant")
                    seen.add(node.self_ref)
                    if isinstance(getattr(node, "text", None), str):
                        texts.append(node.text)
                    for child in node.children:
                        visit(child.resolve(document))

                for child in item.children:
                    visit(child.resolve(document))
                sections.append({"title": item.text, "text": "\n\n".join(texts),
                                 "id": item.self_ref, "basis": "native_children_text"})
    return formulas, tables, sections, headings


def docling_backend(path, config, request, result):
    converter = make_docling_converter(config)
    count = pdf_page_count(path)
    result["coverage"]["page_count"] = count
    conversion = converter.convert(source=path, raises_on_error=False)
    status = getattr(conversion.status, "value", conversion.status)
    errors = native_json(conversion.errors)
    processed = [page.page_no for page in conversion.pages]
    result["provenance"].update(artifacts_path=config["artifacts_path"], model_pins=config["model_pins"],
                                ocr=config["ocr"], accelerator="cpu", num_threads=4,
                                native_status=status, native_errors=errors,
                                processed_page_numbers=processed)
    document = conversion.document
    # Keep the complete native result before any projection can fail.
    result["native_document"] = document.export_to_dict()
    result["document"] = {"text": document.export_to_markdown(), "formulas": [], "tables": [], "sections": []}
    formulas, tables, sections, headings = docling_structure(document)
    result["document"].update(formulas=formulas, tables=tables, sections=sections)
    result["native_headings"] = headings
    native_page_numbers = sorted(document.pages)
    result["provenance"]["native_page_numbers"] = native_page_numbers
    result["native_pages"] = [
        {"page": page, "text": document.export_to_markdown(page_no=page)}
        for page in native_page_numbers
    ]
    result["structure_support"] = {"formulas": "native_labels_no_enrichment",
                                   "tables": "native_cells_start_coordinates_no_span_expansion",
                                   "sections": "native_children_only; ungrouped headings retained separately"}
    complete = (status == "success" and not errors and
                sorted(processed) == list(range(1, count + 1)) and
                native_page_numbers == list(range(1, count + 1)))
    result["coverage"].update(whole_document=complete,
                             basis="native success, no errors, processed and document pages equal PDF metadata")
    result["status"] = "OK" if complete else "INCOMPLETE"
    if not complete:
        result["errors"].append({"type": "IncompleteConversion", "message":
                                 "native status/errors/page coverage do not establish complete conversion"})


def run(input_path, config, request, result):
    result["provenance"]["execution_identity"] = execution_identity()
    verify_execution_identity(config, result["provenance"]["execution_identity"])
    require(config.get("schema") == SCHEMA, "unsupported backend config schema")
    require(config.get("backend") in ("pymupdf", "docling"), "only pymupdf/docling supported")
    require(config.get("output_scope") == "document", "adapter requires declared document scope")
    result["provenance"].update(backend=config["backend"], output_scope="document", request=request)
    validate_request(request, input_path)
    result["provenance"]["packages"] = verify_versions(config)
    backend = pymupdf_backend if config["backend"] == "pymupdf" else docling_backend
    backend(input_path, config, request, result)
    require(file_sha(input_path) == request["original_sha256"], "original changed during extraction")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("input", "output", "config", "request"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    require(output.is_absolute() and output.name not in ("", ".", ".."), "absolute output file required")
    parent = local_path(str(output.parent), directory=True)
    output = parent / output.name
    inputs = [local_path(value) for value in (args.input, args.config, args.request)]
    require(output not in inputs, "output must not overwrite an input")
    # O_EXCL also refuses existing symlinks. Reserve before importing models.
    with output.open("x", encoding="utf-8", newline="") as stream:
        result = {"schema": SCHEMA, "status": "ERROR", "errors": [], "provenance": {},
                  "coverage": {"whole_document": False, "page_count": None}}
        try:
            os.chdir(parent)
            configure_offline(parent)
            config = json.loads(inputs[1].read_text(encoding="utf-8"))
            request = json.loads(inputs[2].read_text(encoding="utf-8"))
            result["provenance"]["config_sha256"] = file_sha(inputs[1])
            result["provenance"]["request_sha256"] = file_sha(inputs[2])
            run(inputs[0], config, request, result)
        except Exception as error:
            result["status"] = "ERROR"
            result["coverage"]["whole_document"] = False
            result["errors"].append({"type": type(error).__name__, "message": str(error),
                                     "traceback": traceback.format_exc(),
                                     "reason": getattr(error, "reason", None),
                                     "returncode": getattr(error, "returncode", None)})
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
