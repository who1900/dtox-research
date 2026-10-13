"""Sealed metadata plans and explicit local-only ORIGINAL adoption; no transport."""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import stat
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.originals import OriginalStore, Provenance, UNKNOWN, _plain, _regular, _sync_dir


RESERVE = 10 * 1024 ** 3
PLAN = "dtox-cached-original-backfill-plan-1"
BINDING = "dtox-local-original-mapping-1"


class BackfillError(ValueError):
    pass


def need(value, reason):
    if not value:
        raise BackfillError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate JSON key: " + key)
            out[key] = value
        return out
    def invalid(value):
        raise BackfillError("nonfinite JSON number: " + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def positive(value, name):
    need(type(value) is int and value > 0, "invalid " + name)
    return value


def source_path(value):
    need(isinstance(value, str) and value.startswith("/") and not value.startswith("//") and "\\" not in value
         and not any(ord(c) < 32 for c in value), "invalid source path")
    p = PurePosixPath(value)
    need(str(p) == value and all(x not in (".", "..") for x in p.parts),
         "noncanonical source path")
    return p


def metadata(path):
    p = _plain(path)
    s = p.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, "not a single-link regular file")
    return dict(dev=s.st_dev, ino=s.st_ino, size=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)


def identity():
    return {"hostname": socket.gethostname(), "platform": sys.platform}


def directory_identity(path):
    p = _plain(path)
    s = p.lstat()
    need(stat.S_ISDIR(s.st_mode), "not a plain directory")
    return {"dev": s.st_dev, "ino": s.st_ino}


def read_bound(path, expected, maximum):
    positive(maximum, "JSON byte cap")
    with _regular(path) as f:
        raw = f.read(maximum + 1)
    need(len(raw) <= maximum and sha(raw) == expected, "input byte cap/SHA mismatch")
    return decode(raw)


def write_new(path, value):
    path = _plain(path)
    raw = encoded(value) + b"\n"
    with path.open("xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    _sync_dir(path.parent)
    return sha(raw)


def payload(rows, inventory_sha256, inventory_bytes, root):
    root = source_path(root)
    need(str(root) != "/", "source root too broad")
    items, counts = [], Counter()
    for entry in rows:
        d = entry.get("row")
        entry["reasons"] = []
        if not isinstance(d, dict):
            entry["reasons"] = [entry.get("parse_error", "invalid row")]
            counts["invalid_row"] += 1
            continue
        need(sha(encoded(d)) == entry["row_sha256"], "canonical row SHA mismatch")
        counts[str(d.get("availability", "UNKNOWN"))] += 1
        originals = d.get("originals", [])
        if not isinstance(originals, list):
            entry["reasons"].append("invalid originals list")
            continue
        for index, original in enumerate(originals):
            item = {"item_id": f"{entry['line']}:{index}", "line": entry["line"],
                    "row_sha256": entry["row_sha256"], "original": original,
                    "aliases": d.get("aliases", []), "reasons": []}
            try:
                need(isinstance(original, dict), "invalid original")
                path = source_path(original.get("original_path"))
                relative = path.relative_to(root)
                need(str(relative) != ".", "original equals root")
                kind = {"pdf": "PDF", "html": "HTML", "xml": "XML",
                        "rawTexarchive": "rawTexarchive"}.get(original.get("kind"))
                need(kind is not None and original.get("scope", "saved_original") == "saved_original",
                     "not a raw original")
                need(kind != "rawTexarchive" or path.suffix.lower() != ".tex", "derived TeX is not raw")
                need(d.get("canonical_id") and d.get("identity_status") == "matched", "identity unresolved")
                need(isinstance(item["aliases"], list) and all(isinstance(x, str) for x in item["aliases"]),
                     "invalid aliases")
                positive(original.get("size"), "original size")
                need(original.get("declared_bytes") == original["size"], "declared size mismatch")
                fp = original.get("file_metadata")
                need(isinstance(fp, dict) and all(type(fp.get(k)) is int and fp[k] >= 0
                     for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns")), "incomplete file metadata")
                need(fp["size"] == original["size"], "metadata size mismatch")
                for obj in (original, fp):
                    need(not obj.get("is_symlink") and not obj.get("symlink") and not obj.get("reparse_point")
                         and not (obj.get("st_file_attributes", 0) & 0x400), "link metadata rejected")
                    need(obj.get("nlink", 1) == 1, "hardlink metadata rejected")
                    if "mode" in obj:
                        need(type(obj["mode"]) is int and stat.S_ISREG(obj["mode"]), "nonregular metadata")
                need(original.get("eligible_for_sealing") is True
                     and original.get("declared_binding_consistent") is True, "not eligible for byte verification")
                p = Provenance(d["canonical_id"], original.get("source_url") or d.get("source_url"),
                               original.get("retrieval_url") or UNKNOWN, kind,
                               original.get("edition") or d.get("historical_version") or UNKNOWN,
                               original.get("retrieved_at") or UNKNOWN, original.get("expected_sha256"))
                p.validate()
                item.update(source_path=str(path), relative_path=str(relative), bytes=original["size"],
                            expected_sha256=p.raw_sha256, provenance=p.__dict__,
                            declared_metadata=fp, content_verified=False)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                item["reasons"].append(str(exc))
            items.append(item)
        if not originals:
            entry["reasons"].append("no raw original; preserve availability/identity/errors")
    for key, description in (("source_path", "path binding conflict"),
                             ("expected_sha256", "hash size conflict")):
        groups = defaultdict(list)
        for item in items:
            if not item["reasons"]:
                groups[item[key]].append(item)
        for values in groups.values():
            signatures = {(v["expected_sha256"], v["bytes"]) if key == "source_path" else v["bytes"] for v in values}
            if len(signatures) > 1:
                for item in values:
                    item["reasons"].append(description)
    groups = defaultdict(list)
    for item in items:
        if not item["reasons"]:
            p = item["provenance"]
            groups[(p["canonical_id"], p["source_url"], p["original_kind"], p["edition"],
                    p["retrieval_url"], p["retrieved_at"])].append(item)
    for values in groups.values():
        if len({v["expected_sha256"] for v in values}) > 1:
            for item in values:
                item["reasons"].append("canonical provenance hash conflict")
    blobs = {}
    for item in items:
        if not item["reasons"]:
            b = blobs.setdefault(item["expected_sha256"], {"bytes": item["bytes"], "items": []})
            b["items"].append(item["item_id"])
    return {"inventory_sha256": inventory_sha256, "inventory_bytes": inventory_bytes,
            "source_root": str(root), "source_host_identity": UNKNOWN,
            "full_corpus_coverage": UNKNOWN, "rows": rows, "items": items, "blobs": blobs,
            "counts": {"rows": len(rows), "availability": dict(counts), "original_entries": len(items),
                       "eligible_items": sum(not i["reasons"] for i in items),
                       "quarantined_items": sum(bool(i["reasons"]) for i in items),
                       "expected_unique_blobs": len(blobs), "expected_unique_bytes": sum(b["bytes"] for b in blobs.values())}}


def plan(inventory, output, *, expected_sha256, source_root, max_bytes, max_rows):
    positive(max_bytes, "inventory byte cap")
    positive(max_rows, "row cap")
    rows, h, total = [], hashlib.sha256(), 0
    with _regular(inventory) as stream:
        while True:
            raw = stream.readline(max_bytes - total + 1)
            if not raw:
                break
            total += len(raw)
            need(total <= max_bytes and len(rows) < max_rows, "inventory bound exceeded")
            h.update(raw)
            entry = {"line": len(rows) + 1, "raw_line_sha256": sha(raw)}
            try:
                row = decode(raw)
                need(isinstance(row, dict), "row must be object")
                entry.update(row=row, row_sha256=sha(encoded(row)))
            except (ValueError, UnicodeError) as exc:
                entry.update(row=None, raw_line_hex=raw.hex(), parse_error=str(exc))
            rows.append(entry)
    need(h.hexdigest() == expected_sha256, "inventory SHA mismatch")
    data = payload(rows, expected_sha256, total, source_root)
    out = _plain(output)
    out.mkdir(mode=0o700)
    _sync_dir(out.parent)
    return write_new(out / "plan.json", {"schema": PLAN, "payload": data, "payload_sha256": sha(encoded(data))})


def sealed_plan(path, expected, maximum):
    envelope = read_bound(path, expected, maximum)
    need(envelope.get("schema") == PLAN, "wrong plan schema")
    p = envelope["payload"]
    need(sha(encoded(p)) == envelope["payload_sha256"], "plan seal mismatch")
    rebuilt = payload(p["rows"], p["inventory_sha256"], p["inventory_bytes"], p["source_root"])
    need(encoded(rebuilt) == encoded(p), "plan derivation mismatch")
    return p


def mapped_inputs(plan_data, receipt, store, limits):
    need(receipt.get("schema") == BINDING, "wrong mapping schema")
    need(receipt.get("source_root") == plan_data["source_root"], "source root binding mismatch")
    source = receipt.get("source_identity", {})
    need(isinstance(source, dict) and all(isinstance(source.get(k), str)
         and source[k].strip() and source[k] != UNKNOWN for k in ("host", "generation")),
         "source host/root identity UNKNOWN")
    source_mount = source_path(receipt.get("source_mount_root"))
    need(source_path(plan_data["source_root"]).is_relative_to(source_mount), "source mount/root mismatch")
    source_fp = receipt.get("source_mount_identity")
    need(isinstance(source_fp, dict) and all(type(source_fp.get(k)) is int and source_fp[k] >= 0
         for k in ("dev", "ino")), "source mount identity missing")
    need(receipt.get("target_identity") == identity(), "target host identity mismatch")
    need(type(receipt.get("other_reserved_bytes")) is int and receipt["other_reserved_bytes"] >= 0,
         "explicit other-reservations byte budget required")
    root = _plain(receipt["local_root"])
    mount = _plain(receipt["mount_root"])
    need(root.is_relative_to(mount) and directory_identity(mount) == receipt.get("target_mount_identity"),
         "target mount/root identity mismatch")
    directory_identity(root)
    need(not root.is_relative_to(store.root) and not store.root.is_relative_to(root), "CAS/input roots overlap")
    items = [i for i in plan_data["items"] if not i["reasons"]]
    need(len(items) <= limits["max_items"], "reference count cap exceeded")
    expected = {}
    for item in items:
        previous = expected.setdefault(item["relative_path"], (item["expected_sha256"], item["bytes"]))
        need(previous == (item["expected_sha256"], item["bytes"]), "conflicting mapped path")
    declared = {}
    for f in receipt["files"]:
        relative = f["relative_path"]
        need(isinstance(relative, str) and not PurePosixPath(relative).is_absolute()
             and str(PurePosixPath(relative)) == relative and ".." not in PurePosixPath(relative).parts
             and "\\" not in relative and ":" not in relative, "unsafe local relative path")
        need(relative not in declared and relative in expected, "extra/duplicate mapped file")
        need((f["expected_sha256"], f["metadata"]["size"]) == expected[relative], "mapped hash/size mismatch")
        declared[relative] = f["metadata"]
    need(set(declared) == set(expected), "mapped file set incomplete")
    actual, nodes = {}, 0
    def failed_walk(error):
        raise error
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
        for name in dirs:
            directory_identity(Path(parent) / name)
        for name in dirs + files:
            nodes += 1
            need(nodes <= limits["max_items"] * 128 + 1, "mapped tree entry cap exceeded")
        for name in files:
            path = _plain(Path(parent) / name)
            need(path.is_relative_to(root), "local path escape")
            need(len(actual) < limits["max_files"], "file count cap exceeded")
            actual[path.relative_to(root).as_posix()] = metadata(path)
    need(actual == declared, "actual mapped file set/metadata mismatch")
    need(all(n <= limits["max_file_bytes"] for _, n in expected.values())
         and sum(n for _, n in expected.values()) <= limits["max_total_bytes"], "source byte caps exceeded")
    return root, items, declared


@contextmanager
def locked(directory):
    path = _plain(directory / "run.lock")
    if not path.exists():
        with path.open("xb") as f:
            f.write(b"0")
            f.flush()
            os.fsync(f.fileno())
    with path.open("r+b") as f:
        need(metadata(path)["size"] == 1, "invalid journal lock")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def import_local(plan_path, receipt_path, output, store_root, *, plan_sha256,
                 receipt_sha256, max_json_bytes, limits, resume=False, retry_failed=False):
    for key in ("max_items", "max_files", "max_file_bytes", "max_total_bytes"):
        positive(limits[key], key)
    need(not retry_failed or resume, "retry requires explicit resume")
    p = sealed_plan(plan_path, plan_sha256, max_json_bytes)
    receipt = read_bound(receipt_path, receipt_sha256, max_json_bytes)
    need(receipt.get("plan_sha256") == plan_sha256 and receipt.get("inventory_sha256") == p["inventory_sha256"],
         "mapping is not bound to plan/inventory")
    store = OriginalStore(store_root)
    root, items, declared = mapped_inputs(p, receipt, store, limits)
    out = _plain(output)
    need(not out.is_relative_to(root) and not root.is_relative_to(out)
         and not out.is_relative_to(store.root) and not store.root.is_relative_to(out), "journal roots overlap")
    binding = {"plan_sha256": plan_sha256, "receipt_sha256": receipt_sha256,
               "store_root": str(store.root), "store_identity": directory_identity(store.root),
               "limits": limits, "reserve_bytes": RESERVE}
    if not resume:
        out.mkdir(mode=0o700)
        _sync_dir(out.parent)
        write_new(out / "bindings.json", binding)
        (out / "events").mkdir(mode=0o700)
        _sync_dir(out)
    else:
        need(out.is_dir(), "resume journal missing")
        with _regular(out / "bindings.json") as f:
            need(decode(f.read(max_json_bytes + 1)) == binding, "resume binding changed")
    with locked(out):
        previous, events, completed = None, [], {}
        for path in sorted((out / "events").iterdir()):
            need(path.name == f"{len(events):08d}.json", "journal sequence gap/extra file")
            with _regular(path) as f:
                raw = f.read(max_json_bytes + 1)
            need(len(raw) <= max_json_bytes, "journal event too large")
            event = decode(raw)
            need(event["previous_sha256"] == previous and event["binding_sha256"] == sha(encoded(binding)),
                 "journal chain/binding mismatch")
            if event["status"] == "INGESTED":
                completed[event["item_id"]] = event
            previous = sha(raw)
            events.append(event)
        need(not events or events[-1]["status"] not in ("FAILED", "BLOCKED") or retry_failed,
             "failed attempt requires --retry-failed")
        def append(value):
            nonlocal previous
            value.update(previous_sha256=previous, binding_sha256=sha(encoded(binding)))
            need(shutil.disk_usage(out).free >= RESERVE + receipt["other_reserved_bytes"]
                 + len(encoded(value)) + 1, "fresh journal disk reserve failed; prior evidence retained")
            previous = write_new(out / "events" / f"{len(events):08d}.json", value)
            events.append(value)
        try:
            missing_bytes = 0
            for digest, blob in p["blobs"].items():
                target = store.blob_path(digest)
                if target.exists():
                    store.verify_blob(digest, blob["bytes"])
                else:
                    missing_bytes += blob["bytes"]
            largest = max((i["bytes"] for i in items), default=0)
            need(shutil.disk_usage(store.root).free >= RESERVE + receipt["other_reserved_bytes"] + missing_bytes
                 + 2 * largest + max_json_bytes,
                 "fresh CAS disk reserve/admission failed")
            need(shutil.disk_usage(out).free >= RESERVE + max_json_bytes, "journal disk reserve failed")
        except Exception as exc:
            append({"status": "BLOCKED", "error_type": type(exc).__name__, "error": str(exc)})
            raise
        known = {i["item_id"] for i in items}
        need(set(completed) <= known, "journal unknown item")
        for item in items:
            path = root / item["relative_path"]
            try:
                need(metadata(path) == declared[item["relative_path"]], "mapped input changed")
                if item["item_id"] in completed:
                    event = completed[item["item_id"]]
                    refs = store.references(item["provenance"]["canonical_id"])
                    need(any(r["id"] == event["original_id"] and all(r[k] == v for k, v in item["provenance"].items())
                             for r in refs), "completed reference missing/changed")
                    store.verify_blob(item["expected_sha256"], item["bytes"])
                    continue
                need(shutil.disk_usage(store.root).free >= RESERVE + receipt["other_reserved_bytes"]
                     + 2 * item["bytes"] + max_json_bytes,
                     "per-item CAS disk reserve failed")
                existing = store.blob_path(item["expected_sha256"]).exists()
                if existing:
                    store.verify_blob(item["expected_sha256"], item["bytes"])
                original_id = store.ingest_file(path, Provenance(**item["provenance"]),
                                                max_bytes=limits["max_file_bytes"])
                need(metadata(path) == declared[item["relative_path"]], "mapped input changed after ingest")
                append({"status": "INGESTED", "item_id": item["item_id"], "original_id": original_id,
                        "row_sha256": item["row_sha256"], "raw_sha256": item["expected_sha256"],
                        "bytes": item["bytes"], "existing_blob_verified": existing, "aliases": item["aliases"]})
                completed[item["item_id"]] = events[-1]
            except Exception as exc:
                append({"status": "FAILED", "item_id": item["item_id"],
                        "error_type": type(exc).__name__, "error": str(exc)})
                raise
        counts = {**p["counts"], "ingested_items": len(completed), "availability_promotions": 0,
                  "full_corpus_coverage": UNKNOWN}
        append({"status": "COMPLETE", "counts": counts})
        return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("plan")
    for name in ("inventory", "output", "inventory-sha256", "source-root"):
        build.add_argument("--" + name, required=True)
    for name in ("max-bytes", "max-rows"):
        build.add_argument("--" + name, type=int, required=True)
    ingest = sub.add_parser("import-local")
    for name in ("plan", "mapping-receipt", "output", "store", "plan-sha256", "receipt-sha256"):
        ingest.add_argument("--" + name, required=True)
    for name in ("max-json-bytes", "max-items", "max-files", "max-file-bytes", "max-total-bytes"):
        ingest.add_argument("--" + name, type=int, required=True)
    ingest.add_argument("--resume", action="store_true")
    ingest.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = plan(args.inventory, args.output, expected_sha256=args.inventory_sha256,
                      source_root=args.source_root, max_bytes=args.max_bytes, max_rows=args.max_rows)
    else:
        result = import_local(args.plan, args.mapping_receipt, args.output, args.store,
                              plan_sha256=args.plan_sha256, receipt_sha256=args.receipt_sha256,
                              max_json_bytes=args.max_json_bytes,
                              limits={k: getattr(args, k) for k in ("max_items", "max_files", "max_file_bytes", "max_total_bytes")},
                              resume=args.resume, retry_failed=args.retry_failed)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
