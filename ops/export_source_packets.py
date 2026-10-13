"""Read-only persisted-corpus exporter; no service import, network or job execution.

--db FILE --latex-cache DIR --fulltext-cache DIR --chunks DIR
--publication-cache DIR [--section-cache DIR] --output NEW_DIRECTORY
[--full-export | --max-sources N | --max-output-bytes N | --limit N]
publishes packets.jsonl and report.json together, with an exclusive atomic rename.
Packets use build_source_representations.packet verbatim. Inventory instead uses
--inventory-only [--sample-size N | --full-scan], without an output directory.
Only DB-addressed artifacts are examined, never recursive directory discovery.
Legacy means no snapshot journal entry, not independent live-index attestation.
Marked raw documents require a source/chunk hash-bound section manifest.
Passage packets carry exact persisted chunk text, artifact SHA, chunk index,
decoded-text locator and persisted section_type/element_type/terms if present.
Missing passage terms may reuse the same source's frozen papers.matched_terms
annotation, capped exactly like original pipeline payloads. Provenance is explicit;
current metadata never overwrites terms in immutable published/chunk artifacts.
--disk-budget-bytes is an abort-only safety bound, not a truncated shard.
--gzip streams packets.jsonl.gz directly, never a plaintext copy. Inventory
--gzip measures compressed bytes with a counting sink and writes no packet files.
Metadata/IDs/snapshot jobs are frozen into an owned temporary SQLite spool;
the primary read transaction closes before any cache artifact is opened.
--spool-dir selects its parent. Citation parsing runs only for inventory;
exports defer actual citation diagnostics to the producer.
Packet SHA and --max-output-bytes refer to decoded JSONL bytes. Storage safety
--disk-budget-bytes and measured_storage_bytes refer to physical compressed bytes.
--work-dir NEW_DIRECTORY enables durable --full-export --gzip batches. Repeat
exactly the same arguments with --resume after interruption; no primary snapshot
is reopened. --checkpoint-rows defaults to 250. --max-seconds returns paused
without publishing output. Only the owned, uncheckpointed packet tail is dropped.
"""
import argparse
from collections import Counter
import contextlib
import ctypes
import hashlib
import gzip as gzip_codec
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.build_source_representations import canonical_id, encoded, packet, unique_object, MAX_LINE_BYTES
from ops.build_source_representations import matched_terms as validate_matched_terms, MAX_MATCHED_TERMS_BYTES, PAYLOAD_TERMS_LIMIT
from api.source_representations import _source as validate_catalog_source, _text as validate_representation_text
from api.source_representations import _url as validate_source_url
from pipeline.citation_contexts import extract_citation_contexts, _mask
from pipeline.document_cache import _plain_path, safe_id, validate_section_manifest
from pipeline import extractor

MAX_METADATA_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_REFERENCES_BYTES = 16_000_000
SHA = re.compile(r"[a-f0-9]{64}\Z")
FIELDS = ("arxiv_id", "title", "year", "layers", "status", "abstract", "source_url", "fulltext_source")


class ExportInterrupted(BaseException):
    pass


class FactsHash:
    def __init__(self, value=None):
        self.value = value or hashlib.sha256(b"").hexdigest()

    def update(self, raw):
        self.value = sha(bytes.fromhex(self.value) + raw)

    def hexdigest(self):
        return self.value


class PrefixReader:
    def __init__(self, stream, remaining):
        self.stream, self.remaining = stream, remaining

    def read(self, size=-1):
        raw = self.stream.read(self.remaining if size < 0 else min(size, self.remaining))
        self.remaining -= len(raw)
        return raw


class HashWriter:
    def __init__(self, stream, digest):
        self.stream, self.digest = stream, digest

    def write(self, raw):
        written = self.stream.write(raw)
        if written != len(raw):
            raise OSError("short durable packet write")
        self.digest.update(raw)
        return written

    def flush(self):
        self.stream.flush()


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path, value):
    raw = encoded(value) + b"\n"
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, path)
    sync_directory(path.parent)
    return sha(raw)


def bounded_json(path):
    return json_value(read_artifact(path, path.parent, MAX_METADATA_BYTES, "checkpoint"))


def published_report(work_path, output_path):
    work_path, output_path = _plain_path(work_path), _plain_path(output_path)
    manifest_path = work_path / "identity.json"
    manifest_raw = read_artifact(manifest_path, work_path, MAX_METADATA_BYTES, "checkpoint")
    manifest = json_value(manifest_raw)
    envelope = bounded_json(work_path / "checkpoint.json")
    state = envelope["payload"]
    report = bounded_json(output_path / "report.json")
    snapshot = manifest["snapshot"]
    if (sha(encoded(state)) != envelope["sha256"] or state["identity_sha256"] != sha(manifest_raw) or
            manifest["config"]["output"] != str(output_path) or
            state["last_row"] != snapshot["counts"]["metadata_rows"] or
            not isinstance(report, dict) or report.get("status") != "ready" or
            report.get("schema") != 1 or report.get("mode") != "export" or
            report.get("output") != str(output_path) or report.get("full_export") is not True or
            report.get("packet_file") != "packets.jsonl.gz" or
            report.get("metadata_snapshot_sha256") != snapshot["sha256"] or
            report.get("packets_sha256") != state["packets_sha256"] or
            report.get("artifact_facts_sha256") != state["facts_sha256"] or
            not isinstance(report.get("counts"), dict) or
            {key: value for key, value in report["counts"].items() if key != "output_storage_bytes"} != state["counts"] or
            report["counts"].get("output_storage_bytes") != state["storage_offset"] or
            type(report.get("partial")) is not bool or
            report.get("complete_generation") is not (not report["partial"])):
        raise ValueError("published durable report does not match owned complete checkpoint")
    packet_path = _plain_path(output_path / "packets.jsonl.gz")
    info = packet_path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            [info.st_dev, info.st_ino] != state["packet_identity"] or info.st_size != state["storage_offset"]):
        raise ValueError("published durable packet does not match owned checkpoint")
    if file_digest(packet_path) != state["storage_sha256"]:
        raise ValueError("published durable packet hash does not match owned checkpoint")
    sync_directory(output_path.parent)
    return report


def file_digest(path):
    path = _plain_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("snapshot/code input must be a regular finite file")
        remaining = before.st_size
        while remaining:
            raw = stream.read(min(1024 * 1024, remaining))
            if not raw:
                raise ValueError("snapshot/code input truncated")
            digest.update(raw)
            remaining -= len(raw)
        after = os.fstat(stream.fileno())
        if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino) or stream.read(1)):
            raise ValueError("snapshot/code input changed")
    return digest.hexdigest()


def runtime_identity():
    modules = [sys.modules[__name__], sys.modules[packet.__module__],
               sys.modules[validate_catalog_source.__module__], sys.modules[extract_citation_contexts.__module__],
               sys.modules[validate_section_manifest.__module__], extractor]
    for name in ("api.boilerplate", "pipeline.pdf_memory"):
        if name in sys.modules:
            modules.append(sys.modules[name])
    return dict(files={str(_plain_path(module.__file__)): file_digest(module.__file__) for module in modules},
                python=sys.version, zlib=zlib.ZLIB_RUNTIME_VERSION, boilerplate_filter=extractor.BOILERPLATE_FILTER)


@contextlib.contextmanager
def work_lock(path, resume):
    if not resume:
        path.mkdir(mode=0o700)
        sync_directory(path.parent)
    elif not path.is_dir():
        raise ValueError("resume requires an existing durable work directory")
    lock_path = _plain_path(path / ".lock")
    with lock_path.open("r+b" if resume else "xb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError("unsafe durable lock file")
        if not resume:
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OSError("durable work directory is locked by another exporter") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def restore_checkpoint(path, identity, raw_stream, snapshot):
    envelope = bounded_json(path)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
        raise ValueError("invalid durable checkpoint")
    state = envelope["payload"]
    keys = {"version", "identity_sha256", "last_row", "storage_offset", "storage_sha256", "packets_sha256",
            "counts", "samples", "facts_sha256", "packet_identity"}
    if (not isinstance(state, dict) or set(state) != keys or state["version"] != 1 or
            sha(encoded(state)) != envelope["sha256"] or state["identity_sha256"] != identity):
        raise ValueError("durable checkpoint identity/hash mismatch")
    if (type(state["last_row"]) is not int or not 0 <= state["last_row"] <= snapshot["counts"]["metadata_rows"] or
            type(state["storage_offset"]) is not int or state["storage_offset"] < 0 or
            not isinstance(state["counts"], dict) or
            any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in state["counts"].items()) or
            state["counts"].get("metadata_rows") != state["last_row"] or
            not isinstance(state["samples"], list) or len(state["samples"]) > 32):
        raise ValueError("invalid durable checkpoint counters")
    for key in ("storage_sha256", "packets_sha256", "facts_sha256"):
        if not isinstance(state[key], str) or not SHA.fullmatch(state[key]):
            raise ValueError("invalid durable checkpoint digest")
    before = os.fstat(raw_stream.fileno())
    offset = state["storage_offset"]
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size < offset or
            state["packet_identity"] != [before.st_dev, before.st_ino]):
        raise ValueError("durable committed packet file replaced/truncated/unsafe")
    storage, digest, decoded_bytes = hashlib.sha256(), hashlib.sha256(), 0
    raw_stream.seek(0)
    remaining = offset
    while remaining:
        raw = raw_stream.read(min(1024 * 1024, remaining))
        if not raw:
            raise ValueError("durable committed packet truncated while hashing")
        storage.update(raw)
        remaining -= len(raw)
    if storage.hexdigest() != state["storage_sha256"]:
        raise ValueError("durable committed packet hash mismatch")
    raw_stream.seek(0)
    try:
        with gzip_codec.GzipFile(fileobj=PrefixReader(raw_stream, offset), mode="rb") as decoded:
            while True:
                raw = decoded.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                    raise ValueError("durable decoded packet exceeds line bound")
                digest.update(raw)
                decoded_bytes += len(raw)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("invalid sealed gzip checkpoint") from exc
    after = os.fstat(raw_stream.fileno())
    if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
            (after.st_size, after.st_mtime_ns, after.st_ino) or
            digest.hexdigest() != state["packets_sha256"] or decoded_bytes != state["counts"].get("output_bytes")):
        raise ValueError("durable packet/checkpoint changed or decoded hash mismatch")
    raw_stream.seek(offset)
    raw_stream.truncate(offset)
    raw_stream.flush()
    os.fsync(raw_stream.fileno())
    return state, storage, digest


class Skip(ValueError):
    pass


class ByteCounter:
    def __init__(self):
        self.size = 0

    def write(self, raw):
        self.size += len(raw)
        return len(raw)

    def flush(self):
        pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_value(raw):
    def nonfinite(value):
        raise ValueError("nonfinite JSON")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=nonfinite)


def root_path(path):
    result = _plain_path(path)
    if not result.is_dir():
        raise ValueError("explicit cache/output parent must be an existing directory")
    return result


def read_artifact(path, root, maximum, label):
    try:
        path = _plain_path(path)
        path.relative_to(root)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise Skip(label + "_bounds")
            raw = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
        _plain_path(path)
        if (len(raw) != before.st_size or
                (before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino)):
            raise Skip(label + "_changed")
        return raw
    except FileNotFoundError as exc:
        raise Skip(label + "_missing") from exc
    except Skip:
        raise
    except (OSError, ValueError) as exc:
        raise Skip(label + "_unsafe_or_unreadable") from exc


def bibliography(raw, source):
    try:
        manifest = json_value(raw)
        if not isinstance(manifest, dict) or set(manifest) != {"version", "source_sha256", "files", "manifest_sha256"}:
            raise ValueError("references schema")
        signature = manifest.pop("manifest_sha256")
        if type(manifest["version"]) is not int or manifest["version"] != 1 or sha(encoded(manifest)) != signature:
            raise ValueError("references signature")
        if manifest["source_sha256"] != sha(source):
            raise Skip("references_stale")
        files = manifest["files"]
        if not isinstance(files, list) or len(files) > 128:
            raise ValueError("references count")
        seen, total = set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "text"}:
                raise ValueError("references entry")
            name, text = item["path"], item["text"]
            if (not isinstance(name, str) or not name or len(name) > 1024 or
                    any(c in name for c in ("\\", ":", "\x00")) or
                    PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or
                    str(PurePosixPath(name)) != name or not name.endswith((".bib", ".bbl")) or name in seen):
                raise ValueError("unsafe references path")
            if not isinstance(text, str):
                raise ValueError("references text")
            seen.add(name)
            size = len(text.encode("utf-8"))
            total += size
            if size > 2_000_000 or total > 8_000_000:
                raise ValueError("references bytes")
        return files
    except Skip:
        raise
    except (ValueError, TypeError, RecursionError, KeyError) as exc:
        raise Skip("references_invalid") from exc


def chunks_for(name, roots, identity):
    raw = read_artifact(roots["chunks"] / (name + ".json"), roots["chunks"], MAX_LINE_BYTES, "chunks")
    try:
        chunks = json_value(raw)
        if not isinstance(chunks, list) or len(chunks) > 10000:
            raise ValueError("chunks schema")
        indices = set()
        for item in chunks:
            if (not isinstance(item, dict) or type(item.get("chunk_index")) is not int or
                    item["chunk_index"] < 0 or item["chunk_index"] in indices or
                    not isinstance(item.get("text"), str) or not item["text"].strip()):
                raise ValueError("chunk identity/text")
            if any(key in item and item[key] != identity for key in ("arxiv_id", "source_id")):
                raise ValueError("chunk owner mismatch")
            indices.add(item["chunk_index"])
        return raw, chunks
    except (ValueError, TypeError, RecursionError) as exc:
        raise Skip("chunks_invalid") from exc


def publication(conn, row, roots, has_journal, chunk_data, counts):
    def legacy():
        chunks = chunk_data[1] if chunk_data is not None else []
        if any("publication_revision" in chunk for chunk in chunks):
            raise Skip("publication_unjournaled_marker")
        selected = []
        for chunk in chunks:
            if extractor.should_embed(chunk):
                selected.append(chunk)
                counts["legacy_owned_chunks_policy_selected"] += 1
            else:
                counts["legacy_owned_chunks_policy_skipped"] += 1
        return None, selected
    if not has_journal:
        return legacy()
    jobs = conn.execute(
        "SELECT revision,state,artifact_path,cache_sha256,oversized FROM paper_publications "
        "WHERE arxiv_id=? AND kind='snapshot' LIMIT 2", (row["arxiv_id"],)).fetchall()
    if not jobs:
        return legacy()
    if len(jobs) != 1:
        raise Skip("publication_ambiguous")
    job = jobs[0]
    if job["oversized"]:
        raise Skip("publication_journal_bounds")
    if job["state"] != "published":
        raise Skip("publication_hidden")
    if not isinstance(job["revision"], str) or not SHA.fullmatch(job["revision"]):
        raise Skip("publication_revision_invalid")
    if chunk_data is None or sha(chunk_data[0]) != job["cache_sha256"]:
        raise Skip("publication_chunks_mismatch")
    if any("publication_revision" in chunk and chunk["publication_revision"] != job["revision"]
           for chunk in chunk_data[1]):
        raise Skip("publication_cache_marker_mismatch")
    try:
        artifact = json_value(read_artifact(job["artifact_path"], roots["publication-cache"],
                                            MAX_SNAPSHOT_BYTES, "publication"))
        if not isinstance(artifact, dict) or set(artifact) != {"points", "cache_path", "metadata"}:
            raise ValueError("snapshot schema")
        current_metadata = {key: row[key] for key in ("title", "year", "layers", "source_url")}
        metadata = artifact["metadata"]
        if not isinstance(metadata, dict) or set(metadata) != set(current_metadata):
            raise ValueError("snapshot metadata schema")
        if metadata != current_metadata:
            counts["publication_metadata_updated"] += 1
        expected_cache = roots["chunks"] / (safe_id(row["arxiv_id"]) + ".json")
        if _plain_path(artifact["cache_path"]) != expected_cache:
            raise Skip("publication_cache_path_mismatch")
        points = artifact["points"]
        if not isinstance(points, list) or len(points) > 3000:
            raise ValueError("snapshot points")
        by_index = {item["chunk_index"]: item for item in chunk_data[1]}
        seen, published = set(), []
        for point in points:
            payload = point.get("payload") if isinstance(point, dict) else None
            if (not isinstance(payload, dict) or payload.get("arxiv_id") != row["arxiv_id"] or
                    payload.get("publication_revision") != job["revision"]):
                raise Skip("publication_marker_mismatch")
            index = payload.get("chunk_index")
            if type(index) is not int or index in seen or index not in by_index or payload.get("text") != by_index[index]["text"]:
                raise Skip("publication_point_mismatch")
            if any(key in by_index[index] and by_index[index][key] != payload.get(key)
                   for key in ("section_type", "element_type", "terms")):
                raise Skip("publication_tags_mismatch")
            seen.add(index)
            published.append({key: payload[key] for key in
                              ("chunk_index", "text", "section_type", "element_type", "terms") if key in payload})
            payload.pop("publication_revision")
        material = encoded({"points": points, "metadata": metadata})
        if sha(job["cache_sha256"].encode("utf-8") + material) != job["revision"]:
            raise Skip("publication_revision_mismatch")
        return job["revision"], published
    except Skip:
        raise
    except (ValueError, TypeError, RecursionError, KeyError) as exc:
        raise Skip("publication_invalid") from exc


def raw_document(row, name, roots, revision, chunk_data, counts, *, diagnostics=True):
    if row["fulltext_source"] != "latex":
        raise Skip("document_not_raw_latex")
    copies = []
    for path, root in ((roots["latex-cache"] / name / "source.tex", roots["latex-cache"]),
                       (roots["fulltext-cache"] / (name + ".tex"), roots["fulltext-cache"])):
        try:
            copies.append(read_artifact(path, root, MAX_LINE_BYTES, "source"))
        except Skip as exc:
            if str(exc) != "source_missing":
                raise
            counts["source_copies_missing"] += 1
    if not copies:
        raise Skip("source_missing")
    if any(raw != copies[0] for raw in copies[1:]):
        raise Skip("source_copies_ambiguous")
    raw = copies[0]
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise Skip("source_invalid_utf8") from exc
    if not text.strip() or "\x00" in text:
        raise Skip("source_invalid")
    if revision is not None:
        if "section-cache" not in roots:
            raise Skip("publication_source_unbound")
        try:
            manifest = json_value(read_artifact(roots["section-cache"] / (name + ".json"),
                                               roots["section-cache"], MAX_LINE_BYTES, "sections"))
            validate_section_manifest(manifest, row["arxiv_id"], chunk_data[0], raw)
            if manifest["parser_mode"] not in {"pylatexenc", "pylatexenc_recovered"}:
                raise ValueError("not parsed LaTeX")
        except Skip:
            raise
        except (ValueError, TypeError, RecursionError, KeyError) as exc:
            raise Skip("publication_source_mismatch") from exc
    visible = _mask(text, [], None)
    if not re.search(r"\\(?:documentclass\b|begin\s*\{document\}|section\b)", visible):
        raise Skip("source_not_latex")
    counts["latex_recoverable"] += 1
    inline = bool(re.search(r"\\bibitem(?![A-Za-z@])", visible))
    try:
        reference_bytes = read_artifact(roots["latex-cache"] / name / "references.json",
                                        roots["latex-cache"], MAX_REFERENCES_BYTES, "references")
    except Skip as exc:
        if str(exc) != "references_missing":
            raise
        counts["references_missing"] += 1
        if not inline:
            raise Skip("document_no_bibliography") from exc
        files = []
    else:
        files = bibliography(reference_bytes, raw)
    if not files and not inline:
        raise Skip("document_no_bibliography")
    counts["bibliography_inline" if inline else "bibliography_sidecar"] += 1
    try:
        if diagnostics:
            extracted = extract_citation_contexts(text, [(item["path"], item["text"]) for item in files])
            for diagnostic in extracted["diagnostics"]:
                counts["citation_" + diagnostic["code"]] += 1
        record = packet(dict(type="document", source_id=row["arxiv_id"], text=text,
                             bibliography_files=files, origin_publication_revision=revision))
        if len(encoded(record)) + 1 > MAX_LINE_BYTES:
            raise ValueError("packet byte bound")
        return record
    except (ValueError, TypeError, RecursionError) as exc:
        raise Skip("document_bounds_or_invalid") from exc


def rename_new(source, destination):
    """Exclusive rename: never replace even an empty concurrently-created output."""
    _plain_path(destination)
    if os.name == "nt":
        os.rename(source, destination)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError("exclusive renameat2 is unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    else:
        raise OSError("exclusive atomic publication is supported on Windows and Linux only")


def passage_packet(identity, name, artifact_sha256, chunk, revision):
    text, index = chunk["text"], chunk["chunk_index"]
    result = dict(type="passage", source_id=identity, origin_id=identity,
                  text=text, artifact_sha256=artifact_sha256, chunk_index=index,
                  locator=dict(start=0, end=len(text), unit="unicode_codepoint",
                               artifact=f"chunk_cache/{name}.json#chunk_index={index}/text"),
                  origin_publication_revision=revision)
    result.update({key: chunk[key] for key in ("section_type", "element_type", "terms") if key in chunk})
    return packet(result)


def own_annotation(row, snapshot_sha256, counts, skipped):
    provenance = dict(kind="frozen_papers_matched_terms", source_id=row["arxiv_id"],
                      metadata_snapshot_sha256=snapshot_sha256, payload_limit=PAYLOAD_TERMS_LIMIT,
                      publication_verified=False)
    reason = None
    if not row["matched_terms_available"]:
        reason = "column_missing"
    elif row["matched_terms_oversized"]:
        reason = "bounds"
    elif row["matched_terms"] is None:
        reason = "null"
    else:
        try:
            values = validate_matched_terms(json_value(row["matched_terms"]))
            counts["source_matched_terms_available"] += 1
            if not values:
                counts["source_matched_terms_empty"] += 1
            if len(values) > PAYLOAD_TERMS_LIMIT:
                counts["source_matched_terms_payload_capped"] += 1
            return values, provenance
        except (ValueError, TypeError, RecursionError):
            reason = "invalid"
    skipped("matched_terms_" + reason, row["arxiv_id"])
    return [], dict(kind="unavailable", source_id=row["arxiv_id"],
                    metadata_snapshot_sha256=snapshot_sha256, reason=reason)


def freeze_inputs(database, destination, scan_limit, guard, progress):
    digest = hashlib.sha256()
    totals = Counter()

    def fingerprint(kind, row):
        values = [{"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row]
        digest.update(encoded([kind, values]) + b"\n")

    with contextlib.closing(sqlite3.connect(destination)) as spool:
        spool.execute("PRAGMA journal_mode=DELETE")
        spool.execute("PRAGMA cache_size=-8192")
        spool.execute("CREATE TABLE identities(identity)")
        spool.execute("CREATE TABLE papers(" + ",".join(FIELDS) +
                      ",oversized,matched_terms,matched_terms_available,matched_terms_oversized)")
        spool.execute("CREATE TABLE paper_publications(arxiv_id,revision,state,artifact_path,cache_sha256,oversized,"
                      "kind DEFAULT 'snapshot')")
        spool.execute("CREATE INDEX publications_owner ON paper_publications(arxiv_id)")
        with contextlib.closing(sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2)) as primary:
            primary.execute("PRAGMA query_only=ON")
            primary.execute("BEGIN")
            columns = {r[1] for r in primary.execute("PRAGMA table_info(papers)")}
            if not set(FIELDS) <= columns:
                raise ValueError("papers schema lacks required metadata fields")
            has_matched_terms = "matched_terms" in columns
            has_journal = bool(primary.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                               "AND name='paper_publications'").fetchone())
            if has_journal:
                columns = {r[1] for r in primary.execute("PRAGMA table_info(paper_publications)")}
                if not {"arxiv_id", "revision", "kind", "state", "artifact_path", "cache_sha256"} <= columns:
                    raise ValueError("publication journal schema is incomplete")
            totals["metadata_total"] = primary.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            totals["metadata_done_total"] = primary.execute("SELECT COUNT(*) FROM papers WHERE status='done'").fetchone()[0]
            sql_limit = totals["metadata_done_total"] if scan_limit is None else scan_limit
            size = "+".join(f"COALESCE(length(CAST({field} AS BLOB)),0)" for field in FIELDS)
            selected = ",".join(f"CASE WHEN ({size})<={MAX_METADATA_BYTES} THEN {field} END" for field in FIELDS)
            annotation = (f"CASE WHEN length(CAST(matched_terms AS BLOB))<={MAX_MATCHED_TERMS_BYTES} "
                          f"THEN matched_terms END,1,COALESCE(length(CAST(matched_terms AS BLOB)),0)>{MAX_MATCHED_TERMS_BYTES}"
                          if has_matched_terms else "NULL,0,0")
            projections = [
                ("identities", "SELECT CASE WHEN length(CAST(arxiv_id AS BLOB))<=256 THEN arxiv_id END "
                 "FROM papers ORDER BY arxiv_id", (), "INSERT INTO identities VALUES(?)"),
                ("metadata", f"SELECT {selected},({size})>{MAX_METADATA_BYTES},{annotation} FROM papers "
                 "WHERE status='done' ORDER BY arxiv_id LIMIT ?", (sql_limit,),
                 "INSERT INTO papers VALUES(" + ",".join("?" for _ in range(len(FIELDS) + 4)) + ")"),
            ]
            if has_journal:
                journal_size = "+".join(f"COALESCE(length(CAST({key} AS BLOB)),0)" for key in
                                       ("revision", "state", "artifact_path", "cache_sha256"))
                fields = ",".join(f"CASE WHEN ({journal_size})<=16384 THEN {key} END" for key in
                                  ("revision", "state", "artifact_path", "cache_sha256"))
                projections.append(("journal", f"SELECT arxiv_id,{fields},({journal_size})>16384 "
                    "FROM paper_publications WHERE kind='snapshot' AND arxiv_id IN "
                    "(SELECT arxiv_id FROM papers WHERE status='done' ORDER BY arxiv_id LIMIT ?) "
                    "ORDER BY arxiv_id,revision,state,artifact_path,cache_sha256", (sql_limit,),
                    "INSERT INTO paper_publications(arxiv_id,revision,state,artifact_path,cache_sha256,oversized) "
                    "SELECT ?,?,?,?,?,? WHERE "
                    "(SELECT COUNT(*) FROM paper_publications WHERE arxiv_id=?)<2"))
            for kind, query, parameters, insert in projections:
                for row in primary.execute(query, parameters):
                    fingerprint(kind, row)
                    spool.execute(insert, (*row, row[0]) if kind == "journal" else row)
                    totals[kind + "_rows"] += 1
                    if totals[kind + "_rows"] % 1000 == 0:
                        spool.commit()
                        guard()
                        if kind != "identities" or totals[kind + "_rows"] % 100000 == 0:
                            progress("snapshot_" + kind, totals[kind + "_rows"])
                spool.commit()
                guard()
            primary.rollback()
        progress("primary_released", totals["metadata_rows"])
        # Collision processing is deliberately outside the primary read transaction.
        spool.execute("CREATE TABLE identity_counts(kind TEXT,value TEXT,n INTEGER,PRIMARY KEY(kind,value)) WITHOUT ROWID")
        for index, (identity,) in enumerate(spool.execute("SELECT identity FROM identities"), 1):
            try:
                value = canonical_id(identity)
            except (ValueError, TypeError):
                continue
            values = [("canonical", value)]
            try:
                values.append(("filename", safe_id(identity).casefold()))
            except (ValueError, TypeError):
                pass
            spool.executemany("INSERT INTO identity_counts VALUES(?,?,1) "
                              "ON CONFLICT(kind,value) DO UPDATE SET n=n+1", values)
            if index % 1000 == 0:
                spool.commit()
                guard()
        spool.commit()
        guard()
    digest.update(encoded(dict(totals=totals, fields=FIELDS, has_journal=has_journal,
                               scan_limit=scan_limit, matched_terms_column_available=has_matched_terms, projection_version=2)))
    return dict(sha256=digest.hexdigest(), counts=dict(totals), has_journal=has_journal,
                bytes=destination.stat().st_size,
                scope="bounded_metadata_and_snapshot_jobs_for_selected_done_rows; all_ids_collision_index",
                matched_terms_column_available=has_matched_terms,
                primary_transaction_released=True)


def export_packets(*, db, latex_cache, fulltext_cache, chunks, publication_cache,
                   output=None, section_cache=None, inventory_only=False, sample_size=100,
                   full_scan=False, limit=None, max_documents=None, max_sources=None,
                   max_output_bytes=None, reserve_bytes=2 * 1024**3, full_export=False,
                   disk_budget_bytes=None, gzip=False, gzip_level=6, spool_dir=None,
                   work_dir=None, resume=False, checkpoint_rows=250, max_seconds=None):
    started = time.monotonic()
    phases = {}
    for name, value in (("limit", limit), ("max_documents", max_documents), ("sample_size", sample_size),
                        ("max_sources", max_sources), ("max_output_bytes", max_output_bytes),
                        ("disk_budget_bytes", disk_budget_bytes)):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(name + " must be positive")
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError("reserve_bytes must be a nonnegative integer")
    if type(gzip_level) is not int or not 1 <= gzip_level <= 9:
        raise ValueError("gzip_level must be 1..9")
    if type(checkpoint_rows) is not int or not 1 <= checkpoint_rows <= 10000:
        raise ValueError("checkpoint_rows must be 1..10000")
    if max_seconds is not None and (type(max_seconds) not in (int, float) or
                                    not 0 < max_seconds < float("inf")):
        raise ValueError("max_seconds must be finite and positive")
    if resume and work_dir is None:
        raise ValueError("--resume requires --work-dir")
    if work_dir is not None and (not full_export or not gzip or spool_dir is not None):
        raise ValueError("--work-dir requires --full-export --gzip and replaces --spool-dir")
    if max_seconds is not None and work_dir is None:
        raise ValueError("--max-seconds requires durable --work-dir")
    if extractor.is_boilerplate_chunk is None:
        raise ValueError("existing should_embed boilerplate policy is unavailable")
    if inventory_only == (output is not None) or (full_scan and not inventory_only):
        raise ValueError("choose --output or --inventory-only; --full-scan is inventory-only")
    if full_scan and limit is not None:
        raise ValueError("--full-scan forbids --limit")
    if inventory_only and max_documents is not None:
        raise ValueError("--max-documents is export-only")
    if inventory_only and (max_sources is not None or max_output_bytes is not None):
        raise ValueError("inventory bounds use --sample-size/--limit, not export shard caps")
    if full_export and (inventory_only or any(v is not None for v in (limit, max_documents, max_sources, max_output_bytes))):
        raise ValueError("--full-export requires --output and forbids all shard limits")
    if inventory_only and disk_budget_bytes is not None:
        raise ValueError("--disk-budget-bytes is export-only")
    if not inventory_only and not full_export and limit is None and max_sources is None and max_output_bytes is None:
        raise ValueError("export requires --full-export or an explicit shard cap")
    roots = {name: root_path(value) for name, value in (
        ("latex-cache", latex_cache), ("fulltext-cache", fulltext_cache),
        ("chunks", chunks), ("publication-cache", publication_cache))}
    if section_cache is not None:
        roots["section-cache"] = root_path(section_cache)
    database = _plain_path(db)
    if not database.is_file():
        raise ValueError("--db must be an existing regular file")
    output_path = _plain_path(output) if output is not None else None
    if output_path is not None:
        root_path(output_path.parent)
        if os.path.lexists(output_path) and not resume:
            raise FileExistsError("output already exists; choose a new directory")
        if any(output_path.is_relative_to(root) for root in roots.values()):
            raise ValueError("output must be outside input cache roots")
    counts = Counter(metadata_rows=0, sources_recoverable=0, documents_recoverable=0,
                     source_packets=0, document_packets=0, chunks_recoverable=0,
                     documents_omitted=0, legacy_unmarked=0, published_snapshots=0,
                     abstracts_recoverable=0, latex_recoverable=0, owned_chunks_recoverable=0,
                     passage_packets=0, packet_bytes_recoverable=0, output_bytes=0,
                     owned_text_bytes=0, abstract_bytes=0, missing_bibliography=0)
    samples, digest, facts = [], hashlib.sha256(), FactsHash()
    stopped = None

    def progress(phase, rows):
        print(encoded(dict(phase=phase, rows=rows, sources=counts["source_packets"],
                           documents=counts["document_packets"], passages=counts["passage_packets"],
                           elapsed_seconds=round(time.monotonic() - started, 3))).decode("utf-8"),
              file=sys.stderr, flush=True)

    def skipped(code, identity):
        counts[code] += 1
        counts["diagnostic_events"] += 1
        if len(samples) < 32:
            samples.append(dict(code=code, source_id=str(identity)[:256]))

    with contextlib.ExitStack() as stack:
        scan_limit = limit
        if inventory_only and not full_scan:
            scan_limit = min(limit, sample_size) if limit is not None else sample_size
        work_path = _plain_path(work_dir) if work_dir is not None else None
        config = None
        if work_path is not None:
            root_path(work_path.parent)
            if (any(work_path.is_relative_to(root) for root in roots.values()) or
                    work_path.is_relative_to(output_path) or output_path.is_relative_to(work_path) or
                    database.is_relative_to(work_path)):
                raise ValueError("durable work directory must be outside inputs and final output")
            if work_path.parent.stat().st_dev != output_path.parent.stat().st_dev:
                raise ValueError("durable work directory and output must share a filesystem")
            config = dict(version=1, output=str(output_path), db=str(database),
                          roots={name: [str(path), path.stat().st_dev, path.stat().st_ino] for name, path in roots.items()},
                          database_identity=[database.stat().st_dev, database.stat().st_ino],
                          options=dict(full_export=full_export, gzip=gzip, gzip_level=gzip_level,
                                       reserve_bytes=reserve_bytes, disk_budget_bytes=disk_budget_bytes,
                                       checkpoint_rows=checkpoint_rows, limit=limit, max_sources=max_sources,
                                       max_documents=max_documents, max_output_bytes=max_output_bytes,
                                       inventory_only=inventory_only, sample_size=sample_size, full_scan=full_scan),
                          code=runtime_identity())
            stack.enter_context(work_lock(work_path, resume))
        spool_root = work_path if work_path is not None else root_path(spool_dir if spool_dir is not None else
                               output_path.parent if output_path is not None else tempfile.gettempdir())
        if any(spool_root.is_relative_to(root) for root in roots.values()):
            raise ValueError("spool directory must be outside input cache roots")

        def spool_guard():
            if shutil.disk_usage(spool_root).free < reserve_bytes + 65536:
                raise OSError("insufficient free space for metadata spool and disk reserve")

        spool_guard()
        spool_path = (work_path / "snapshot.sqlite" if work_path is not None else
                      Path(stack.enter_context(tempfile.TemporaryDirectory(
                          prefix=".source-metadata-", dir=spool_root))) / "snapshot.sqlite")
        work_identity = None
        if resume:
            manifest_raw = read_artifact(work_path / "identity.json", work_path, MAX_METADATA_BYTES, "checkpoint")
            manifest = json_value(manifest_raw)
            if (not isinstance(manifest, dict) or set(manifest) != {"config", "snapshot", "snapshot_file_sha256"} or
                    manifest["config"] != config or file_digest(spool_path) != manifest["snapshot_file_sha256"]):
                raise ValueError("durable options/roots/code/frozen snapshot identity mismatch")
            snapshot = manifest["snapshot"]
            work_identity = sha(manifest_raw)
            if os.path.lexists(output_path):
                return published_report(work_path, output_path)
        else:
            snapshot = freeze_inputs(database, spool_path, scan_limit, spool_guard, progress)
            if work_path is not None:
                with spool_path.open("r+b") as frozen:
                    os.fsync(frozen.fileno())
                work_identity = atomic_json(work_path / "identity.json", dict(
                    config=config, snapshot=snapshot, snapshot_file_sha256=file_digest(spool_path)))
        phases["snapshot_seconds"] = round(time.monotonic() - started, 3)
        artifact_started = time.monotonic()
        counts["metadata_total"] = snapshot["counts"]["metadata_total"]
        counts["metadata_done_total"] = snapshot["counts"]["metadata_done_total"]
        counts["source_not_done"] = counts["metadata_total"] - counts["metadata_done_total"]
        has_journal = snapshot["has_journal"]
        conn = stack.enter_context(contextlib.closing(sqlite3.connect(
            spool_path.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None)))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA cache_size=-8192")
        conn.execute("PRAGMA temp_store=FILE")

        def identity_count(kind, value):
            row = conn.execute("SELECT n FROM identity_counts WHERE kind=? AND value=?", (kind, value)).fetchone()
            return row[0] if row is not None else 0
        stream = None
        raw_stream = None
        measurement_stream = None
        measurement_counter = None
        temporary = None
        checkpoint_state = None
        last_row = 0
        storage_digest = hashlib.sha256()
        packet_filename = "packets.jsonl.gz" if gzip else "packets.jsonl"
        if output_path is not None:
            if shutil.disk_usage(output_path.parent).free < reserve_bytes + 65536:
                raise OSError("insufficient free space for output and disk reserve")
            temporary = (work_path / "payload" if work_path is not None else
                         Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".source-packets-", dir=output_path.parent))))
            if work_path is not None and not resume:
                temporary.mkdir(mode=0o700)
            _plain_path(temporary)
            raw_stream = stack.enter_context(_plain_path(temporary / packet_filename).open("r+b" if resume else "xb"))
            if resume:
                checkpoint_state, storage_digest, digest = restore_checkpoint(
                    work_path / "checkpoint.json", work_identity, raw_stream, snapshot)
                counts = Counter(checkpoint_state["counts"])
                samples = checkpoint_state["samples"]
                facts = FactsHash(checkpoint_state["facts_sha256"])
                last_row = checkpoint_state["last_row"]
                progress("restored", last_row)
            if work_path is not None:
                if not resume or last_row < snapshot["counts"]["metadata_rows"]:
                    stream = gzip_codec.GzipFile(filename="", fileobj=HashWriter(raw_stream, storage_digest),
                                                 mode="wb", mtime=0, compresslevel=gzip_level)
                stack.callback(lambda: stream.close() if stream is not None else None)
            else:
                stream = stack.enter_context(gzip_codec.GzipFile(
                    filename="", fileobj=raw_stream, mode="wb", mtime=0, compresslevel=gzip_level)) if gzip else raw_stream
        elif gzip:
            measurement_counter = ByteCounter()
            measurement_stream = stack.enter_context(gzip_codec.GzipFile(
                filename="", fileobj=measurement_counter, mode="wb", mtime=0, compresslevel=gzip_level))

        def emit(record, raw):
            if len(raw) > MAX_LINE_BYTES:
                raise Skip("packet_bounds")
            if stream is not None:
                if not gzip and disk_budget_bytes is not None and counts["output_bytes"] + len(raw) > disk_budget_bytes:
                    raise OSError("disk output budget reached; no output published")
                if shutil.disk_usage(output_path.parent).free < reserve_bytes + len(raw):
                    raise OSError("disk reserve reached; no output published")
                stream.write(raw)
                digest.update(raw)
                counts[record["type"] + "_packets"] += 1
                counts["output_bytes"] += len(raw)
            elif measurement_stream is not None:
                measurement_stream.write(raw)

        def save_checkpoint(seal=True):
            nonlocal stream, checkpoint_state
            if seal:
                stream.close()
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
            if disk_budget_bytes is not None and raw_stream.tell() > disk_budget_bytes:
                raise OSError("durable disk output budget reached; previous checkpoint retained")
            spool_guard()
            info = os.fstat(raw_stream.fileno())
            checkpoint_state = dict(version=1, identity_sha256=work_identity, last_row=last_row,
                                    storage_offset=raw_stream.tell(), storage_sha256=storage_digest.hexdigest(),
                                    packets_sha256=digest.hexdigest(), counts=dict(counts), samples=list(samples),
                                    facts_sha256=facts.hexdigest(), packet_identity=[info.st_dev, info.st_ino])
            atomic_json(work_path / "checkpoint.json", dict(payload=checkpoint_state, sha256=sha(encoded(checkpoint_state))))
            progress("checkpoint", last_row)

        if work_path is not None and not resume:
            # The first member header is an uncommitted tail until the first seal.
            raw_stream.flush()
            checkpoint_state = dict(version=1, identity_sha256=work_identity, last_row=0, storage_offset=0,
                                    storage_sha256=sha(b""), packets_sha256=sha(b""), counts=dict(counts), samples=[],
                                    facts_sha256=facts.hexdigest(), packet_identity=[os.fstat(raw_stream.fileno()).st_dev,
                                                                                   os.fstat(raw_stream.fileno()).st_ino])
            atomic_json(work_path / "checkpoint.json", dict(payload=checkpoint_state, sha256=sha(encoded(checkpoint_state))))
        rows = stack.enter_context(contextlib.closing(conn.execute(
            "SELECT rowid AS export_row,* FROM papers WHERE rowid>? ORDER BY rowid", (last_row,))))
        for row in rows:
            if work_path is not None and counts["metadata_rows"] - checkpoint_state["last_row"] >= checkpoint_rows:
                save_checkpoint()
                stream = gzip_codec.GzipFile(filename="", fileobj=HashWriter(raw_stream, storage_digest),
                                             mode="wb", mtime=0, compresslevel=gzip_level)
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                if last_row != checkpoint_state["last_row"]:
                    save_checkpoint()
                return dict(status="paused", partial=True, complete_generation=False, output=None,
                            work_dir=str(work_path), committed_rows=checkpoint_state["last_row"],
                            expected_metadata_rows=counts["metadata_done_total"], counts=checkpoint_state["counts"])
            if counts["metadata_rows"] and counts["metadata_rows"] % 1000 == 0:
                progress("artifacts", counts["metadata_rows"])
            if max_sources is not None and counts["source_packets"] >= max_sources:
                stopped = "max_sources"
                break
            counts["metadata_rows"] += 1
            last_row = row["export_row"]
            facts.update(encoded({key: row[key] for key in row.keys() if key != "export_row"}))
            identity = row["arxiv_id"]
            try:
                if row["oversized"]:
                    raise Skip("metadata_bounds")
                if row["status"] != "done":
                    raise Skip("source_not_done")
                if identity_count("canonical", canonical_id(identity)) != 1:
                    raise Skip("source_identity_ambiguous")
                try:
                    name = safe_id(identity)
                except ValueError:
                    name = None
                chunk_data = None
                if name is not None and identity_count("filename", name.casefold()) == 1:
                    try:
                        chunk_data = chunks_for(name, roots, identity)
                        counts["chunks_recoverable"] += 1
                    except Skip as exc:
                        skipped(str(exc), identity)
                revision, owned_chunks = publication(conn, row, roots, has_journal, chunk_data, counts)
                facts.update(encoded([identity, revision]))
                if row["layers"] is not None and not isinstance(row["layers"], str):
                    raise ValueError("layers must be persisted CSV")
                annotation_terms, annotation_provenance = own_annotation(row, snapshot["sha256"], counts, skipped)
                title = row["title"]
                if isinstance(title, str):
                    normalized = re.sub(r"[ \t\r\n\f]+", " ", title).strip(" ")
                    if normalized != title:
                        counts["metadata_title_whitespace_normalized"] += 1
                    title = normalized
                try:
                    source_url = validate_source_url(row["source_url"])
                except (ValueError, TypeError):
                    source_url = None
                    skipped("invalid_source_url_omitted", identity)
                record = packet(dict(type="source", source_id=identity, title=title,
                                     year=row["year"], layers=row["layers"].split(",") if row["layers"] else [],
                                     status="done", publication_revision=revision,
                                     abstract=row["abstract"] if row["abstract"] is not None else "",
                                     source_url=source_url, matched_terms=annotation_terms,
                                     terms_provenance=annotation_provenance))
                if len(record["title"]) > 4096 or len(record["abstract"]) > 65536 or len(record["layers"]) > 32:
                    raise ValueError("source exceeds catalog bounds")
                validate_catalog_source({key: record[key] for key in
                                         ("source_id", "title", "year", "layers", "status", "publication_revision", "source_url")},
                                        canonical_id)
                if record["abstract"].strip():
                    validate_representation_text(record["abstract"])
                counts["sources_recoverable"] += 1
                counts["legacy_unmarked" if revision is None else "published_snapshots"] += 1
                if record["abstract"].strip():
                    counts["abstracts_recoverable"] += 1
                    counts["abstract_bytes"] += len(record["abstract"].encode("utf-8"))
            except Skip as exc:
                skipped(str(exc), identity)
                continue
            except (ValueError, TypeError, RecursionError) as exc:
                skipped("metadata_invalid", identity)
                continue
            passages = []
            chunk_sha = sha(chunk_data[0]) if chunk_data is not None else None
            for chunk in owned_chunks:
                try:
                    passage = passage_packet(identity, name, chunk_sha, chunk, revision)
                    if "terms" in chunk:
                        passage["terms_provenance"] = dict(
                            kind="publication_snapshot_payload" if revision is not None else "chunk_cache_payload",
                            source_id=identity, artifact_sha256=chunk_sha)
                        if revision is not None:
                            passage["terms_provenance"]["publication_revision"] = revision
                            if annotation_provenance["kind"] == "frozen_papers_matched_terms" and chunk["terms"] != annotation_terms[:PAYLOAD_TERMS_LIMIT]:
                                counts["publication_terms_metadata_updated"] += 1
                    elif annotation_provenance["kind"] == "frozen_papers_matched_terms":
                        passage["terms"] = annotation_terms[:PAYLOAD_TERMS_LIMIT]
                        passage["terms_provenance"] = annotation_provenance
                        counts["owned_chunks_terms_from_metadata"] += 1
                    if "terms" not in passage:
                        counts["owned_chunks_terms_unavailable"] += 1
                    packet(passage)
                    passages.append(passage)
                    counts["owned_chunks_recoverable"] += 1
                    counts["owned_text_bytes"] += len(chunk["text"].encode("utf-8"))
                    for key in ("section_type", "element_type", "terms"):
                        if key not in chunk:
                            counts["owned_chunks_missing_" + key] += 1
                except (ValueError, TypeError, RecursionError):
                    skipped("passage_invalid", identity)
            document = None
            try:
                if name is None:
                    raise Skip("document_unsafe_id")
                if identity_count("filename", name.casefold()) != 1:
                    raise Skip("document_filename_ambiguous")
                document = raw_document(row, name, roots, revision, chunk_data, counts, diagnostics=inventory_only)
                counts["documents_recoverable"] += 1
            except Skip as exc:
                skipped(str(exc), identity)
                if str(exc) == "document_no_bibliography":
                    counts["missing_bibliography"] += 1
            group = [(item, encoded(item) + b"\n") for item in [record, *passages]]
            counts["packet_bytes_recoverable"] += sum(len(raw) for _, raw in group)
            if document is not None:
                document_raw = encoded(document) + b"\n"
                counts["packet_bytes_recoverable"] += len(document_raw)
                if max_documents is not None and counts["document_packets"] >= max_documents:
                    counts["documents_omitted"] += 1
                else:
                    group.append((document, document_raw))
            group_bytes = sum(len(raw) for _, raw in group)
            if max_output_bytes is not None and counts["output_bytes"] + group_bytes > max_output_bytes:
                stopped = "max_output_bytes"
                break
            if stream is not None and shutil.disk_usage(output_path.parent).free < reserve_bytes + group_bytes:
                raise OSError("insufficient free space for next source bundle and disk reserve")
            for item, raw in group:
                emit(item, raw)
            if gzip and stream is not None:
                if work_path is None:
                    stream.flush()
                    raw_stream.flush()
                if disk_budget_bytes is not None and raw_stream.tell() > disk_budget_bytes:
                    raise OSError("compressed disk output budget reached; no output published")
            elif measurement_stream is not None:
                measurement_stream.flush()
        phases["artifact_seconds"] = round(time.monotonic() - artifact_started, 3)
        finalization_started = time.monotonic()
        if full_export and counts["metadata_rows"] != counts["metadata_done_total"]:
            raise ValueError("full export did not visit the entire finite metadata snapshot")
        if raw_stream is not None:
            if work_path is not None:
                save_checkpoint(seal=stream is not None)
            else:
                stream.flush()
                if gzip:
                    stream.close()
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
            counts["output_storage_bytes"] = raw_stream.tell()
            if disk_budget_bytes is not None and counts["output_storage_bytes"] > disk_budget_bytes:
                raise OSError("final disk output budget reached; no output published")
            raw_stream.close()
        if measurement_stream is not None:
            measurement_stream.close()
        storage_bytes = (counts["output_storage_bytes"] if raw_stream is not None else
                         measurement_counter.size if measurement_counter is not None else counts["packet_bytes_recoverable"])
        incomplete_sources = sum(counts[code] for code in (
            "metadata_bounds", "metadata_invalid", "source_identity_ambiguous",
            "publication_ambiguous", "publication_revision_invalid", "publication_journal_bounds",
            "publication_chunks_mismatch", "publication_cache_marker_mismatch", "publication_unjournaled_marker",
            "publication_missing", "publication_bounds", "publication_changed", "publication_unsafe_or_unreadable",
            "publication_invalid", "publication_cache_path_mismatch", "publication_marker_mismatch",
            "publication_point_mismatch", "publication_tags_mismatch", "publication_revision_mismatch"))
        partial = bool(stopped or counts["metadata_rows"] < counts["metadata_done_total"] or counts["documents_omitted"] or
                       (not inventory_only and (incomplete_sources or not full_export)))
        free = shutil.disk_usage(output_path.parent if output_path is not None else database.parent).free
        report = dict(schema=1, status="ready", mode="inventory" if inventory_only else "export",
                      coverage="db_addressed_artifacts_only", partial=partial,
                      scan="full" if scan_limit is None else "bounded_first_done_ids",
                      limit=limit, max_documents=max_documents,
                      max_sources=max_sources, max_output_bytes=max_output_bytes, stopped_by=stopped,
                      full_export=full_export, complete_generation=bool(full_export and not partial),
                      expected_metadata_rows=counts["metadata_done_total"], expected_all_metadata_rows=counts["metadata_total"],
                      expected_done_rows=counts["metadata_done_total"], artifact_scan_predicate="papers.status='done'",
                      legacy_owned_policy=dict(predicate="pipeline.extractor.should_embed",
                                               boilerplate_filter=extractor.BOILERPLATE_FILTER,
                                               publication_certified=False),
                      source_coverage=dict(recoverable_sources=counts["sources_recoverable"],
                                           exported_sources=counts["source_packets"],
                                           unresolved_source_rows=incomplete_sources,
                                           excluded_not_done=counts["source_not_done"],
                                           excluded_unpublished=counts["publication_hidden"]),
                      owned_passage_coverage=dict(chunks_recoverable=counts["owned_chunks_recoverable"],
                          missing_chunk_files=counts["chunks_missing"], invalid_chunk_files=counts["chunks_invalid"],
                          invalid_passages=counts["passage_invalid"],
                          legacy_policy_skipped=counts["legacy_owned_chunks_policy_skipped"],
                          missing_terms=counts["owned_chunks_missing_terms"],
                          terms_from_own_metadata=counts["owned_chunks_terms_from_metadata"],
                          terms_unavailable=counts["owned_chunks_terms_unavailable"],
                          terms_policy="persisted_payload_first; missing_only_frozen_same_source_matched_terms_first_12",
                          terms_provenance="hash_bound_packets_and_representation_ids; own_annotation_not_publication_certificate",
                          legacy_publication_certified=False),
                      citation_binding_coverage=dict(scope="recoverable_raw_latex_only", certified=False,
                          raw_latex=counts["latex_recoverable"], documents_with_bibliography=counts["documents_recoverable"],
                          missing_bibliography=counts["missing_bibliography"], stale_sidecars=counts["references_stale"],
                          invalid_sidecars=counts["references_invalid"],
                          diagnostics_scope="inventory_parsed" if inventory_only else "deferred_to_producer",
                          unresolved_mentions=(counts["citation_unknown_reference"] + counts["citation_unresolved_reference"]
                                               if inventory_only else None),
                          ambiguous_bindings=(counts["citation_ambiguous_identifiers"] + counts["citation_conflicting_duplicate_key"]
                                              if inventory_only else None)),
                      sample_size=sample_size if inventory_only and not full_scan else None,
                      counts=dict(counts), diagnostics=dict(samples=samples, sample_limit=32,
                                                           samples_truncated=counts["diagnostic_events"] > len(samples)),
                      metadata_snapshot_sha256=snapshot["sha256"], metadata_snapshot=snapshot,
                      artifact_facts_sha256=facts.hexdigest(),
                      packets_sha256=digest.hexdigest() if raw_stream is not None else None,
                      artifact_facts_hash_algorithm="sha256_chained_updates_v1",
                      metadata_normalization=dict(
                          display_title="fold_space_tab_cr_lf_formfeed; preserve_frozen_raw_metadata",
                          invalid_optional_url="null_with_diagnostic; no_guessed_link",
                          titles_changed=counts["metadata_title_whitespace_normalized"],
                          urls_omitted=counts["invalid_source_url_omitted"]),
                      paths={"db": str(database), **{k: str(v) for k, v in roots.items()}},
                      output=str(output_path) if output_path is not None else None,
                      packet_file=packet_filename, compression="gzip" if gzip else "none",
                      gzip_level=gzip_level if gzip else None,
                      packets_sha256_scope="decoded_exact_jsonl_bytes",
                      publication_visibility="local_read_snapshot; legacy_unmarked_not_certified",
                      live_visibility_verified=False, evidence_state="not_read",
                      plan=dict(measured_packet_bytes=counts["packet_bytes_recoverable"],
                                measured_storage_bytes=storage_bytes,
                                measurement_scope="sample" if stopped or counts["metadata_rows"] < counts["metadata_done_total"] else "full_db_addressed_scan",
                                free_bytes=free, reserve_bytes=reserve_bytes,
                                disk_budget_bytes=disk_budget_bytes,
                                catalog_estimate_bytes=8 * counts["packet_bytes_recoverable"],
                                combined_peak_estimate_bytes=storage_bytes + 8 * counts["packet_bytes_recoverable"] + reserve_bytes,
                                estimate_certified=False,
                                note="8x catalog heuristic, not an upper bound; no full-corpus extrapolation"))
        phases["finalization_seconds"] = round(time.monotonic() - finalization_started, 3)
        phases["total_seconds"] = round(time.monotonic() - started, 3)
        report["phase_elapsed"] = phases
        if raw_stream is not None:
            report_path = temporary / "report.json"
            if work_path is not None and os.path.lexists(report_path):
                prior = bounded_json(report_path)
                keys = ("schema", "mode", "output", "packet_file", "packets_sha256",
                        "metadata_snapshot_sha256", "full_export", "partial", "complete_generation", "counts")
                if not isinstance(prior, dict) or any(prior.get(key) != report.get(key) for key in keys):
                    raise ValueError("durable final report identity mismatch")
                report = prior
            else:
                atomic_json(report_path, report)
            sync_directory(temporary)
            try:
                rename_new(temporary, output_path)
                sync_directory(output_path.parent)
                progress("ready", counts["metadata_rows"])
            except ExportInterrupted:
                if work_path is not None and os.path.lexists(output_path):
                    return published_report(work_path, output_path)
                raise
        else:
            progress("ready", counts["metadata_rows"])
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("db", "latex-cache", "fulltext-cache", "chunks", "publication-cache"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--section-cache", type=Path, help="source/chunk hash binding required for marked documents")
    parser.add_argument("--spool-dir", type=Path, help="owned temporary metadata SQLite parent; defaults to output parent or system temp")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="new immutable directory containing packets.jsonl[.gz] and report.json")
    mode.add_argument("--inventory-only", "--inventory", "--plan", dest="inventory_only", action="store_true")
    parser.add_argument("--sample-size", type=int, default=100, help="inventory artifact sample, first N sorted DB IDs")
    parser.add_argument("--full-scan", action="store_true", help="explicit full inventory artifact scan")
    parser.add_argument("--limit", type=int, help="metadata row scan cap; omitted rows imply partial=true")
    parser.add_argument("--max-documents", type=int, help="export document cap; omitted documents imply partial=true")
    parser.add_argument("--max-sources", type=int, help="whole-source shard cap; always labelled partial")
    parser.add_argument("--max-output-bytes", type=int, help="whole-source decoded JSONL byte cap; always labelled partial")
    parser.add_argument("--reserve-bytes", type=int, default=2 * 1024**3, help="minimum remaining free bytes; default 2 GiB")
    parser.add_argument("--full-export", action="store_true", help="entire finite DB snapshot; forbids all shard limits")
    parser.add_argument("--disk-budget-bytes", type=int, help="abort-only physical output ceiling; never publish a truncated full export")
    parser.add_argument("--gzip", action="store_true", help="stream gzip packets directly; inventory measures gzip with no files")
    parser.add_argument("--gzip-level", type=int, default=6, choices=range(1, 10), help="gzip compression level; default 6")
    parser.add_argument("--work-dir", type=Path, help="new durable full-export gzip work directory outside inputs/output")
    parser.add_argument("--resume", action="store_true", help="verify and resume the same durable work directory")
    parser.add_argument("--checkpoint-rows", type=int, default=250, help="seal a recoverable whole-source batch; default 250")
    parser.add_argument("--max-seconds", type=float, help="pause without publication after this per-run duration")
    args = parser.parse_args(argv)
    handlers = {}

    def interrupted(signum, frame):
        raise ExportInterrupted()

    try:
        if args.work_dir is not None:
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, interrupted)
        report = export_packets(**vars(args))
    except ExportInterrupted:
        if args.work_dir is not None and args.output is not None and os.path.lexists(args.output):
            try:
                report = published_report(args.work_dir, args.output)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print(encoded(dict(status="error", error="publication interrupted: " + str(exc))).decode("utf-8"))
                return 2
            print(encoded(report).decode("utf-8"))
            return 0
        print(encoded(dict(status="paused", partial=True, complete_generation=False,
                           work_dir=str(args.work_dir), reason="signal", output=None)).decode("utf-8"))
        return 3
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
        print(encoded(dict(status="error", error=str(exc))).decode("utf-8"))
        return 2
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    print(encoded(report).decode("utf-8"))
    return 3 if report["status"] == "paused" else 0


if __name__ == "__main__":
    raise SystemExit(main())
