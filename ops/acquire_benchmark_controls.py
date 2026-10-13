"""One candidate-only acquisition attempt for each of 20 SHA-bound benchmark controls."""
import sys

sys.dont_write_bytecode = True

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import http.client
import os
from pathlib import Path
import re
import ssl
import stat
import time
import types

try:
    from ops import catalog_cached_originals as catalog
except ModuleNotFoundError:
    import catalog_cached_originals as catalog


REVIEWED_FETCHER_SHA256 = "de2e306d190a1bae0d721f571455d7f8828c66f23109967d22da9422901d4f55"
REVIEWED_EXPORTER_SHA256 = "2df18a3ff763346e25f2647cf85abce275dd5bdc9e8681e0666ab970d9b7a38c"
PDF_CAP = 32 * 1024 * 1024
SAVED = "candidate_saved_not_identity_accepted"


class SavedVerificationError(ValueError):
    pass


class IdentityError(ValueError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def read_bound(path, expected_sha256, limit):
    catalog.need(catalog.valid_sha(expected_sha256), "invalid_expected_sha256")
    path = catalog.safe_path(path)
    before = path.stat()
    catalog.need(stat.S_ISREG(before.st_mode) and before.st_size <= limit, "bound_file_type_or_limit")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
        catalog.need(catalog.fingerprint(os.fstat(handle.fileno())) == catalog.fingerprint(before), "bound_file_changed")
    catalog.need(len(raw) == before.st_size and catalog.fingerprint(path.stat()) == catalog.fingerprint(before),
                 "bound_file_changed")
    catalog.need(hashlib.sha256(raw).hexdigest() == expected_sha256, "bound_sha_mismatch:" + path.name)
    return raw, {"path": str(path), "sha256": expected_sha256, "bytes": len(raw)}


def controls_from_selection(plan):
    rows = plan.get("candidates") if type(plan) is dict else None
    catalog.need(type(rows) is list and len(rows) == 100 and all(type(row) is dict for row in rows), "selection_requires_100")
    for key in ("document_id", "queue_id"):
        values = [row.get(key) for row in rows]
        catalog.need(all(isinstance(value, str) and value for value in values)
                     and len(set(values)) == 100, "selection_duplicate_or_missing_" + key)
    catalog.need(all(type(row.get("pdf_acquisition_required")) is bool for row in rows), "invalid_acquisition_flag")
    controls = [row for row in rows if row["pdf_acquisition_required"] is True]
    catalog.need(len(controls) == 20, "selection_requires_20_controls")
    for row in controls:
        identifier, qid = row["document_id"], row["queue_id"]
        catalog.need(re.fullmatch(r"[0-9]{2}(?:0[1-9]|1[0-2])\.[0-9]{4,5}", identifier) is not None,
                     "numeric_unversioned_arxiv_id_required")
        catalog.need(row.get("pdf_url_candidate") == "https://arxiv.org/pdf/" + identifier, "noncanonical_pdf_url")
        catalog.need(row.get("source_uri") == "https://arxiv.org/html/" + identifier, "noncanonical_bound_html_url")
        source, original = row.get("source_record", {}), row.get("inventory_record", {})
        provenance = original.get("status_provenance") or {}
        catalog.need(row.get("cohort") == "html_potential_control" and source.get("kind") == "html"
                     and source.get("scope") == "saved_original" and source.get("eligible_for_sealing") is True
                     and source.get("declared_binding_consistent") is True
                     and catalog.valid_sha(source.get("expected_sha256"))
                     and type(source.get("size")) is int and source["size"] > 0
                     and source["size"] == source.get("declared_bytes")
                     and isinstance(source.get("original_path"), str) and source["original_path"]
                     and original.get("canonical_id") == identifier and original.get("queue_id") == qid
                     and original.get("identity_status") == "matched" and original.get("aliases") == [qid]
                     and not original.get("errors") and not original.get("duplicate_document_of")
                     and original.get("is_document_representative") is True
                     and original.get("receipt_source_url") == row["source_uri"]
                     and original.get("representative_source") == source["original_path"]
                     and source in original.get("originals", [])
                     and provenance.get("status") == "NO_VERIFIED_TEXT_CHANGE"
                     and provenance.get("sha256_verified") is True, "control_html_binding_or_status_invalid")
    return controls


def load_fetcher(path, fetcher_sha256, exporter_sha256):
    catalog.need(fetcher_sha256 == REVIEWED_FETCHER_SHA256 and exporter_sha256 == REVIEWED_EXPORTER_SHA256,
                 "unreviewed_fetcher_or_exporter_pin")
    path = catalog.safe_path(path)
    catalog.need(path.name == "batch_repair_v1.py", "unexpected_fetcher_filename")
    exporter = path.with_name("export_repair_sources_v1.py")
    fetcher_raw, fetcher_binding = read_bound(path, fetcher_sha256, catalog.SMALL_LIMIT)
    exporter_raw, exporter_binding = read_bound(exporter, exporter_sha256, catalog.SMALL_LIMIT)
    sibling = types.ModuleType("export_repair_sources_v1")
    sibling.__file__ = str(exporter)
    module = types.ModuleType("_dtox_benchmark_pinned_fetcher")
    module.__file__ = str(path)
    previous = sys.modules.get(sibling.__name__)
    try:
        exec(compile(exporter_raw, str(exporter), "exec"), sibling.__dict__)
        sys.modules[sibling.__name__] = sibling
        exec(compile(fetcher_raw, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(sibling.__name__, None)
        else:
            sys.modules[sibling.__name__] = previous
    catalog.need(module.PDF_CAP == PDF_CAP, "fetcher_cap_mismatch")
    return module.Fetcher(), {"fetcher": fetcher_binding, "exporter": exporter_binding}


def saved_file(directory):
    path = catalog.safe_path(directory / "original.pdf", directory)
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > PDF_CAP:
        raise SavedVerificationError("saved_file_type_or_cap")
    with path.open("rb") as handle:
        raw = handle.read(PDF_CAP + 1)
        unchanged = catalog.fingerprint(os.fstat(handle.fileno())) == catalog.fingerprint(before)
    if not unchanged or len(raw) != before.st_size or catalog.fingerprint(path.stat()) != catalog.fingerprint(before):
        raise SavedVerificationError("saved_file_changed")
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "pdf_header_present": raw.startswith(b"%PDF-")}


def failure_status(error, receipt, url):
    requests = receipt.get("requests", [])
    if not isinstance(requests, list) or any(type(r) is not dict or r.get("url") != url for r in requests):
        return "identity_unresolved"
    message = str(error).lower()
    if isinstance(error, (IdentityError, ssl.SSLCertVerificationError)) or any(
            text in message for text in ("redirect", "unsafe url", "public https", "nonpublic dns")):
        return "identity_unresolved"
    if isinstance(error, SavedVerificationError):
        return "acquisition_error"
    status = requests[-1].get("status") if requests else None
    if status == 404:
        return "acquisition_pending"
    if status == 429 or type(status) is int and 500 <= status <= 599:
        return "temporarily_unavailable"
    if isinstance(error, (OSError, http.client.HTTPException)):
        return "temporarily_unavailable"
    if type(status) is int and status != 200:
        return "acquisition_pending"
    if isinstance(error, ValueError) and any(text in message for text in (
            "truncated source", "source cap", "source too large", "did not deliver pdf", "status/encoding")):
        return "acquisition_pending"
    return "acquisition_error"


def acquire_one(fetcher, item, directory, selection_sha256):
    url = item["pdf_url_candidate"]
    receipt = {"utc_started": utc(), "document_id": item["document_id"], "queue_id": item["queue_id"],
               "selection_sha256": selection_sha256, "selection_record": item, "attempts": 1, "requests": [],
               "edition": "UNKNOWN", "version_known": None, "historical_identity_proven": False,
               "identity_accepted": False, "source_available": False, "healthy_status": "unreviewed_not_proven"}
    try:
        target = fetcher.fetch(url, "pdf", directory, receipt)
        if target != directory / "original.pdf":
            raise SavedVerificationError("missing_or_unexpected_fetcher_output")
        saved = saved_file(directory)
        source = receipt.get("source") or {}
        if source.get("url") != url or any(r.get("url") != url for r in receipt["requests"]):
            raise IdentityError("source_or_request_identity_url_mismatch")
        if (not saved or not saved["pdf_header_present"] or not saved["bytes"]
                or source.get("kind") != "pdf" or source.get("bytes") != saved["bytes"]
                or source.get("sha256") != saved["sha256"] or not receipt["requests"]
                or receipt["requests"][-1].get("status") != 200):
            raise SavedVerificationError("missing_saved_output_or_sha_size_pdf_status_mismatch")
        receipt.update(status=SAVED, saved_file=saved, saved_sha256_verified=True)
    except Exception as error:
        receipt.update(status=failure_status(error, receipt, url), saved_sha256_verified=False,
                       error={"type": type(error).__name__, "message": str(error)})
        try:
            receipt["retained_file"] = saved_file(directory)
        except Exception as verification_error:
            receipt["retained_file_error"] = {"type": type(verification_error).__name__, "message": str(verification_error)}
            receipt["status"] = "acquisition_error"
    receipt["utc_finished"] = utc()
    return receipt


def durable_write(path, value):
    raw = catalog.encoded(value) + b"\n"
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(raw).hexdigest()


def progress(handle, **fields):
    handle.write(catalog.encoded({"utc": utc(), **fields}) + b"\n")
    handle.flush()
    os.fsync(handle.fileno())


def run(selection, selection_sha256, fetcher_path, fetcher_sha256, exporter_sha256, output):
    raw, selection_binding = read_bound(selection, selection_sha256, catalog.PLAN_LIMIT)
    controls = controls_from_selection(catalog.decode(raw))
    output = catalog.safe_path(output)
    catalog.need(not output.exists(), "output_must_be_new")
    fetcher, code_bindings = load_fetcher(fetcher_path, fetcher_sha256, exporter_sha256)
    output.mkdir(parents=True, exist_ok=False)
    (output / "documents").mkdir()
    preflight = {"utc": utc(), "selection": selection_binding, **code_bindings, "controls": controls,
                 "requested": 20, "attempts_per_document": 1, "outer_spacing_seconds": 3,
                 "pdf_cap_bytes": PDF_CAP, "request_timeout_seconds": 45,
                 "redirect_policy": "reviewed_Fetcher_same_origin_exact_identity_max_3_redirects_not_retries",
                 "production": False, "publication": False, "extractor_launched": False}
    preflight_sha = durable_write(output / "preflight.json", preflight)
    counts, results = Counter(), []
    with (output / "progress.jsonl").open("xb") as log:
        progress(log, event="start", requested=20, attempted=0, remaining=20, preflight_sha256=preflight_sha)
        for index, item in enumerate(controls):
            if index:
                time.sleep(3)
            directory = output / "documents" / hashlib.sha256(item["queue_id"].encode("utf-8")).hexdigest()
            directory.mkdir()
            progress(log, event="attempt_started", queue_id=item["queue_id"], attempt=1)
            receipt = acquire_one(fetcher, item, directory, selection_sha256)
            receipt_sha = durable_write(directory / "receipt.json", receipt)
            counts[receipt["status"]] += 1
            results.append({"document_id": item["document_id"], "queue_id": item["queue_id"], "status": receipt["status"],
                            "receipt": str((directory / "receipt.json").relative_to(output)), "receipt_sha256": receipt_sha})
            progress(log, event="attempt_finished", **results[-1], attempted=index + 1, remaining=19 - index, counts=dict(counts))
        summary = {"utc": utc(), "selection": selection_binding, **code_bindings, "preflight_sha256": preflight_sha,
                   "requested": 20, "attempted": len(results), "remaining": 20 - len(results),
                   "counts": dict(counts), "saved_candidates": counts[SAVED], "failed": 20 - counts[SAVED],
                   "all_attempts_recorded": len(results) == 20, "results": results,
                   "identity_accepted": 0, "source_available": 0, "proven_healthy": 0, "edition": "UNKNOWN",
                   "publication": False, "gates_passed": []}
        summary_sha = durable_write(output / "summary.json", summary)
        progress(log, event="end", attempted=len(results), remaining=summary["remaining"], counts=dict(counts), summary_sha256=summary_sha)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "selection-sha256", "fetcher-path", "fetcher-sha256", "exporter-sha256", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        summary = run(args.selection, args.selection_sha256, args.fetcher_path,
                      args.fetcher_sha256, args.exporter_sha256, args.output)
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(catalog.encoded({key: summary[key] for key in ("requested", "attempted", "saved_candidates", "failed", "counts")}).decode())
    return 0 if summary["saved_candidates"] == 20 else 2


if __name__ == "__main__":
    sys.exit(main())
