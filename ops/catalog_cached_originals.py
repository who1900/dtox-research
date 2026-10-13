"""Inventory r3-bound cached sources without reading original content or publishing.

inventory --plan FILE --output NEW_DIR [--ledger FILE]
export-missing --plan FILE --ledger FILE --expected-missing-count N --output NEW_DIR
Only small receipt/metadata files and optional ledger JSON payloads are hashed.
Original hashes remain declarations until an independent selected-source seal.
Assembled cached TeX is reported separately and never satisfies raw availability.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import urlsplit


SMALL_LIMIT = 1024 * 1024
PLAN_LIMIT = 64 * 1024 * 1024
RESULT_LIMIT = 32 * 1024 * 1024
AVAILABILITY = ("acquisition_pending", "temporarily_unavailable",
                "permanently_unavailable", "identity_unresolved", "source_available")


class InventoryError(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise InventoryError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            need(key not in value, "duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: need(False, "nonfinite_json"))


def safe_path(value, root=None):
    need(isinstance(value, (str, Path)), "invalid_path")
    text = str(value)
    need(bool(text) and "\x00" not in text and not text.startswith(("\\\\", "//")), "unsafe_path")
    path = Path(text)
    need(".." not in path.parts and (os.name == "nt" or "\\" not in text), "unsafe_path")
    path = path.absolute()
    need(not any(":" in part for part in path.parts[1:]), "unsafe_path")
    if root is not None:
        need(path.is_relative_to(root), "path_outside_root")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
             "linked_path")
    return path


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name != "nt" else None)


def read_json(path, binding=None, root=None, limit=SMALL_LIMIT):
    path = safe_path(path, root)
    before = path.stat()
    need(stat.S_ISREG(before.st_mode) and before.st_size <= limit, "small_file_limit_or_type")
    if binding is not None:
        need(type(binding) is dict and type(binding.get("bytes")) is int
             and binding["bytes"] == before.st_size and valid_sha(binding.get("sha256")),
             "invalid_small_file_binding")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        need(fingerprint(os.fstat(handle.fileno())) == fingerprint(before), "small_file_replaced")
        raw = handle.read(limit + 1)
        need(fingerprint(os.fstat(handle.fileno())) == fingerprint(before), "small_file_changed")
    need(len(raw) == before.st_size and fingerprint(path.stat()) == fingerprint(before), "small_file_changed")
    digest = hashlib.sha256(raw).hexdigest()
    if binding is not None:
        need(digest == binding["sha256"], "small_file_sha_mismatch")
    return decode(raw), digest


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def stem_for(queue_id):
    need(isinstance(queue_id, str) and ":" in queue_id, "invalid_queue_id")
    stem = queue_id.split(":", 1)[1]
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}", stem) is not None
         and ".." not in stem, "unsafe_stem")
    return stem


def actual_url(value):
    if value is None:
        return None
    need(isinstance(value, str) and not any(ord(c) < 32 for c in value), "invalid_source_url")
    parsed = urlsplit(value)
    need(parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
         and not parsed.password, "invalid_source_url")
    return value


def candidate_url(identifier):
    value = identifier.removeprefix("arxiv:")
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", value):
        return "https://arxiv.org/pdf/" + value
    if identifier.startswith("acl:"):
        return "https://aclanthology.org/" + identifier[4:] + ".pdf"
    return None


def roots_for(plan):
    need(type(plan) is dict and type(plan.get("inputs")) is dict, "invalid_plan_inputs")
    runtime = safe_path(plan["runtime"])
    baselines = safe_path(plan["baselines"])
    need(baselines.name == "fulltext_cache", "invalid_baseline_root")
    bundle = safe_path(plan["r2_provenance"]["bundle_path"])
    return runtime, bundle, baselines.parent / "latex_cache"


def binding_conflicts(inputs):
    paths = defaultdict(list)
    for qid, item in inputs.items():
        if type(item) is dict:
            for key in ("receipt", "metadata", "cached_tex"):
                binding = item.get(key)
                if type(binding) is dict and isinstance(binding.get("path"), str):
                    paths[os.path.normcase(binding["path"])].append(
                        (qid, (binding.get("sha256"), binding.get("bytes"))))
    return {qid for rows in paths.values() if len({repr(row[1]) for row in rows}) > 1 for qid, _ in rows}


def ledger_results(path, inputs):
    path = safe_path(path)
    need(path.is_file(), "ledger_missing")
    statuses, errors = {}, {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(results)")]
        need(columns == ["queue_id", "payload", "sha"], "unsupported_results_schema")
        for qid, size, payload, digest in conn.execute(
                "SELECT queue_id,length(payload),substr(payload,1,?),sha FROM results", (RESULT_LIMIT + 1,)):
            need(qid in inputs and qid not in statuses and qid not in errors, "ledger_scope_or_duplicate")
            try:
                need(type(payload) is bytes and size <= RESULT_LIMIT, "ledger_payload_type_or_limit")
                need(valid_sha(digest) and hashlib.sha256(payload).hexdigest() == digest, "ledger_sha_mismatch")
                value = decode(payload)
                need(type(value) is dict and value.get("queue_id") == qid
                     and isinstance(value.get("status"), str), "ledger_payload_identity")
                statuses[qid] = {"status": value["status"], "payload_sha256": digest,
                                 "path": str(path), "table": "results", "sha256_verified": True}
            except (ValueError, TypeError, UnicodeError) as exc:
                errors[qid] = str(exc)
    return statuses, errors


def declarations(receipt, kind):
    rows = []
    source = receipt.get("source", {})
    core = receipt.get("core_identity", {})
    core = core if type(core) is dict else {}
    candidates = [source, receipt.get("html_source", {})] + list(core.values())
    requests = receipt.get("requests", [])
    if type(requests) is list:
        candidates += [r for r in requests if type(r) is dict and r.get("status") == 200]
    for value in candidates:
        if type(value) is not dict:
            continue
        declared_kind = value.get("kind")
        is_kind = declared_kind in (("pdf", "seed_pdf") if kind == "pdf" else ("html",))
        if kind == "pdf" and value is core.get("pdf"):
            is_kind = True
        if kind == "html" and value is receipt.get("html_source"):
            is_kind = True
        if not is_kind:
            continue
        rows.append({"sha256": value.get("sha256", value.get("source_sha256")),
                     "bytes": value.get("bytes", value.get("source_bytes")),
                     "path": value.get("path"), "canonical_id": value.get("canonical_id")})
    return rows


def original_record(path, kind, declarations_, identity_ok, canonical_id):
    path = safe_path(path)
    record = {"kind": kind, "original_path": str(path), "size": None,
              "expected_sha256": None, "declared_bytes": None, "file_metadata": None,
              "verification_status": "unverified_binding", "eligible_for_sealing": False,
              "declared_binding_consistent": False,
              "usable": False, "content_sha256_verified": False, "version_known": None,
              "scope": "assembled_source_derived" if kind == "assembled_tex" else "saved_original"}
    hashes, sizes = set(), set()
    for value in declarations_:
        if value.get("path") is not None:
            need(safe_path(value["path"]) == path, "declared_original_path_mismatch")
        if value.get("canonical_id") is not None:
            need(value["canonical_id"] == canonical_id, "origin_identity_mismatch")
        if value.get("sha256") is not None:
            need(valid_sha(value["sha256"]), "invalid_origin_sha")
            hashes.add(value["sha256"])
        if value.get("bytes") is not None:
            need(type(value["bytes"]) is int and value["bytes"] > 0, "invalid_origin_size")
            sizes.add(value["bytes"])
    need(len(hashes) <= 1 and len(sizes) <= 1, "conflicting_origin_bindings")
    record["expected_sha256"] = next(iter(hashes), None)
    record["declared_bytes"] = next(iter(sizes), None)
    try:
        info = path.stat()
    except FileNotFoundError:
        record["verification_status"] = "missing"
        return record
    need(stat.S_ISREG(info.st_mode), "original_not_regular")
    record["size"] = info.st_size
    record["file_metadata"] = {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
                               "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
    if hashes and sizes:
        record["verification_status"] = "declared_binding_unsealed" if info.st_size in sizes else "size_mismatch"
        record["declared_binding_consistent"] = info.st_size in sizes and identity_ok
        record["eligible_for_sealing"] = record["declared_binding_consistent"] and kind in ("pdf", "html")
    return record


def inspect_row(qid, item, roots, aliases, conflict=False):
    runtime, bundle, latex = roots
    row = {"queue_id": qid, "aliases": aliases, "stem": None, "canonical_id": None,
           "version_known": None, "id_version": None, "historical_version": None,
           "source_url": None, "metadata_source_url": None, "receipt_source_url": None,
           "source_url_provenance": None, "candidate_source_url": None,
           "identity_status": "unresolved", "availability": "identity_unresolved",
           "old_r2_status": item.get("old_status") if type(item) is dict else None,
           "originals": [], "derived_sources": [], "representative_source": None,
           "representative_derived_source": None, "source_errors": [], "errors": []}
    receipt, metadata = {}, {}
    try:
        stem = stem_for(qid)
        row["stem"] = stem
        need(type(item) is dict, "invalid_input_record")
        need(not conflict, "conflicting_shared_binding")
        document = runtime / "documents" / hashlib.sha256(qid.encode()).hexdigest()
        for key, expected, root in (("metadata", bundle / stem / "metadata.json", bundle),
                                     ("receipt", document / "receipt.v1.json", runtime)):
            try:
                binding = item.get(key)
                need(type(binding) is dict, "missing_" + key + "_binding")
                need(safe_path(binding["path"], root) == expected, "unexpected_" + key + "_path")
                value, _ = read_json(expected, binding, root)
                need(type(value) is dict, "invalid_" + key)
                if key == "metadata":
                    metadata = value
                else:
                    receipt = value
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row["errors"].append({"where": key, "code": str(exc)})
        refs = metadata.get("metadata", [])
        need(metadata.get("stem") == stem and type(refs) is list and len(refs) == 1
             and type(refs[0]) is dict, "metadata_identity_not_unique")
        meta = refs[0]
        identifier = meta.get("arxiv_id")
        need(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", identifier)
             and ".." not in identifier and identifier.replace("/", "_").replace(":", "_") == stem,
             "metadata_identity_stem_mismatch")
        row.update(canonical_id=identifier, identity_status="matched")
        match = re.search(r"(v[1-9][0-9]*)$", identifier)
        row["id_version"] = match.group(1) if match else None
        row["candidate_source_url"] = candidate_url(identifier)
        row["source_url"] = actual_url(meta.get("source_url"))
        row["metadata_source_url"] = row["source_url"]
        if row["source_url"]:
            row["source_url_provenance"] = "bound_metadata.source_url"
        identity = receipt.get("identity", {})
        identity = identity if type(identity) is dict else {}
        if receipt:
            need(receipt.get("queue_id") == qid and receipt.get("stem") == stem, "receipt_queue_identity_mismatch")
            need(identity.get("arxiv_id") == identifier and identity.get("forward_mapping_verified") is True,
                 "receipt_forward_identity_unverified")
            if type(identity.get("metadata")) is dict:
                need(identity["metadata"].get("arxiv_id") == identifier, "receipt_metadata_identity_mismatch")
            row["source_errors"] = receipt.get("errors", [])
            source = receipt.get("source", {})
            if type(source) is dict and source.get("url"):
                row["source_url"] = actual_url(source["url"])
                row["receipt_source_url"] = row["source_url"]
                row["source_url_provenance"] = "bound_receipt.source.url"
        for kind in ("pdf", "html"):
            try:
                path = safe_path(document / ("original." + kind), runtime)
                declared = declarations(receipt, kind)
                if path.exists() or declared:
                    original = original_record(path, kind, declared, bool(receipt), identifier)
                    row["originals"].append(original)
                    if original["verification_status"] in ("size_mismatch", "missing"):
                        row["errors"].append({"where": kind, "code": "original_" + original["verification_status"]})
            except (OSError, ValueError, TypeError) as exc:
                row["errors"].append({"where": kind, "code": str(exc)})
        if "cached_tex" in item:
            try:
                binding = item["cached_tex"]
                need(type(binding) is dict, "invalid_cached_tex_binding")
                path = safe_path(binding["path"], latex)
                need(path == latex / stem / "source.tex", "unexpected_cached_tex_path")
                original = original_record(path, "assembled_tex", [binding], True, identifier)
                row["derived_sources"].append(original)
                if original["declared_binding_consistent"]:
                    row["representative_derived_source"] = original["original_path"]
                if original["verification_status"] in ("size_mismatch", "missing"):
                    row["errors"].append({"where": "cached_tex", "code": "original_" + original["verification_status"]})
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row["errors"].append({"where": "cached_tex", "code": str(exc)})
        eligible = [r for r in row["originals"] if r["eligible_for_sealing"]]
        if eligible:
            row["availability"] = "source_available"
            row["representative_source"] = eligible[0]["original_path"]
        else:
            requests = receipt.get("requests", [])
            temporary = any(type(r) is dict and type(r.get("status")) is int
                            and (r["status"] in (408, 429) or 500 <= r["status"] <= 599)
                            for r in requests) if type(requests) is list else False
            row["availability"] = "temporarily_unavailable" if temporary else "acquisition_pending"
    except (OSError, ValueError, TypeError, KeyError) as exc:
        row["errors"].append({"where": "record", "code": str(exc)})
        row["identity_status"] = "unresolved"
        row["availability"] = "identity_unresolved"
        row["representative_source"] = None
        row["representative_derived_source"] = None
        for original in row["originals"] + row["derived_sources"]:
            original["eligible_for_sealing"] = False
            original["declared_binding_consistent"] = False
    return row


def catalog(plan_path, output, *, ledger=None, export_missing=False, expected_missing_count=None):
    plan_path = safe_path(plan_path)
    plan, plan_sha = read_json(plan_path, limit=PLAN_LIMIT)
    roots = roots_for(plan)
    inputs = plan["inputs"]
    if "scope" in plan:
        need(plan["scope"].get("total") == len(inputs), "plan_scope_count_mismatch")
    need(not export_missing or ledger is not None and type(expected_missing_count) is int
         and expected_missing_count >= 0, "export_missing_requires_ledger_and_expected_count")
    statuses, ledger_errors = ledger_results(ledger, inputs) if ledger is not None else ({}, {})
    observed = sum(r["status"] == "CACHED_SOURCE_UNAVAILABLE" for r in statuses.values())
    output = safe_path(output)
    for root in (*roots, roots[2].parent):
        need(not output.is_relative_to(root), "output_inside_source_root")
    need(output.parent.is_dir(), "output_parent_missing")
    aliases = defaultdict(list)
    for qid in inputs:
        try:
            aliases[stem_for(qid)].append(qid)
        except ValueError:
            pass
    conflicts = binding_conflicts(inputs)
    counts = {key: Counter() for key in ("kind", "derived_kind", "availability", "identity", "errors")}
    seen_documents, seen_derived_documents, seen_hashes = {}, {}, {}
    written = errored = blocking = missing_receipts = derived_available = 0
    output.mkdir(mode=0o700, exist_ok=False)
    filename = "missing.jsonl" if export_missing else "originals.jsonl"
    with (output / filename).open("xb") as handle:
        for qid, item in inputs.items():
            try:
                alias = aliases[stem_for(qid)]
            except ValueError:
                alias = [qid]
            row = inspect_row(qid, item, roots, alias, qid in conflicts)
            row["status_provenance"] = statuses.get(qid)
            if ledger is not None and qid not in statuses:
                row["errors"].append({"where": "ledger", "code": ledger_errors.get(qid, "ledger_result_missing")})
            identifier = row["canonical_id"]
            row["duplicate_document_of"] = seen_documents.get(identifier) if identifier else None
            row["is_document_representative"] = False
            if identifier and row["representative_source"] and identifier not in seen_documents:
                seen_documents[identifier] = qid
                row["is_document_representative"] = True
            elif row["duplicate_document_of"]:
                row["representative_source"] = None
            row["duplicate_derived_document_of"] = seen_derived_documents.get(identifier) if identifier else None
            row["is_derived_document_representative"] = False
            if identifier and row["representative_derived_source"] and identifier not in seen_derived_documents:
                seen_derived_documents[identifier] = qid
                row["is_derived_document_representative"] = True
            elif row["duplicate_derived_document_of"]:
                row["representative_derived_source"] = None
            derived_available += any(r["declared_binding_consistent"] for r in row["derived_sources"])
            for original in row["originals"]:
                counts["kind"][original["kind"]] += 1
            for original in row["derived_sources"]:
                counts["derived_kind"][original["kind"]] += 1
            for original in row["originals"] + row["derived_sources"]:
                key = (original["expected_sha256"], original["declared_bytes"])
                original["declared_duplicate_of"] = seen_hashes.get(key) if key[0] and key[1] else None
                if key[0] and key[1] and key not in seen_hashes:
                    seen_hashes[key] = {"queue_id": qid, "path": original["original_path"]}
            counts["availability"][row["availability"]] += 1
            counts["identity"][row["identity_status"]] += 1
            errored += bool(row["errors"])
            row["provenance_complete"] = not row["errors"]
            row_blocking = any(error["code"] != "missing_receipt_binding" for error in row["errors"])
            blocking += row_blocking
            missing_receipts += any(error["code"] == "missing_receipt_binding" for error in row["errors"])
            for error in row["errors"]:
                counts["errors"][error["code"]] += 1
            missing = statuses.get(qid, {}).get("status") == "CACHED_SOURCE_UNAVAILABLE"
            if not export_missing or missing or row_blocking:
                row["export_reason"] = "actual_v3_cached_source_unavailable" if missing else (
                    "verification_error" if export_missing else "inventory")
                handle.write(encoded(row) + b"\n")
                written += 1
        handle.flush()
        os.fsync(handle.fileno())
    count_matches = expected_missing_count is None or observed == expected_missing_count
    summary = {"schema": "dtox_cached_original_inventory_v1", "utc": datetime.now(timezone.utc).isoformat(),
               "mode": "export-missing" if export_missing else "inventory", "plan_path": str(plan_path),
               "plan_sha256": plan_sha, "input_count": len(inputs), "rows_written": written,
               "excluded_count": len(inputs) - written, "rows_with_errors": errored,
               "rows_with_blocking_errors": blocking, "rows_missing_receipt_binding": missing_receipts,
               "provenance_complete": errored == 0,
               "unique_represented_documents": len(seen_documents),
               "unique_represented_derived_documents": len(seen_derived_documents),
               "derived_rows_available": derived_available,
               "counts_by_kind": dict(counts["kind"]),
               "counts_by_derived_kind": dict(counts["derived_kind"]),
               "counts_by_availability": {key: counts["availability"][key] for key in AVAILABILITY},
               "counts_by_identity": dict(counts["identity"]), "counts_by_error": dict(counts["errors"]),
               "ledger_path": str(safe_path(ledger)) if ledger is not None else None,
               "verified_v3_missing_count": observed if ledger is not None else None,
               "expected_missing_count": expected_missing_count, "missing_count_matches": count_matches,
               "complete": blocking == 0 and count_matches, "original_content_hashed": False,
               "raw_original_version_proven": False, "no_change_implies_healthy": False,
               "sealing_required": True, "publication": False}
    with (output / "summary.json").open("xb") as handle:
        handle.write(encoded(summary) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "export-missing"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--expected-missing-count", type=int)
    args = parser.parse_args(argv)
    if args.command == "export-missing" and (args.ledger is None or args.expected_missing_count is None):
        parser.error("export-missing requires --ledger and --expected-missing-count from actual progress")
    try:
        summary = catalog(args.plan, args.output, ledger=args.ledger,
                          export_missing=args.command == "export-missing",
                          expected_missing_count=args.expected_missing_count)
        print(encoded(summary).decode("utf-8"))
        return 0 if summary["complete"] else 2
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
