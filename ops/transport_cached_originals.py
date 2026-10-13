"""Bounded direct ORIGINAL stream transport; no landing tree or batch atomicity."""
import argparse
import importlib
from importlib.machinery import ModuleSpec
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import traceback
import types
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

backfill = OriginalStore = Provenance = None


PLAN_SHA256 = "99e626b0183cbb097ba4d8e403926df4bc0747b8146523382ebde652cb8527f8"
INVENTORY_SHA256 = "5b1f5cdf1be75174782c03a34db12615d9308deb3247c663ba76d74bf5993af5"
SOURCE_ROOT = "/opt/dtox-repair-20261010/runs/r2/runtime/documents"
TARGET_STORE = "/opt/dtox-source-recovery-20261012/originals-store-v1"
SOURCE_PIN = {"hostname": "vmi1530274.contaboserver.net",
              "boot_id": "17f957ba-f3d5-4755-9f33-0ffdd05573f8",
              "root_dev": 2051, "root_ino": 13807393}
TARGET_PIN = {"hostname": "claude", "boot_id": "f3022afa-aa43-46f7-9f96-2f67feb424ce",
              "store_dev": 2049, "store_ino": 813809}
SCHEMA = "dtox-cached-original-transport-1"
CHUNK = 1024 * 1024
MAX_ITEMS = 4708
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 8 * 1024 ** 3
MAX_WIRE = 8 * 1024 ** 3
MAX_PLAN = 64 * 1024 ** 2
MAX_JSON_FRAME = 1024 * 1024
MAX_CONFIG = 1024 * 1024
MAX_METADATA = 256 * 1024 ** 2
OPERATIONAL_RESERVE = 1024 ** 3
RESERVE = 10 * 1024 ** 3
SOURCE_HOST = "root@161.97.126.166"
TARGET_HOST = "ubuntu@92.5.127.67"
SSH = "C:/Windows/System32/OpenSSH/ssh.exe"
KEY = "C:/Users/User/.ssh/id_ed25519_claude_dtox"
HEADER = "header"
ITEM = "item"
TRAILER = "trailer"
TERMINAL = "terminal"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CODE_FILES = ("ops/__init__.py", "ops/backfill_cached_originals.py",
              "pipeline/__init__.py", "pipeline/originals.py")


class TransportError(ValueError):
    pass


def need(value, message):
    if not value:
        raise TransportError(message)


def load_dependencies():
    global backfill, OriginalStore, Provenance
    if backfill is None:
        root = Path(__file__).resolve().parents[1]
        # The deployment bundle contains pinned empty package initializers. The
        # namespace fallback is only for this repository's focused local tests.
        if not (root / "ops" / "__init__.py").is_file() or not (root / "pipeline" / "__init__.py").is_file():
            for name in ("ops", "pipeline"):
                package = types.ModuleType(name)
                package.__package__ = name
                package.__path__ = [str(root / name)]
                package.__spec__ = ModuleSpec(name, loader=None, is_package=True)
                package.__spec__.submodule_search_locations = package.__path__
                sys.modules[name] = package
            sys.modules.pop("ops.backfill_cached_originals", None)
            sys.modules.pop("pipeline.originals", None)
        backfill = importlib.import_module("ops.backfill_cached_originals")
        originals = importlib.import_module("pipeline.originals")
        need(Path(backfill.__file__).resolve() == root / "ops" / "backfill_cached_originals.py" and
             Path(originals.__file__).resolve() == root / "pipeline" / "originals.py",
             "project dependency resolved outside pinned bundle")
        OriginalStore, Provenance = originals.OriginalStore, originals.Provenance


def verify_code_pins(module_root, pins):
    need(isinstance(pins, dict) and set(pins) == set(CODE_FILES),
         "dependency-code pin set mismatch")
    root = Path(module_root).resolve(strict=True)
    for relative in CODE_FILES:
        path = root / relative
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            need(not current.is_symlink(), "symlink in pinned dependency path: " + relative)
        need(path.is_file(), "missing/nonregular pinned dependency: " + relative)
        need(hashlib.sha256(path.read_bytes()).hexdigest() == pins[relative],
             "dependency code SHA mismatch: " + relative)


def metadata_from_stat(value):
    need(stat.S_ISREG(value.st_mode) and value.st_nlink == 1,
         "source must be a single-link regular file")
    return {"dev": int(value.st_dev), "ino": int(value.st_ino), "size": int(value.st_size),
            "mtime_ns": int(value.st_mtime_ns), "ctime_ns": int(value.st_ctime_ns)}


def metadata_equal(actual, expected):
    return all(type(expected.get(k)) is int and actual.get(k) == expected[k]
               for k in ("dev", "ino", "size", "mtime_ns", "ctime_ns"))


def source_identity(root):
    s = os.stat(root, follow_symlinks=False)
    need(stat.S_ISDIR(s.st_mode), "source root is not a plain directory")
    return {"hostname": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "root_dev": int(s.st_dev), "root_ino": int(s.st_ino)}


def target_identity(root):
    s = os.stat(root, follow_symlinks=False)
    need(stat.S_ISDIR(s.st_mode), "CAS root is not a plain directory")
    return {"hostname": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "store_dev": int(s.st_dev), "store_ino": int(s.st_ino)}


def load_plan(path, expected_sha=PLAN_SHA256, *, max_bytes=MAX_PLAN):
    load_dependencies()
    p = backfill.sealed_plan(path, expected_sha, max_bytes)
    need(p["inventory_sha256"] == INVENTORY_SHA256, "unexpected inventory SHA")
    need(p["source_root"] == SOURCE_ROOT, "unexpected source root")
    items = [x for x in p["items"] if not x["reasons"]]
    need(len(items) == MAX_ITEMS and p["counts"]["eligible_items"] == MAX_ITEMS,
         "sealed plan eligible-item count mismatch")
    need(p["counts"]["expected_unique_bytes"] == 7445860488,
         "sealed plan unique-byte count mismatch")
    need(sum(x["bytes"] for x in items) == 7451842709, "sealed plan logical-byte count mismatch")
    need(sum(x["bytes"] for x in items) <= MAX_TOTAL, "payload cap exceeded")
    need(all(0 < x["bytes"] <= MAX_FILE for x in items), "per-file cap exceeded")
    for item in items:
        rel = PurePosixPath(item["relative_path"])
        need(not rel.is_absolute() and ".." not in rel.parts and str(rel) == item["relative_path"],
             "unsafe plan path")
    return p, items


def validate_config(c, role):
    need(c.get("schema") == SCHEMA and c.get("role") == role, "wrong config schema/role")
    need(c.get("plan_sha256") == PLAN_SHA256 and c.get("inventory_sha256") == INVENTORY_SHA256,
         "config plan pins mismatch")
    pins = c.get("code_sha256")
    need(isinstance(pins, dict) and set(pins) == set(CODE_FILES) and
         all(re.fullmatch(r"[0-9a-f]{64}", pins[name] if isinstance(pins[name], str) else "")
             for name in CODE_FILES), "dependency-code pin set/value mismatch")
    lim = c.get("limits", {})
    required = {"items": MAX_ITEMS, "file_bytes": MAX_FILE, "total_bytes": MAX_TOTAL,
                "wire_bytes": MAX_WIRE,
                "plan_bytes": MAX_PLAN, "json_frame_bytes": MAX_JSON_FRAME,
                "chunk_bytes": CHUNK, "metadata_bytes": MAX_METADATA,
                "operational_reserve_bytes": OPERATIONAL_RESERVE, "reserve_bytes": RESERVE}
    need(lim == required, "config limits must equal the frozen bounds")
    need(re.fullmatch(r"[0-9a-f]{32}", c.get("stream_id", "")) is not None,
         "fresh stream_id required")
    plan_path = c.get("plan_path")
    plan_absolute = (isinstance(plan_path, str) and
                     (Path(plan_path).is_absolute() if role == "relay"
                      else PurePosixPath(plan_path).is_absolute()))
    need(plan_absolute, "absolute sealed-plan path required for role: " + role)
    need(c.get("source", {}).get("root") == SOURCE_ROOT and
         c.get("source", {}).get("identity") == SOURCE_PIN, "source pin mismatch")
    need(c.get("target", {}).get("store_root") == TARGET_STORE and
         c.get("target", {}).get("identity") == TARGET_PIN, "target pin mismatch")
    need(type(c.get("other_reserved_bytes")) is int and
         c["other_reserved_bytes"] == OPERATIONAL_RESERVE, "operational reservation mismatch")
    job_root = c.get("target", {}).get("job_root")
    need(isinstance(job_root, str) and PurePosixPath(job_root).is_absolute(),
         "absolute target job root required")
    store_root = PurePosixPath(TARGET_STORE)
    job_path = PurePosixPath(job_root)
    need(job_path != store_root and not job_path.is_relative_to(store_root)
         and not store_root.is_relative_to(job_path), "job/CAS roots overlap")
    lock_dir = c.get("target", {}).get("lock_dir")
    need(isinstance(lock_dir, str) and PurePosixPath(lock_dir).is_absolute(),
         "absolute dedicated receiver lock directory required")
    lock_path = PurePosixPath(lock_dir)
    need(not lock_path.is_relative_to(store_root) and not store_root.is_relative_to(lock_path)
         and not lock_path.is_relative_to(job_path) and not job_path.is_relative_to(lock_path),
         "receiver lock/CAS/job roots overlap")
    if role == "relay":
        relay_cfg = c.get("relay", {})
        for key in ("source_config", "destination_config", "remote_module"):
            need(isinstance(relay_cfg.get(key), str) and PurePosixPath(relay_cfg[key]).is_absolute(),
                 "absolute relay path required: " + key)
        need(isinstance(relay_cfg.get("local_job_root"), str) and
             Path(relay_cfg["local_job_root"]).is_absolute(), "absolute local job root required")
        for key in ("send_config_sha256", "receive_config_sha256"):
            need(re.fullmatch(r"[0-9a-f]{64}", relay_cfg.get(key, "")) is not None,
                 "remote config SHA pin required: " + key)
    return c


def _source_path(root, relative):
    rel = PurePosixPath(relative)
    need(not rel.is_absolute() and ".." not in rel.parts and str(rel) == relative,
         "unsafe source-relative path")
    path = Path(root)
    for part in rel.parts[:-1]:
        path = path / part
        s = path.lstat()
        need(stat.S_ISDIR(s.st_mode), "source path parent is not a plain directory")
    return path / rel.parts[-1]


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        n = stream.write(view)
        need(n is not None and n > 0, "transport write failed")
        view = view[n:]


def write_frame(stream, value, limit=MAX_JSON_FRAME):
    raw = backfill.encoded(value)
    need(0 < len(raw) <= limit, "JSON frame cap exceeded")
    _write_all(stream, struct.pack("!I", len(raw)))
    _write_all(stream, raw)
    return len(raw) + 4


class BoundedInput:
    def __init__(self, stream, cap):
        self.stream, self.cap, self.count = stream, cap, 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = CHUNK
        size = min(size, CHUNK, self.cap - self.count + 1)
        data = self.stream.read(size)
        self.count += len(data)
        need(self.count <= self.cap, "wire byte cap exceeded")
        return data

    def exact(self, count):
        chunks = bytearray()
        while len(chunks) < count:
            data = self.read(count - len(chunks))
            need(data, "truncated stream")
            chunks.extend(data)
        return bytes(chunks)

    def frame(self, limit=MAX_JSON_FRAME):
        size = struct.unpack("!I", self.exact(4))[0]
        need(0 < size <= limit, "invalid JSON frame size")
        return backfill.decode(self.exact(size))


class WireOutput:
    def __init__(self, stream, cap):
        self.stream, self.cap, self.count = stream, cap, 0

    def write(self, data):
        self.count += len(data)
        need(self.count <= self.cap, "wire byte cap exceeded")
        _write_all(self.stream, data)
        return len(data)


def _assert_source_identity(cfg):
    root = cfg["source"]["root"]
    need(root == SOURCE_ROOT, "source root path changed")
    actual = source_identity(root)
    need(actual == cfg["source"]["identity"], "source host/boot/root identity mismatch")
    return actual


def send_all(cfg, plan, items, output):
    ident = _assert_source_identity(cfg)
    need(re.fullmatch(r"[0-9a-f]{64}", cfg.get("_config_sha256", "")) is not None and
         re.fullmatch(r"[0-9a-f]{64}", cfg.get("_peer_config_sha256", "")) is not None and
         re.fullmatch(r"[0-9a-f]{64}", cfg.get("_module_sha256", "")) is not None,
         "runtime code/config pins missing")
    wire = WireOutput(output, cfg["limits"]["wire_bytes"])
    write_frame(wire, {"kind": HEADER, "schema": SCHEMA, "stream_id": cfg["stream_id"],
                       "plan_sha256": PLAN_SHA256, "inventory_sha256": INVENTORY_SHA256,
                       "source_config_sha256": cfg["_config_sha256"],
                       "destination_config_sha256": cfg["_peer_config_sha256"],
                       "module_sha256": cfg["_module_sha256"],
                       "source_identity": ident, "item_count": len(items),
                       "logical_bytes": sum(x["bytes"] for x in items)})
    sent = 0
    for item in items:
        path = _source_path(SOURCE_ROOT, item["relative_path"])
        expected = item["declared_metadata"]
        before_path = metadata_from_stat(path.lstat())
        need(metadata_equal(before_path, expected), "source path metadata differs from sealed plan")
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", buffering=0) as source:
            before_fd = metadata_from_stat(os.fstat(source.fileno()))
            need(metadata_equal(before_fd, expected), "opened source differs from sealed plan")
            write_frame(wire, {"kind": ITEM, "item_id": item["item_id"],
                               "relative_path": item["relative_path"], "bytes": item["bytes"],
                               "expected_sha256": item["expected_sha256"], "metadata": before_fd})
            digest = hashlib.sha256()
            remaining = item["bytes"]
            while remaining:
                block = source.read(min(CHUNK, remaining))
                need(block, "source truncated while streaming")
                digest.update(block)
                wire.write(block)
                remaining -= len(block)
            after_fd = metadata_from_stat(os.fstat(source.fileno()))
            after_path = metadata_from_stat(path.lstat())
            need(after_fd == before_fd == after_path, "source changed while streaming")
            need(digest.hexdigest() == item["expected_sha256"], "source bytes differ from sealed SHA")
        need(_source_path(SOURCE_ROOT, item["relative_path"]) == path,
             "source path parent changed after read")
        backfill._plain(path)
        write_frame(wire, {"kind": TRAILER, "item_id": item["item_id"],
                           "bytes": item["bytes"], "sha256": digest.hexdigest(),
                           "metadata": after_fd})
        sent += item["bytes"]
    need(_assert_source_identity(cfg) == ident, "source identity changed during stream")
    terminal = {"kind": TERMINAL, "schema": SCHEMA, "stream_id": cfg["stream_id"],
                "plan_sha256": PLAN_SHA256, "inventory_sha256": INVENTORY_SHA256,
                "source_config_sha256": cfg["_config_sha256"],
                "destination_config_sha256": cfg["_peer_config_sha256"],
                "module_sha256": cfg["_module_sha256"],
                "sender_exit": 0, "item_count": len(items), "logical_bytes": sent}
    write_frame(wire, terminal)
    output.flush()
    return {"status": "SENT", "item_count": len(items), "logical_bytes": sent,
            "wire_bytes": wire.count}


class ValidatedBody:
    """Body reader withholds EOF until body hash and source trailer are validated."""
    def __init__(self, incoming, item, header):
        self.incoming, self.item, self.header = incoming, item, header
        self.remaining = item["bytes"]
        self.digest = hashlib.sha256()
        self.validated = False

    def read(self, size=-1):
        if self.validated:
            return b""
        if self.remaining:
            if size is None or size < 0:
                size = CHUNK
            data = self.incoming.read(min(size, CHUNK, self.remaining))
            need(data, "truncated file body")
            self.remaining -= len(data)
            self.digest.update(data)
            return data
        trailer = self.incoming.frame()
        expected = self.item
        need(trailer.get("kind") == TRAILER and trailer.get("item_id") == expected["item_id"],
             "missing/misbound file trailer")
        need(trailer.get("bytes") == expected["bytes"] and
             trailer.get("sha256") == expected["expected_sha256"] == self.digest.hexdigest(),
             "body/trailer/sealed-plan hash or size mismatch")
        need(metadata_equal(trailer.get("metadata", {}), self.header["metadata"]),
             "sender source metadata changed")
        self.validated = True
        return b""


def _create_job(path):
    path = backfill._plain(Path(path))
    need(not path.exists(), "receiver job directory already exists; no resume/retry")
    path.mkdir(mode=0o700)
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode), "new receiver job directory is not a directory")
    if os.name != "nt":
        need(stat.S_IMODE(info.st_mode) == 0o700, "new receiver job directory is not private")
    backfill._plain(path)
    backfill._sync_dir(path.parent)
    return path


def _missing_blobs(store, plan):
    missing = {}
    for digest, blob in plan["blobs"].items():
        target = store.blob_path(digest)
        if target.exists():
            store.verify_blob(digest, blob["bytes"])
        else:
            missing[digest] = blob["bytes"]
    return missing


def _check_budget(store, missing, items, completed, other_reserved, metadata_budget):
    missing_bytes = sum(missing.values())
    largest = max((x["bytes"] for x in items if x["item_id"] not in completed), default=0)
    need(shutil.disk_usage(store.root).free >= RESERVE + other_reserved + missing_bytes + largest + metadata_budget,
         "CAS free-space admission failed")


def _references_match(store, item, original_id):
    refs = store.references(item["provenance"]["canonical_id"])
    expected = item["provenance"]
    need(any(row["id"] == original_id and all(row.get(k) == v for k, v in expected.items())
             for row in refs), "committed provenance reference mismatch")


def _receive_locked(cfg, plan, items, incoming, store, job):
    limits = cfg["limits"]
    target = cfg["target"]
    need(str(store.root) == TARGET_STORE and target_identity(store.root) == target["identity"],
         "target host/boot/CAS identity mismatch")
    need(re.fullmatch(r"[0-9a-f]{64}", cfg.get("_config_sha256", "")) is not None and
         re.fullmatch(r"[0-9a-f]{64}", cfg.get("_peer_config_sha256", "")) is not None and
         re.fullmatch(r"[0-9a-f]{64}", cfg.get("_module_sha256", "")) is not None,
         "runtime code/config pins missing")
    job = _create_job(job)
    binding = {"schema": SCHEMA, "stream_id": cfg["stream_id"], "plan_sha256": PLAN_SHA256,
               "inventory_sha256": INVENTORY_SHA256, "target_identity": target["identity"],
               "source_config_sha256": cfg["_peer_config_sha256"],
               "destination_config_sha256": cfg["_config_sha256"],
               "module_sha256": cfg["_module_sha256"], "code_sha256": cfg["code_sha256"],
               "other_reserved_bytes": cfg["other_reserved_bytes"],
               "metadata_budget_bytes": limits["metadata_bytes"]}
    metadata_used = len(backfill.encoded(binding)) + 1
    need(metadata_used <= limits["metadata_bytes"], "metadata budget exhausted by binding")
    need(shutil.disk_usage(job).free >= RESERVE + cfg["other_reserved_bytes"] +
         limits["metadata_bytes"], "journal initial reserve failed")
    backfill.write_new(job / "bindings.json", binding)
    incoming = BoundedInput(incoming, limits["wire_bytes"])
    events = []
    previous = None
    done = {}
    active_item_id = None
    active_item_outcome = "NONE"

    def append(event):
        nonlocal previous, metadata_used
        event.update(previous_sha256=previous, binding_sha256=backfill.sha(backfill.encoded(binding)))
        raw = backfill.encoded(event) + b"\n"
        next_used = metadata_used + len(raw)
        need(next_used <= limits["metadata_bytes"], "receipt metadata budget exceeded")
        need(shutil.disk_usage(job).free >= RESERVE + cfg["other_reserved_bytes"] +
             limits["metadata_bytes"] - next_used, "journal free-space reserve failed")
        previous = backfill.write_new(job / "events" / f"{len(events):08d}.json", event)
        metadata_used = next_used
        events.append(event)

    def write_receipt(value):
        nonlocal metadata_used
        raw = backfill.encoded(value) + b"\n"
        next_used = metadata_used + len(raw)
        need(next_used <= limits["metadata_bytes"], "receipt metadata budget exceeded")
        need(shutil.disk_usage(job).free >= RESERVE + cfg["other_reserved_bytes"] +
             limits["metadata_bytes"] - next_used, "journal free-space reserve failed")
        backfill.write_new(job / "receipt.json", value)
        metadata_used = next_used

    (job / "events").mkdir(mode=0o700)
    backfill._plain(job / "events")
    backfill._sync_dir(job)
    with backfill.locked(job):
        try:
            missing = _missing_blobs(store, plan)
            _check_budget(store, missing, items, done, cfg["other_reserved_bytes"], limits["metadata_bytes"])
            header = incoming.frame(limits["json_frame_bytes"])
            need(header.get("kind") == HEADER and header.get("schema") == SCHEMA and
                 header.get("stream_id") == cfg["stream_id"], "invalid stream header")
            need(header.get("plan_sha256") == PLAN_SHA256 and
                 header.get("inventory_sha256") == INVENTORY_SHA256 and
                 header.get("source_config_sha256") == cfg["_peer_config_sha256"] and
                 header.get("destination_config_sha256") == cfg["_config_sha256"] and
                 header.get("module_sha256") == cfg["_module_sha256"] and
                 header.get("source_identity") == cfg["source"]["identity"], "header source/plan binding mismatch")
            need(header.get("item_count") == len(items) and
                 header.get("logical_bytes") == sum(x["bytes"] for x in items), "header totals mismatch")
            for item in items:
                _check_budget(store, missing, items, done, cfg["other_reserved_bytes"], limits["metadata_bytes"])
                framed = incoming.frame(limits["json_frame_bytes"])
                declared = {"kind": ITEM, "item_id": item["item_id"],
                            "relative_path": item["relative_path"], "bytes": item["bytes"],
                            "expected_sha256": item["expected_sha256"],
                            "metadata": item["declared_metadata"]}
                need(framed == declared, "per-item frame differs from sealed plan")
                reader = ValidatedBody(incoming, item, framed)
                # Store.stream must consume through validated EOF before committing.
                active_item_id = item["item_id"]
                active_item_outcome = "UNKNOWN"
                protected_floor = (RESERVE + cfg["other_reserved_bytes"] + limits["metadata_bytes"] +
                                   sum(n for digest, n in missing.items()
                                       if digest != item["expected_sha256"]))
                original_id = store.ingest_stream(
                    reader, Provenance(**item["provenance"]), max_bytes=MAX_FILE,
                    expected_bytes=item["bytes"],
                    source_name=PurePosixPath(item["relative_path"]).name,
                    reserve_bytes=protected_floor)
                active_item_outcome = "STORE_RETURNED_UNJOURNALED"
                need(reader.validated, "Store.stream returned before source proof/trailer")
                _references_match(store, item, original_id)
                event = {"status": "INGESTED", "item_id": item["item_id"],
                         "original_id": original_id, "raw_sha256": item["expected_sha256"],
                         "bytes": item["bytes"], "row_sha256": item["row_sha256"]}
                append(event)
                done[item["item_id"]] = event
                missing.pop(item["expected_sha256"], None)
                active_item_id = None
                active_item_outcome = "NONE"
            terminal = incoming.frame(limits["json_frame_bytes"])
            need(terminal == {"kind": TERMINAL, "schema": SCHEMA, "stream_id": cfg["stream_id"],
                              "plan_sha256": PLAN_SHA256, "inventory_sha256": INVENTORY_SHA256,
                              "source_config_sha256": cfg["_peer_config_sha256"],
                              "destination_config_sha256": cfg["_config_sha256"],
                              "module_sha256": cfg["_module_sha256"],
                              "sender_exit": 0, "item_count": len(items),
                              "logical_bytes": sum(x["bytes"] for x in items)},
                 "sender terminal/status/totals mismatch")
            relay_commit = incoming.frame(limits["json_frame_bytes"])
            need(relay_commit == {"kind": "relay_commit", "schema": SCHEMA,
                                  "stream_id": cfg["stream_id"], "plan_sha256": PLAN_SHA256,
                                  "source_config_sha256": cfg["_peer_config_sha256"],
                                  "destination_config_sha256": cfg["_config_sha256"],
                                  "module_sha256": cfg["_module_sha256"],
                                  "sender_returncode": 0, "item_count": len(items)},
                 "relay did not confirm successful sender exit")
            need(not incoming.read(1), "trailing transport bytes")
            need(len(done) == MAX_ITEMS and len(events) == MAX_ITEMS,
                 "logical item/reference count incomplete")
            for item in items:
                _references_match(store, item, done[item["item_id"]]["original_id"])
            for digest, blob in plan["blobs"].items():
                store.verify_blob(digest, blob["bytes"])
            need(target_identity(store.root) == target["identity"],
                 "target host/boot/CAS identity changed before completion")
            append({"status": "COMPLETE", "logical_items": len(done),
                    "unique_blobs": len(plan["blobs"]), "logical_bytes": sum(x["bytes"] for x in items),
                    "availability_promotions": 0, "full_corpus_coverage": "UNKNOWN"})
            receipt = {"status": "COMPLETE", "binding": binding, "event_count": len(events),
                       "stream_id": cfg["stream_id"], "plan_sha256": PLAN_SHA256,
                       "logical_items": len(done), "unique_blobs": len(plan["blobs"]),
                       "logical_bytes": sum(x["bytes"] for x in items), "last_event_sha256": previous}
            write_receipt(receipt)
            return receipt
        except BaseException as exc:
            try:
                append({"status": "FAILED", "error_type": type(exc).__name__,
                        "error": str(exc)[:4096], "confirmed_journal_items": len(done),
                        "active_item_id": active_item_id,
                        "active_item_outcome": active_item_outcome,
                        "partial_references_preserved": True})
                write_receipt({
                    "status": "FAILED", "binding": binding, "confirmed_journal_items": len(done),
                    "active_item_id": active_item_id, "active_item_outcome": active_item_outcome,
                    "partial_references_preserved": True, "last_event_sha256": previous})
            except BaseException:
                pass
            raise


def _check_private_lock_dir(path):
    path = backfill._plain(Path(path))
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode), "receiver lock path is not a plain directory")
    if os.name != "nt":
        need(os.geteuid() == 0 and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
             "receiver global lock directory must be root-owned mode 0700")
    return path


def receive_all(cfg, plan, items, incoming, store, job):
    target = cfg["target"]
    lock_dir = _check_private_lock_dir(target["lock_dir"])
    with backfill.locked(lock_dir):
        return _receive_locked(cfg, plan, items, incoming, store, job)


def _capture(pipe, limit=1024 * 1024):
    result = {"data": bytearray(), "overflow": False, "error": None}
    def drain():
        try:
            while True:
                block = pipe.read(CHUNK)
                if not block:
                    return
                room = max(0, limit - len(result["data"]))
                result["data"].extend(block[:room])
                result["overflow"] |= len(block) > room
        except BaseException as exc:
            result["error"] = repr(exc)
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    result["thread"] = thread
    return result


def _remote_command(cfg, role, config_path, job_id, module_sha):
    path = PurePosixPath(config_path)
    module = PurePosixPath(cfg["relay"]["remote_module"])
    need(path.is_absolute() and ".." not in path.parts and str(path) == config_path and
         module.is_absolute() and ".." not in module.parts and
         module.name == "transport_cached_originals.py" and module.parent.name == "ops",
         "remote module/config path must be absolute and canonical")
    target = role == "receive"
    host = TARGET_HOST if target else SOURCE_HOST
    slug = cfg["stream_id"]
    args = (["sudo", "-n"] if target else []) + ["systemd-run", "--scope", "--quiet",
            f"--unit=dtox-original-stream-{role}-{slug}", "-p", "CPUQuota=200%",
            "-p", "MemoryMax=2G", "-p", "MemorySwapMax=0", "-p", "RuntimeMaxSec=infinity"]
    args += ["/usr/bin/python3", "-E", "-S", str(module), role, "--config", str(path),
             "--config-sha256", cfg["relay"][f"{role}_config_sha256"],
             "--peer-config-sha256", cfg["relay"]["receive_config_sha256" if role == "send"
                                                   else "send_config_sha256"],
             "--module-sha256", module_sha, "--module-root", str(module.parents[1]),
             "--job-id", job_id]
    ssh = [SSH, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=yes", "-i", KEY, host, shlex.join(args)]
    return ssh


def relay(cfg, job):
    need(cfg.get("role") == "relay", "relay config required")
    job = _create_job(job)
    module_sha = cfg["_module_sha256"]
    receipt = {"status": "FAILED", "schema": SCHEMA, "stream_id": cfg["stream_id"],
               "plan_sha256": PLAN_SHA256, "module_sha256": module_sha,
               "config_sha256": cfg["_config_sha256"], "code_sha256": cfg["code_sha256"],
               "source_host": SOURCE_HOST, "target_host": TARGET_HOST,
               "host_key_policy": "StrictHostKeyChecking=yes"}
    backfill.write_new(job / "intent.json", receipt)
    src = dst = None
    captures = []
    failed = False
    sender_returncode = None
    receiver_returncode = None
    wire = 0
    try:
        dst = subprocess.Popen(_remote_command(cfg, "receive", cfg["relay"]["destination_config"],
                                               cfg["stream_id"], module_sha), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        dst_out, dst_err = _capture(dst.stdout), _capture(dst.stderr)
        captures.extend((dst_out, dst_err))
        src = subprocess.Popen(_remote_command(cfg, "send", cfg["relay"]["source_config"],
                                               cfg["stream_id"], module_sha), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False)
        src_err = _capture(src.stderr)
        captures.append(src_err)
        wire = 0
        cap = cfg["limits"]["wire_bytes"]
        while True:
            block = src.stdout.read(CHUNK)
            if not block:
                break
            wire += len(block)
            need(wire <= cap, "relay wire cap exceeded")
            _write_all(dst.stdin, block)
        sender_returncode = src.wait()
        receipt["sender_returncode"] = sender_returncode
        need(sender_returncode == 0, "sender process failed")
        commit = {"kind": "relay_commit", "schema": SCHEMA, "stream_id": cfg["stream_id"],
                  "plan_sha256": PLAN_SHA256,
                  "source_config_sha256": cfg["relay"]["send_config_sha256"],
                  "destination_config_sha256": cfg["relay"]["receive_config_sha256"],
                  "module_sha256": module_sha, "sender_returncode": sender_returncode,
                  "item_count": MAX_ITEMS}
        commit_output = WireOutput(dst.stdin, cap - wire)
        write_frame(commit_output, commit)
        wire += commit_output.count
        dst.stdin.close()
        receiver_returncode = dst.wait()
        receipt["receiver_returncode"] = receiver_returncode
        need(receiver_returncode == 0, "receiver process failed")
        for capture in captures:
            capture["thread"].join()
            need(capture["error"] is None and not capture["overflow"], "bounded subprocess capture failed")
        ack = backfill.decode(bytes(dst_out["data"]))
        need(ack.get("status") == "COMPLETE" and ack.get("logical_items") == MAX_ITEMS and
             ack.get("plan_sha256") == PLAN_SHA256 and ack.get("stream_id") == cfg["stream_id"],
             "receiver ACK/count binding mismatch")
        receipt.update(status="COMPLETE", receiver_ack=ack, wire_bytes=wire)
    except BaseException as exc:
        failed = True
        receipt.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:4096])
        raise
    finally:
        cleanup_errors = []
        def close_pipe(pipe):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError as close_error:
                    cleanup_errors.append(repr(close_error))
        if failed:
            close_pipe(dst.stdin if dst is not None else None)
            close_pipe(src.stdout if src is not None else None)
        else:
            close_pipe(dst.stdin if dst is not None else None)
        for process in (src, dst):
            if process is not None:
                try:
                    code = process.wait()
                    if process is src:
                        sender_returncode = code
                    else:
                        receiver_returncode = code
                except BaseException as wait_error:
                    cleanup_errors.append(repr(wait_error))
                for pipe in (process.stdin, process.stdout, process.stderr):
                    close_pipe(pipe)
        for capture in captures:
            try:
                capture["thread"].join()
            except RuntimeError as join_error:
                cleanup_errors.append(repr(join_error))
        if cleanup_errors:
            receipt["cleanup_errors"] = cleanup_errors[:8]
            receipt["status"] = "FAILED"
        receipt["sender_returncode"] = sender_returncode
        receipt["receiver_returncode"] = receiver_returncode
        receipt["diagnostics"] = [{"data": bytes(c["data"]).decode(errors="replace"),
                                    "overflow": c["overflow"], "error": c["error"]} for c in captures]
        try:
            backfill.write_new(job / "receipt.json", receipt)
        except BaseException:
            if not failed:
                raise
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("send", "receive", "relay"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--module-sha256", required=True)
    parser.add_argument("--module-root", required=True)
    parser.add_argument("--peer-config-sha256")
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    config_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    config_fd = os.open(config_path, config_flags)
    with os.fdopen(config_fd, "rb") as config_file:
        config_stat = os.fstat(config_file.fileno())
        need(stat.S_ISREG(config_stat.st_mode) and config_stat.st_nlink == 1,
             "config must be a single-link regular file")
        config_raw = config_file.read(MAX_CONFIG + 1)
    need(len(config_raw) <= MAX_CONFIG, "config JSON cap exceeded")
    need(hashlib.sha256(config_raw).hexdigest() == args.config_sha256, "config SHA mismatch")
    need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == args.module_sha256,
         "module SHA mismatch")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, "duplicate config JSON key")
            result[key] = value
        return result
    cfg = json.loads(config_raw, object_pairs_hook=unique_pairs,
                     parse_constant=lambda value: need(False, "nonfinite config JSON number"))
    validate_config(cfg, args.mode)
    module_root = Path(args.module_root).resolve(strict=True)
    need(module_root == Path(__file__).resolve().parents[1], "module root does not match script location")
    verify_code_pins(args.module_root, cfg["code_sha256"])
    cfg["_config_sha256"] = args.config_sha256
    if args.mode in ("send", "receive"):
        need(re.fullmatch(r"[0-9a-f]{64}", args.peer_config_sha256 or "") is not None,
             "peer config SHA pin required")
        cfg["_peer_config_sha256"] = args.peer_config_sha256
    cfg["_module_sha256"] = args.module_sha256
    load_dependencies()
    plan, items = load_plan(cfg["plan_path"], cfg["plan_sha256"], max_bytes=cfg["limits"]["plan_bytes"])
    if args.mode == "send":
        result = send_all(cfg, plan, items, sys.stdout.buffer)
    elif args.mode == "receive":
        need(args.job_id == cfg["stream_id"], "job id/stream id mismatch")
        store = OriginalStore(cfg["target"]["store_root"])
        job = Path(cfg["target"]["job_root"]) / args.job_id
        result = receive_all(cfg, plan, items, sys.stdin.buffer, store, job)
    else:
        need(args.job_id is None, "relay job path comes from config")
        validate_config(cfg, "relay")
        result = relay(cfg, cfg["relay"]["local_job_root"] + "/" + cfg["stream_id"])
    print(json.dumps(result, sort_keys=True), file=sys.stderr if args.mode == "send" else sys.stdout, flush=True)
    return 0


if __name__ == "__main__":
    try:
        result_code = main()
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__,
                          "error": str(exc)[:4096]}, sort_keys=True),
              file=sys.stderr if len(sys.argv) > 1 and sys.argv[1] == "send" else sys.stdout, flush=True)
        result_code = 1
    raise SystemExit(result_code)
