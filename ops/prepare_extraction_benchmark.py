"""Prepare a source-only draft; never reads PDFs, PNGs, extractor outputs or services.

CLI: --selection PATH SHA --transfer PATH SHA (repeatable) --review PATH SHA
(repeatable) [--annotations PATH SHA] --output NEW_JSON.
Or --derive-annotations from actual Luna challenge/control-scope schemas; accepted
all declared class gates are recorded, never a quality acceptance.
Annotations schema dtox-benchmark-source-annotations-1: extractor_outputs_seen=false,
documents=[{document_id, original_sha256, classes, status="verified", reviewer, evidence_uri,
two_column (optional), source_version (optional)}], frozen_gates={...}.
Classes are explicit labels, including legacy healthy=control cohort, NOT health.
Additive exact refs use dtox-benchmark-source-references-1, with source-only
attestation, reviewer, documents [{document_id, original_sha256, references}].
References use harness schema and independently verified source review bindings.
Missing inputs yield a non-ready draft, never replacement selection or auto-fetch.
"""
import argparse
from collections import Counter, defaultdict
import copy
import json
from pathlib import Path
import re

try:
    from ops import source_extraction_benchmark as bench
except ModuleNotFoundError:
    import source_extraction_benchmark as bench


MAIN = "main_page1_visual_reference_v1"
CONTROLS = "controls_sampled_source_visual_reference_v1"
EXACT = "dtox-benchmark-source-references-1"
METADATA = "challenge_source_visual_sampling_v1"
ANNOTATIONS = "dtox-benchmark-source-annotations-1"
CHALLENGES = "challenge_source_visual_review_v1"
CONTROL_SCOPES = "control_source_scope_annotations_v1"
BODY_REGIONS = {"body", "left_column_body", "right_column_body", "body_below_table"}
SPATIAL_PREFIXES = {"left", "right", "top", "bottom", "middle", "left bottom", "right top", "right bottom",
                    "right body", "right lower", "full-width top",
                    "full-width lower", "left-bottom", "left-top", "right-bottom", "right-top", "left-column bottom", "right-column top",
                    "left beside algorithm", "left Dataset ends", "right below Figure 3", "right below Figure3",
                    "right below Figure4", "right below FigureB-3", "right below Table 1", "right below Table1",
                    "right below Table4", "right below Table5", "right below Table6", "right top below Table7", "right under Figure8"}


def state(value=None):
    return {"status": "unestablished" if value is None else "established", "value": value}


def load_pin(path, expected):
    path = bench.local(path)
    bench.need(path.suffix.lower() == ".json" and bench.digest(expected), "pinned source JSON required")
    raw = bench.bounded_bytes(path, bench.OUTPUT_READ_LIMITS["json_bytes"])
    bench.need(bench.sha(raw) == expected, "source input SHA mismatch: " + str(path))
    value = json.loads(raw, object_pairs_hook=bench.unique_keys,
                       parse_constant=lambda v: bench.need(False, "nonfinite input"))
    return value, {"path": str(path), "sha256": expected}


def source_only(bundle):
    bench.need(bundle.get("extractor_outputs_seen") is False, "source-only attestation required")


def anchors_from_source(values):
    # These actual review strings mix spatial locators and quoted source text.
    # No colon means descriptive topology, not a verbatim text expectation.
    result = []
    for value in values:
        prefix, separator, text = value.partition(":")
        if not separator or not text.strip() or prefix not in SPATIAL_PREFIXES:
            return None
        result.append(text.strip())
    return result if len(result) >= 2 else None


def scoped_reviews(review_inputs):
    result = [(copy.deepcopy(bundle), pin) for bundle, pin in review_inputs if bundle.get("schema") != CONTROL_SCOPES]
    for scope, pin in review_inputs:
        if scope.get("schema") != CONTROL_SCOPES:
            continue
        source_only(scope)
        matches = [bundle for bundle, original_pin in result if original_pin["sha256"] == scope["source_review"]["sha256"]
                   and bundle.get("schema") == CONTROLS]
        bench.need(len(matches) == 1, "control scopes require exact old review SHA")
        old = matches[0]
        for annotation in scope["documents"]:
            candidates = [doc for doc in old["documents"] if doc["document_id"] == annotation["document_id"]]
            bench.need(len(candidates) == 1, "foreign/duplicate control scope document")
            doc = candidates[0]
            bench.need(doc["original_sha256"] == annotation["original_sha256"], "control scope original SHA mismatch")
            pages = {p["page"]: p for p in doc["pages"]}
            for image in annotation["reviewed_page_layouts"]:
                bench.need(image.get("personally_viewed") is True and bench.digest(image.get("image_sha256")), "scope image not reviewed")
                if image["page"] in pages:
                    bench.need(pages[image["page"]]["image_sha256"] == image["image_sha256"], "scope image SHA mismatch")
                else:
                    doc["pages"].append(copy.deepcopy(image))
                pages[image["page"]] = image
            for item in annotation["reference_annotations"]:
                pointer = re.fullmatch(r"/documents/(\d+)/body_passages/(\d+)", item["source_pointer"])
                bench.need(pointer is not None, "unsupported scope source pointer")
                target_doc = old["documents"][int(pointer[1])]
                target = target_doc["body_passages"][int(pointer[2])]
                bench.need(target_doc is doc and target["page"] == item["page"] and target["text"] == item["text"], "scope pointer/text/page mismatch")
                bench.need(item["original_sha256"] == doc["original_sha256"] and item.get("personally_viewed") is True and
                           item["image_sha256"] == pages[item["page"]]["image_sha256"], "scope source binding mismatch")
                bench.need(item["scope"] in ("body", "other", "title") and item["counts_as_body"] == (item["scope"] == "body"), "scope body assertion mismatch")
                target["content_scope"] = item["scope"]
                target["scope_annotation"] = {"pin": pin, "source_pointer": item["source_pointer"], "previous_region": item.get("previous_region")}
            for item in annotation["additional_source_references"]:
                bench.need(item.get("extractor_outputs_seen") is False and item.get("personally_viewed") is True and
                           item["original_sha256"] == doc["original_sha256"] and item["image_sha256"] == pages[item["page"]]["image_sha256"], "additive scope reference binding mismatch")
                bench.need(item["scope"] in ("body", "other", "title") and item["counts_as_body"] == (item["scope"] == "body"), "additive body assertion mismatch")
                addition = copy.deepcopy(item)
                addition.update(content_scope=item["scope"], scope_annotation={"pin": pin, "additive": True})
                doc["body_passages"].append(addition)
            replacements = annotation["reading_order_annotations"]
            merged = []
            for order in doc.get("reading_order_anchors", []):
                replacement = [item for item in replacements if item["page"] == order["page"]]
                bench.need(len(replacement) <= 1, "ambiguous scoped order page")
                if replacement:
                    item = copy.deepcopy(replacement[0])
                    bench.need(item.get("personally_viewed") is True and item["original_sha256"] == doc["original_sha256"] and
                               item["image_sha256"] == pages[item["page"]]["image_sha256"], "scoped order binding mismatch")
                    item.update(content_scope=item["scope"], scope_annotation={"pin": pin, "legacy_record": order})
                    merged.append(item)
                else:
                    merged.append(order)
            for item in replacements:
                if not any(order["page"] == item["page"] for order in merged):
                    bench.need(item.get("personally_viewed") is True and item["original_sha256"] == doc["original_sha256"] and
                               item["image_sha256"] == pages[item["page"]]["image_sha256"], "scoped order binding mismatch")
                    merged.append(dict(item, content_scope=item["scope"], scope_annotation={"pin": pin, "additive": True}))
            doc["reading_order_anchors"] = merged
    return result


def derive_annotations(selection_input, review_inputs):
    selection, selection_pin = selection_input
    sources = {}
    for bundle, pin in review_inputs:
        if bundle.get("schema") not in (CHALLENGES, CONTROL_SCOPES):
            continue
        source_only(bundle)
        for doc in bundle["documents"]:
            bench.need(doc["document_id"] not in sources, "duplicate source class review")
            sources[doc["document_id"]] = (doc, bundle, pin)
    labels = []
    for chosen in selection["candidates"]:
        if chosen["document_id"] not in sources:
            continue
        doc, bundle, pin = sources[chosen["document_id"]]
        tags = set(doc.get("source_observed_classes", []))
        layouts = [p.get("layout", "") for p in doc.get("pages", [])] + [p["visible_layout"] for p in doc.get("reviewed_page_layouts", [])]
        classes, basis = set(), {"source_review": pin, "source_observed_classes": sorted(tags), "reviewed_layouts": layouts,
                                "source_notes": copy.deepcopy(doc.get("notes", []))}
        for raw, raw_pin in review_inputs:
            if raw.get("schema") == CONTROLS:
                previous = [item for item in raw["documents"] if item["document_id"] == chosen["document_id"]]
                if previous:
                    bench.need(previous[0]["original_sha256"] == doc["original_sha256"], "control class original mismatch")
                    basis["formula_table_source_notes"] = {"pin": raw_pin, "observations": copy.deepcopy(previous[0].get("formula_table_source_notes", [])),
                                                           "not_exact_semantic_expectations": True}
        old = chosen.get("inventory_record", {}).get("status_provenance", {})
        observed_old = doc.get("old_failure_evidence", {})
        basis["old_failure_evidence"] = {"source_review_observation": copy.deepcopy(observed_old),
                                         "selection_pin": selection_pin, "selection_status_provenance": copy.deepcopy(old),
                                         "not_current_quality_evidence": True}
        if observed_old.get("status") == "NEEDS_GLYPH_VERIFICATION":
            bench.need(old.get("status") == observed_old["status"] and old.get("payload_sha256") == observed_old.get("payload_sha256") and
                       bench.digest(old.get("payload_sha256")), "encoding old-selection evidence mismatch")
            classes.add("encoding")
            basis["encoding_old_selection_flag"] = {"selection": selection_pin, "evidence": old, "not_current_extraction_failure": True}
        if tags & {"CJK", "CJK_multilingual", "multilingual", "multilingual_diacritics", "diacritics", "escaped_unicode", "stylized_glyph_figure"}:
            classes.add("encoding")
        if "two_column" in tags or "wrapped_float" in tags or any("two_column" in layout for layout in layouts):
            classes.add("layout")
        if tags & {"display_formula", "inline_formula", "logical_formula", "chemical_formula", "superscript_notation", "subscript_statistics", "proof"} or any(
                any(token in layout for token in ("display_equation", "with_equations", "inline_formulas", "lemma_and_math")) for layout in layouts):
            classes.add("math")
        if tags & {"numeric_table", "table", "notation_table", "numeric_or_categorical_table", "prompt_example_table"} or any(
                re.search(r"(?:^|_)tables?(?:_|$)", layout) for layout in layouts):
            classes.add("tables")
        # Publisher/source_prompt_example/source URI do not imply semantic completeness.
        if tags & {"source_completeness", "missing_section", "truncated_source"}:
            classes.add("source")
        if tags & {"scan", "scanned_pdf", "image_only"}:
            classes.add("scans")
        for gold, gold_pin in review_inputs:
            if gold.get("schema") != EXACT:
                continue
            for reviewed_doc in gold["documents"]:
                if reviewed_doc["document_id"] != chosen["document_id"]:
                    continue
                bench.need(reviewed_doc["original_sha256"] == doc["original_sha256"], "gold class original mismatch")
                for ref in reviewed_doc.get("references", []):
                    if ref.get("expected_kind") != "source_image":
                        continue
                    bench.validate_source_image(ref, doc["original_sha256"], reviewed_doc.get("page_count"))
                    bench.need(bench.reviewed(ref, {"original": {"sha256": doc["original_sha256"]}}),
                               "gold class requires verified source review")
                    classes.update(c for c, check in bench.SEMANTIC_CHECK.items() if ref["type"] == check)
                    basis.setdefault("semantic_image_gold", []).append({"pin": gold_pin, "check_id": ref["check_id"]})
        if chosen["cohort"] == "html_potential_control":
            classes.add("healthy")
            basis["healthy"] = "selection control cohort only; NOT wholepaper healthy"
        bench.need(classes, "unestablished source classes: " + chosen["document_id"])
        label = {"document_id": chosen["document_id"], "original_sha256": doc["original_sha256"], "status": "verified",
                 "classes": sorted(classes), "reviewer": bundle["reviewer"], "evidence_uri": pin["path"], "class_provenance": basis}
        if "two_column" in doc:
            bench.need(type(doc["two_column"]) is bool, "explicit control two_column required")
            label["two_column"] = doc["two_column"]
        labels.append(label)
    return {"schema": ANNOTATIONS, "extractor_outputs_seen": False, "documents": labels,
            "derivation_code_sha256": bench.file_sha(__file__),
            "harness_schema_code_sha256": bench.file_sha(bench.__file__),
            "textual_reference_normalization": "nfc_whitespace; no casefold/NFKC/fuzzy/word repair",
            "source_inputs": [pin for bundle, pin in review_inputs], "selection": selection_pin,
            "frozen_gates": {"expected_physical_documents": 100, "min_documents_per_class_split": 1,
                             "min_reviewed_checks_per_document": 2, "min_observed_check_ratio": {"all": 1.0},
                             "max_healthy_regressions": 0, "max_class_regressions": 0, "min_candidate_check_gain": 1,
                             "required_classes": sorted({c for label in labels for c in label["classes"]} - {"healthy"})}}


def adapt_review(bundle, pin):
    source_only(bundle)
    kind = bundle.get("schema")
    bench.need(kind in (MAIN, CONTROLS, EXACT, METADATA, CHALLENGES), "unsupported source review schema; do not guess")
    result = []
    for doc in bundle["documents"]:
        refs, unadapted = [], []
        if kind == EXACT:
            refs = copy.deepcopy(doc.get("references", []))
            for ref in refs:
                bench.need(bench.reviewed(ref, {"original": {"sha256": doc["original_sha256"]}}),
                           "exact ref requires independent verified source review")
                if ref.get("expected_kind") == "source_image":
                    bench.validate_source_image(ref, doc["original_sha256"], doc.get("page_count"))
        elif kind != METADATA:
            pages = {p["page"]: p for p in (doc["pages"] if kind in (CONTROLS, CHALLENGES) else [doc])}
            def append(check, page, expected, region, suffix, scope=None, annotation=None):
                image = pages.get(page, {})
                viewed = image.get("visually_viewed") is True if kind == CHALLENGES else image.get("personally_viewed") is True if kind == CONTROLS else image.get("visually_readable_in_scope") is True
                bench.need(viewed and bench.digest(image.get("image_sha256")) and image.get("image_path"),
                           "reference lacks personally reviewed source image binding")
                refs.append({"check_id": pin["sha256"][:16] + ":" + suffix,
                             "type": check, "page": page, "locator": "source page " + str(page) + ", " + region,
                             "content_scope": scope or ("body" if region in BODY_REGIONS else "other"),
                             "normalization": "nfc_whitespace", "expected": expected,
                             "review": {"status": "verified", "method": "independent_page_review",
                                        "reviewer": bundle["reviewer"], "evidence_uri": image["image_path"],
                                        "image_sha256": image["image_sha256"], "source_review_sha256": pin["sha256"],
                                        "scope_annotation": annotation,
                                        "original_sha256": doc["original_sha256"]}})
            if kind == MAIN:
                for index, text in enumerate(doc["neutral_checks"]["snippets"]):
                    append("snippet", doc["page"], text, "scope_unestablished", "snippet:" + str(index))
                anchors = doc["neutral_checks"].get("reading_order", [])
                if len(anchors) >= 2:
                    append("ordered_anchors", doc["page"], [a["anchor"] for a in anchors],
                           "scope_unestablished", "order")
            else:
                for index, passage in enumerate(doc.get("body_passages", [])):
                    append("snippet", passage["page"], passage["text"], passage.get("region", passage.get("section", "scope_unestablished")),
                           "snippet:" + str(index), passage.get("scope") if kind == CHALLENGES else passage.get("content_scope"), passage.get("scope_annotation"))
                for index, order in enumerate(doc.get("reading_order" if kind == CHALLENGES else "reading_order_anchors", [])):
                    expected = anchors_from_source(order["anchors"]) if "anchors" in order else [order["first"], order["then"]] if all(
                        isinstance(order.get(key), str) and order[key].strip() for key in ("first", "then")) else None
                    if expected is None:
                        unadapted.append({"reason": "UNADAPTED_READING_ORDER_EXPECTATIONS",
                                          "status": "NOT_A_DECLARED_CHECK", "record": copy.deepcopy(order)})
                        continue
                    body = order.get("first_region") in BODY_REGIONS and order.get("then_region") in BODY_REGIONS
                    append("ordered_anchors", order["page"], expected,
                           "body" if body else "scope_unestablished", "order:" + str(index), order.get("content_scope"), order.get("scope_annotation"))
                    if "anchors" in order:
                        refs[-1]["locator"] += "; source-location labels: " + " | ".join(value.partition(":")[0] for value in order["anchors"])
                        refs[-1]["review"]["raw_source_anchors"] = copy.deepcopy(order["anchors"])
        for ref in refs:
            if ref["type"] in bench.QUALITY_CHECKS:
                ref["normalization"] = "nfc_whitespace"
        # Generic formula_table_source_notes are observations, never exact expectations.
        result.append({"document_id": doc["document_id"], "sha256": doc["original_sha256"],
                       "references": refs, "page_count": doc.get("page_count") if kind == CHALLENGES or doc.get("pdfinfo_exit_code") == 0 else None,
                       "saved_pdf_visible_version": doc.get("saved_pdf_visible_version"),
                       "unadapted": unadapted, "pin": pin})
    return result


def build(selection_input, transfer_inputs, review_inputs, annotation_input=None, derive=False):
    selection, selection_pin = selection_input
    bench.need(not (derive and annotation_input), "choose derived or supplied source annotations")
    if derive:
        annotation_input = (derive_annotations(selection_input, review_inputs), None)
    annotations, annotation_pin = annotation_input or ({"schema": ANNOTATIONS, "extractor_outputs_seen": False, "documents": []}, None)
    bench.need(selection.get("protocol", {}).get("selection_before_extractor_results") is True,
               "pre-extraction selection required")
    bench.need(annotations.get("schema") == ANNOTATIONS, "source annotations schema required")
    source_only(annotations)
    selected = selection["candidates"]
    bench.need(selected and all(row.get("cohort") in ("cached_pdf_challenge", "html_potential_control") for row in selected),
               "unsupported selection cohort")
    ids = {row["document_id"] for row in selected}
    transfers = defaultdict(list)
    inputs = {"selection": selection_pin, "annotations": annotation_pin, "transfers": [], "reviews": []}
    for bundle, pin in transfer_inputs:
        bench.need(bundle.get("selection_sha256") == selection_pin["sha256"], "transfer belongs to different selection")
        inputs["transfers"].append(pin)
        for row in bundle["documents"]:
            bench.need(row["document_id"] in ids, "foreign transfer document")
            if row.get("kind", "pdf") == "pdf":
                transfers[row["document_id"]].append((row, bundle.get("content_hashes_verified") is True, pin))
    inputs["reviews"] = [pin for bundle, pin in review_inputs]
    bench.need(len({pin["sha256"] for pin in inputs["reviews"]}) == len(review_inputs), "duplicate source review input")
    supplied_pins = {(pin["path"], pin["sha256"]) for _, pin in review_inputs}
    for bundle, pin in review_inputs:
        if bundle.get("schema") == EXACT:
            bench.need(bundle.get("selection_sha256", selection_pin["sha256"]) == selection_pin["sha256"],
                       "gold review belongs to different selection")
            for source in bundle.get("source_inputs", []):
                bench.need((str(bench.local(source["path"])), source["sha256"]) in supplied_pins,
                           "gold source input pin missing/mismatched")
    reviews = defaultdict(list)
    for bundle, pin in scoped_reviews(review_inputs):
        if bundle.get("schema") == CHALLENGES:
            bench.need(bundle.get("selection_sha256") == selection_pin["sha256"], "challenge review belongs to different selection")
        for row in adapt_review(bundle, pin):
            bench.need(row["document_id"] in ids, "foreign source review document")
            reviews[row["document_id"]].append(row)
    labels = {}
    for row in annotations["documents"]:
        bench.need(row["document_id"] in ids and row["document_id"] not in labels, "foreign/duplicate annotation")
        bench.need(row.get("status") == "verified" and row.get("reviewer") and row.get("evidence_uri"), "source class evidence required")
        bench.need(isinstance(row.get("classes"), list) and row["classes"] and
                   len(set(row["classes"])) == len(row["classes"]) and set(row["classes"]) <= set(bench.CLASSES), "explicit valid classes required")
        labels[row["document_id"]] = row
    docs, gaps = [], []
    for chosen in selected:
        identity = chosen["document_id"]
        exceptions, references, unadapted = [], [], []
        transfer = transfers[identity]
        if transfer:
            row, verified, pin = transfer[0]
            bench.need(all(bench.encoded(t[0]) == bench.encoded(row) and t[1] == verified for t in transfer), "conflicting raw transfer")
            sha = row.get("sha256")
            bench.need(sha is None or bench.digest(sha), "invalid transfer SHA")
            path = str(Path(pin["path"]).parent / (sha + ".pdf")) if sha else None
            original = {"status": "available" if verified and sha else "unestablished", "path": path, "sha256": sha}
            uri = row.get("source_uri")
            if chosen["cohort"] == "cached_pdf_challenge":
                expected = chosen.get("source_record", {}).get("expected_sha256")
                bench.need(expected is None or expected == sha, "selected raw SHA differs from transfer")
        else:
            sha = chosen.get("source_record", {}).get("expected_sha256") if chosen["cohort"] == "cached_pdf_challenge" else None
            review_shas = {r["sha256"] for r in reviews[identity]}
            bench.need(len(review_shas) <= 1, "conflicting review originals")
            sha = sha or next(iter(review_shas), None)
            uri = chosen.get("source_uri") if chosen["cohort"] == "cached_pdf_challenge" else chosen.get("pdf_url_candidate")
            original = {"status": "missing", "path": None, "sha256": sha}
        if original["status"] != "available":
            exceptions.append("MISSING_OR_UNVERIFIED_RAW_PDF_TRANSFER")
        label = labels.get(identity)
        if label:
            bench.need(label.get("original_sha256") == sha and bench.digest(sha), "annotation original SHA mismatch")
        else:
            exceptions.append("MISSING_SOURCE_CLASS_ANNOTATION")
        counts, versions = set(), set()
        for review in reviews[identity]:
            bench.need(review["sha256"] == sha and bench.digest(sha), "review original SHA mismatch")
            references.extend(review["references"])
            unadapted.extend(review["unadapted"])
            if review["page_count"] is not None:
                counts.add(review["page_count"])
            observed_version = review["saved_pdf_visible_version"]
            if isinstance(observed_version, str) and observed_version.strip() and observed_version.upper() != "UNKNOWN":
                versions.add(observed_version)
        bench.need(len(counts) <= 1, "conflicting source page counts")
        bench.need(len(versions) <= 1, "conflicting saved PDF visible versions")
        if not references:
            exceptions.append("MISSING_INDEPENDENT_SOURCE_REFERENCES")
        minimum_checks = annotations.get("frozen_gates", {}).get("min_reviewed_checks_per_document")
        if minimum_checks is not None and sum(ref["type"] in bench.QUALITY_CHECKS for ref in references) < minimum_checks:
            exceptions.append("INSUFFICIENT_PREDECLARED_TEXTUAL_SOURCE_CHECKS")
        exceptions.extend(sorted({item["reason"] for item in unadapted if item.get("status") != "NOT_A_DECLARED_CHECK"}))
        classes = label["classes"] if label else []
        required_types = {bench.CLASS_CHECK[c] for c in classes} | {bench.SEMANTIC_CHECK[c] for c in classes if c in bench.SEMANTIC_CHECK}
        present_types = {ref["type"] for ref in references}
        for kind in sorted(required_types - present_types):
            exceptions.append("MISSING_REQUIRED_SOURCE_REFERENCE:" + kind)
        version = copy.deepcopy(label.get("source_version", state(next(iter(versions), None)))) if label else state(next(iter(versions), None))
        bench.evidence_field(version)
        bench.need(not versions or version["status"] != "established" or version["value"] in versions,
                   "annotation conflicts with saved PDF visible version")
        doc = {"document_id": identity, "cohort": chosen["cohort"], "classes": classes,
               "class_provenance": copy.deepcopy(label.get("class_provenance")) if label else None,
               "source_uri": state(uri), "source_version": version, "historical_original_text_edition": state(),
               "page_count": state(next(iter(counts), None)), "real_document": state(True) if original["status"] == "available" else state(),
               "source_kind": "arxiv_pdf" if chosen["cohort"] == "html_potential_control" else "other_pdf",
               "original": original, "references": references, "unadapted_source_observations": unadapted, "exceptions": exceptions}
        if chosen["cohort"] == "html_potential_control":
            if "healthy" not in classes:
                exceptions.append("MISSING_EXPLICIT_CONTROL_COHORT_CLASS")
            control = {"status": "verified", "method": "independent_sampled_source_pages_review",
                       "reviewer": label["reviewer"] if label else None,
                       "evidence_uri": label["evidence_uri"] if label else None, "original_sha256": sha}
            if label and type(label.get("two_column")) is bool:
                control["two_column"] = label["two_column"]
            doc["healthy_control_review"] = control
            if not bench.healthy_reviewed(doc):
                control["status"] = "missing"
                exceptions.append("MISSING_TWO_TRUE_BODY_PAGES_OR_LAYOUT_REVIEW")
        docs.append(doc)
        gaps.extend({"document_id": identity, "reason": reason} for reason in exceptions)
    manifest = {"schema": bench.SCHEMA, "sampling": {"method": "import frozen source selection; no replacement",
                "selected_before_extraction": True, "success_independent": True,
                "inventory_sha256": selection["input_inventory"]["sha256"]}, "documents": docs,
                "preparation_inputs": inputs}
    # Unlabeled rows remain in a draft, but cannot silently become sealable.
    if all(doc["classes"] for doc in docs):
        bench.validate_manifest(manifest)
    unique = {doc["document_id"]: doc for doc in docs}
    bench.need(len({bench.encoded(doc) for doc in docs}) == len(unique), "conflicting duplicate selection rows")
    physical = {doc["original"]["sha256"] for doc in unique.values() if doc["original"]["sha256"]}
    cohorts = Counter(doc["cohort"] for doc in unique.values())
    target = len(unique) == 100 and len(physical) == 100 and cohorts == {"cached_pdf_challenge": 80, "html_potential_control": 20}
    declared_classes = sorted({c for doc in docs for c in doc["classes"]})
    return {"schema": "dtox-benchmark-preparation-1", "manifest": manifest, "manifest_sha256": bench.sha(bench.encoded(manifest)),
            "source_annotations": copy.deepcopy(annotations), "preparation_code_sha256": bench.file_sha(__file__),
            "frozen_gates": copy.deepcopy(annotations.get("frozen_gates")),
            "report": {"source_reference_ready": target and not gaps and bool(annotations.get("frozen_gates")) and not annotations.get("pending_source_reviews"),
                       "quality_measured": False, "control_scope": "checked passages/pages NOT wholepaper healthy",
                       "raw_binding_scope": "verified transfer receipts; no local PDF reads; harness must rehash originals at seal/preflight",
                       "gate_status": "NOT_EVALUATED_NO_EXTRACTION_OUTPUTS",
                       "rows": len(docs), "unique_document_ids": len(unique), "duplicate_id_rows": len(docs) - len(unique),
                       "known_physical_pdfs": len(physical), "physical_alias_ids": sum(bool(d["original"]["sha256"]) for d in unique.values()) - len(physical),
                       "cohorts": dict(cohorts), "target_100_80_20": target,
                       "observed_classes": declared_classes, "absent_classes": sorted(set(bench.CLASSES) - set(declared_classes)),
                       "class_document_counts": {kind: sum(kind in doc["classes"] for doc in unique.values()) for kind in bench.CLASSES},
                       "semantic_missing_documents": {kind: [doc["document_id"] for doc in unique.values() if kind in doc["classes"] and
                           not any(ref["type"] == check for ref in doc["references"])] for kind, check in bench.SEMANTIC_CHECK.items()},
                       "source_inventory_complete": target and all(doc["classes"] and doc["page_count"]["status"] == "established" for doc in docs),
                       "absent_class_status": "NOT_MEASURED_NOT_ACCEPTED",
                       "adapted_reference_records": sum(len(doc["references"]) for doc in docs),
                       "unadapted_reference_records": sum(len(doc["unadapted_source_observations"]) for doc in docs),
                       "gaps": gaps, "pending_source_reviews": copy.deepcopy(annotations.get("pending_source_reviews", []))}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", nargs=2, required=True)
    parser.add_argument("--transfer", nargs=2, action="append", required=True)
    parser.add_argument("--review", nargs=2, action="append", default=[])
    parser.add_argument("--annotations", nargs=2)
    parser.add_argument("--derive-annotations", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = build(load_pin(*args.selection), [load_pin(*p) for p in args.transfer],
                   [load_pin(*p) for p in args.review], load_pin(*args.annotations) if args.annotations else None, args.derive_annotations)
    bench.write_json(bench.local(args.output), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
