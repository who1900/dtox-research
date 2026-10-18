"""Bounded, source-ordered section read views; no parsing or index writes."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 10000
PARSER_MODES = {"pylatexenc", "pylatexenc_recovered", "markdown"}
SECTION_TYPES = {"abstract", "introduction", "related_work", "method", "experiments",
                 "analysis", "limitations", "conclusion", "appendix", "other"}


class ManifestInvalid(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_id(paper_id):
    # Existing ACL metadata stores these records with their source URL embedded
    # in the ID. Preserve that identity and the historical cache filename, but
    # accept only the known LREC proceedings URL form.
    if isinstance(paper_id, str) and paper_id.startswith("acl:https://"):
        url = paper_id[len("acl:"):]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "lrec.elra.info"
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or not re.fullmatch(r"/lrec2026-(?:main-\d{3}|ws-[a-z0-9]+-\d{2})", parsed.path)):
            raise ManifestInvalid("unsafe paper ID")
        return paper_id.replace(":", "_").replace("/", "_")
    if (not isinstance(paper_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,180}", paper_id)
            or ".." in paper_id or paper_id.endswith((".", ":", "/"))
            or re.match(r"^[A-Za-z]:", paper_id)
            or any(not part for part in paper_id.split("/"))):
        raise ManifestInvalid("unsafe paper ID")
    name = paper_id.replace(":", "_").replace("/", "_")
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name):
        raise ManifestInvalid("reserved paper ID")
    return name


def _plain_path(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ManifestInvalid("path traversal")
    for component in (*reversed(path.parents), path):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ManifestInvalid("symlinked document path")
    return path


def _cache_directory(path):
    path = _plain_path(path)
    if not path.is_dir():
        raise ManifestInvalid("configured document cache directory is missing")
    return path


def document_paths(root, paper_id, *, cache_dir=None, section_cache_dir=None):
    root = _plain_path(root)
    name = safe_id(paper_id) + ".json"
    cache_root = (_cache_directory(cache_dir) if cache_dir is not None
                  else _plain_path(root / "chunk_cache"))
    section_root = (_cache_directory(section_cache_dir) if section_cache_dir is not None
                    else _plain_path(root / "section_cache"))
    return (_plain_path(cache_root / name), _plain_path(section_root / name))


def read_bounded(path, limit=MAX_BYTES):
    path = _plain_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ManifestInvalid("document exceeds byte bound or is not regular")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ManifestInvalid("document changed during bounded read")
    _plain_path(path)
    return data


def _label(value):
    return (isinstance(value, str) and len(value) <= MAX_BYTES
            and not any(ord(char) < 32 for char in value))


def make_section_manifest(paper_id, chunks, cache_bytes, source_bytes, parser_mode, parser_revision):
    safe_id(paper_id)
    if parser_mode not in PARSER_MODES or not isinstance(parser_revision, (str, int)) or isinstance(parser_revision, bool):
        raise ManifestInvalid("unverified parser mode/revision")
    if not str(parser_revision) or len(str(parser_revision)) > 128:
        raise ManifestInvalid("invalid parser revision")
    if (not isinstance(cache_bytes, bytes) or not isinstance(source_bytes, bytes)
            or max(len(cache_bytes), len(source_bytes)) > MAX_BYTES
            or not isinstance(json.loads(cache_bytes), list)):
        raise ManifestInvalid("invalid cache/source snapshot")
    if not isinstance(chunks, list) or not chunks or len(chunks) > MAX_CHUNKS:
        raise ManifestInvalid("invalid source-ordered chunks")
    sections, size = [], 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ManifestInvalid("invalid parsed chunk")
        title, kind, text = chunk.get("section_title", ""), chunk.get("section_type", "other"), chunk.get("text")
        if not _label(title) or kind not in SECTION_TYPES or not isinstance(text, str):
            raise ManifestInvalid("invalid parsed section")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        total, part = chunk.get("part_total", 1), chunk.get("part_index", 0)
        if type(total) is not int or type(part) is not int or total != 1 or part != 0:
            raise ManifestInvalid("split chunks cannot prove canonical order; supply unsplit section_sink records")
        size += len(text.encode("utf-8"))
        if size > MAX_BYTES:
            raise ManifestInvalid("canonical text exceeds byte bound")
        if not sections or (sections[-1]["section_title"], sections[-1]["section_type"]) != (title, kind):
            sections.append({"section_title": title, "section_type": kind,
                             "source_order": len(sections), "text": ""})
        assembled = sections[-1]["text"]
        sections[-1]["text"] = assembled + ("\n\n" if assembled and text else "") + text
    manifest = {"schema": SCHEMA_VERSION, "paper_id": paper_id,
                "source_sha256": sha256(source_bytes), "cache_sha256": sha256(cache_bytes),
                "parser_mode": parser_mode, "parser_revision": str(parser_revision),
                "coverage": "parsed_source", "sections": sections}
    return validate_section_manifest(manifest, paper_id, cache_bytes, source_bytes)


def validate_section_manifest(manifest, paper_id, cache_bytes=None, source_bytes=None):
    safe_id(paper_id)
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest["schema"] != SCHEMA_VERSION or manifest.get("paper_id") != paper_id
            or manifest.get("parser_mode") not in PARSER_MODES
            or not _label(manifest.get("parser_revision")) or not manifest["parser_revision"]
            or manifest.get("coverage") not in {"parsed_source", "examined_sections"}):
        raise ManifestInvalid("invalid manifest identity/parser contract")
    for name, data in (("cache_sha256", cache_bytes), ("source_sha256", source_bytes)):
        value = manifest.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) or (data is not None and sha256(data) != value):
            raise ManifestInvalid("manifest snapshot hash mismatch")
    sections = manifest.get("sections")
    if not isinstance(sections, list) or not sections or len(sections) > MAX_CHUNKS:
        raise ManifestInvalid("invalid canonical sections")
    for index, section in enumerate(sections):
        if (not isinstance(section, dict) or not _label(section.get("section_title"))
                or section.get("section_type") not in SECTION_TYPES
                or type(section.get("source_order")) is not int or section["source_order"] != index
                or not isinstance(section.get("text"), str)):
            raise ManifestInvalid("invalid canonical section order/text")
    if len(json.dumps(manifest, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ManifestInvalid("manifest exceeds byte bound")
    return manifest


def manifest_bytes(manifest):
    validate_section_manifest(manifest, manifest.get("paper_id"))
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def publish_section_manifest(path, manifest, *, cache_dir=None):
    data = manifest_bytes(manifest)
    path = _plain_path(path)
    directory_matches = (path.parent.name == "section_cache" if cache_dir is None
                         else path.parent == _plain_path(cache_dir))
    if not directory_matches or path.name != safe_id(manifest["paper_id"]) + ".json":
        raise ManifestInvalid("manifest path does not match paper ID")
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".section-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_path(path)
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_section_manifest(root, paper_id, *, cache_dir=None, section_cache_dir=None):
    cache_path, manifest_path = document_paths(
        root, paper_id, cache_dir=cache_dir, section_cache_dir=section_cache_dir)
    cache = read_bounded(cache_path)
    if not isinstance(json.loads(cache), list):
        raise ManifestInvalid("cache must remain a JSON list")
    manifest = json.loads(read_bounded(manifest_path))
    validate_section_manifest(manifest, paper_id, cache)
    if read_bounded(cache_path) != cache:
        raise ManifestInvalid("cache changed while loading manifest")
    return manifest
