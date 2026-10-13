"""Select unsealed candidates from a SHA-bound catalog; never open original files."""
import argparse
from collections import Counter
import hashlib
import os
import stat
import sys
from urllib.parse import urlsplit

try:
    from ops import catalog_cached_originals as catalog
except ModuleNotFoundError:
    import catalog_cached_originals as catalog


DEFAULT_CONFIG = {"pdf_host_quotas": {"ACL": 44, "PMLR": 22, "OA": 7, "HAL": 5, "author": 2},
                  "html_controls": 20}
NAMESPACES = {"acl": "ACL", "pmlr": "PMLR", "oa": "OA", "hal": "HAL", "author": "author"}
MANDATORY = {"INPUT_OR_ANALYSIS_UNRESOLVED": 6, "STRUCTURAL_REEXTRACT_REQUIRED": 1}
PDF_STATUSES = {"NEEDS_GLYPH_VERIFICATION", *MANDATORY}


def read_inventory(path, expected_sha256):
    catalog.need(catalog.valid_sha(expected_sha256), "invalid_expected_inventory_sha256")
    path = catalog.safe_path(path)
    before = path.stat()
    catalog.need(stat.S_ISREG(before.st_mode) and before.st_size <= catalog.PLAN_LIMIT,
                 "inventory_type_or_limit")
    with path.open("rb") as handle:
        raw = handle.read(catalog.PLAN_LIMIT + 1)
        catalog.need(catalog.fingerprint(os.fstat(handle.fileno())) == catalog.fingerprint(before),
                     "inventory_changed")
    catalog.need(len(raw) == before.st_size
                 and catalog.fingerprint(path.stat()) == catalog.fingerprint(before), "inventory_changed")
    catalog.need(hashlib.sha256(raw).hexdigest() == expected_sha256, "inventory_sha_mismatch")
    rows = [catalog.decode(line) for line in raw.splitlines()]
    ids = [row.get("queue_id") if type(row) is dict else None for row in rows]
    catalog.need(all(isinstance(qid, str) and qid for qid in ids)
                 and len(ids) == len(set(ids)), "invalid_or_duplicate_queue_id")
    return rows, {"path": str(path), "sha256": expected_sha256, "bytes": len(raw)}


def candidate(row):
    if row.get("identity_status") != "matched" or row.get("errors"):
        return None, "identity_or_provenance_errors"
    if row.get("is_document_representative") is not True or row.get("duplicate_document_of"):
        return None, "alias_or_nonrepresentative"
    identity, qid = row.get("canonical_id"), row["queue_id"]
    provenance = row.get("status_provenance") or {}
    if not isinstance(identity, str) or not identity or provenance.get("sha256_verified") is not True:
        return None, "identity_or_status_unverified"
    sources = [source for source in row.get("originals", [])
               if source.get("kind") in {"pdf", "html"} and source.get("scope") == "saved_original"
               and source.get("eligible_for_sealing") is True
               and source.get("declared_binding_consistent") is True
               and not source.get("declared_duplicate_of")
               and catalog.valid_sha(source.get("expected_sha256"))
               and type(source.get("size")) is int and source["size"] > 0
               and source["size"] == source.get("declared_bytes")
               and source.get("original_path") == row.get("representative_source")]
    if len(sources) != 1:
        return None, "no_unique_bound_raw_original"
    source, status = sources[0], provenance.get("status")
    uri = row.get("receipt_source_url")
    try:
        catalog.need(bool(uri), "missing_receipt_source_uri")
        host = urlsplit(catalog.actual_url(uri)).hostname.lower()
    except (ValueError, TypeError, AttributeError):
        return None, "source_uri_unverified"
    group = NAMESPACES.get(identity.split(":", 1)[0])
    if source["kind"] == "pdf":
        if group is None:
            return None, "out_of_scope_pdf_namespace"
        if status not in PDF_STATUSES:
            return None, "out_of_scope_pdf_status"
        cohort, pdf_candidate = "cached_pdf_challenge", None
    else:
        pdf_candidate = catalog.candidate_url(identity)
        if (host != "arxiv.org" or not pdf_candidate or not pdf_candidate.startswith("https://arxiv.org/pdf/")
                or status != "NO_VERIFIED_TEXT_CHANGE"):
            return None, "out_of_scope_html_status_or_identity"
        if row.get("aliases") != [qid]:
            return None, "html_alias_not_control"
        cohort, group = "html_potential_control", None
    return {"document_id": identity, "queue_id": qid,
            "rank_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "cohort": cohort, "quota_group": group, "delivery_host": host,
            "source_uri": uri, "source_record": source, "inventory_record": row,
            "mandatory_status": status if source["kind"] == "pdf" and status in MANDATORY else None,
            "pdf_acquisition_required": source["kind"] == "html",
            "pdf_url_candidate": pdf_candidate, "pdf_url_status": "CANDIDATE" if pdf_candidate else None,
            "edition": "UNKNOWN", "healthy_status": "unreviewed_not_proven",
            "visual_review_required": True, "sealed": False, "quality_accepted": False}, None


def rank(item):
    return item["rank_sha256"], item["document_id"], item["queue_id"]


def select_candidates(rows, config=None):
    config = catalog.decode(catalog.encoded(DEFAULT_CONFIG if config is None else config))
    catalog.need(type(config) is dict and set(config) == {"pdf_host_quotas", "html_controls"}, "invalid_config")
    quotas = config["pdf_host_quotas"]
    catalog.need(type(quotas) is dict and set(quotas) == set(DEFAULT_CONFIG["pdf_host_quotas"])
                 and all(type(n) is int and n >= 0 for n in quotas.values()) and sum(quotas.values()) == 80
                 and type(config["html_controls"]) is int and config["html_controls"] == 20, "invalid_quotas")
    pool, excluded = [], Counter()
    for row in rows:
        item, reason = candidate(row)
        if item is None:
            excluded[reason] += 1
        else:
            pool.append(item)
    mandatory_counts = Counter(item["mandatory_status"] for item in pool if item["mandatory_status"])
    catalog.need(dict(mandatory_counts) == MANDATORY, "mandatory_pool_counts_mismatch")
    unique, seen_ids, seen_hashes = [], set(), set()
    for item in sorted(pool, key=lambda item: (not bool(item["mandatory_status"]), rank(item))):
        digest = item["source_record"]["expected_sha256"]
        if item["document_id"] in seen_ids or digest in seen_hashes:
            catalog.need(not item["mandatory_status"], "mandatory_duplicate_identity_or_content")
            excluded["duplicate_expected_content_or_identity"] += 1
            continue
        seen_ids.add(item["document_id"])
        seen_hashes.add(digest)
        unique.append(item)
    selected, pool_counts = [], {}
    for group in (*DEFAULT_CONFIG["pdf_host_quotas"], "arxiv_HTML"):
        eligible = [item for item in unique if (item["quota_group"] == group if group != "arxiv_HTML"
                    else item["cohort"] == "html_potential_control")]
        required = sorted((item for item in eligible if item["mandatory_status"]), key=rank)
        others = sorted((item for item in eligible if not item["mandatory_status"]), key=rank)
        quota = quotas[group] if group != "arxiv_HTML" else config["html_controls"]
        pool_counts[group] = len(eligible)
        catalog.need(len(required) <= quota <= len(eligible), "quota_unavailable:" + group)
        selected.extend(sorted(required + others[:quota - len(required)], key=rank))
        excluded["outside_rank_quota"] += len(eligible) - quota
    summary = {"input_rows": len(rows), "selected_count": len(selected), "cached_pdf_challenges": 80,
               "html_potential_controls": 20, "pdf_acquisition_needed": 20,
               "selected_pdf_by_quota_group": dict(quotas), "mandatory_pdf_counts": dict(mandatory_counts),
               "deduplicated_pool_by_group": pool_counts, "excluded_count": sum(excluded.values()),
               "excluded_by_reason": dict(sorted(excluded.items())),
               "out_of_scope_count": sum(n for key, n in excluded.items() if key.startswith("out_of_scope_")),
               "derived_source_rows": sum(bool(row.get("derived_sources")) for row in rows),
               "sealed_count": 0, "proven_healthy_count": 0, "quality_accepted": False, "gates_passed": []}
    catalog.need(len(selected) == 100 and summary["excluded_count"] + len(selected) == len(rows),
                 "selection_denominator_mismatch")
    return selected, summary, config


def run(inventory, expected_sha256, output, config_path=None):
    rows, binding = read_inventory(inventory, expected_sha256)
    config, config_sha = catalog.read_json(config_path) if config_path else (DEFAULT_CONFIG, None)
    selected, summary, config = select_candidates(rows, config)
    protocol = {"config": config, "config_origin": str(config_path) if config_path else "authorized_protocol_defaults",
                "config_sha256": config_sha or hashlib.sha256(catalog.encoded(config)).hexdigest(),
                "quota_basis": "canonical_source_namespace_not_delivery_host_or_feature_class",
                "rank_basis": "sha256(canonical_document_id_utf8)", "mandatory_pdf_status_counts": MANDATORY,
                "dedup_basis": ["canonical_id", "declared_expected_content_sha256"],
                "selection_before_extractor_results": True, "original_bytes_read": False,
                "original_content_hashes_verified": False, "pdf_control_editions_proven": False}
    selection = {"input_inventory": binding, "protocol": protocol, "candidates": selected}
    summary.update({"input_inventory": binding, "protocol": protocol})
    output = catalog.safe_path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("selection.json", selection), ("summary.json", summary)):
        with (output / name).open("xb") as handle:
            handle.write(catalog.encoded(value) + b"\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        summary = run(args.inventory, args.expected_sha256, args.output, args.config)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(catalog.encoded(summary).decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
