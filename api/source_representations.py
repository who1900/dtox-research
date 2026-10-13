"""Offline source discovery from caller records, never evidence or a corpus reader.

Public API: build_catalog(path, sources, representations, *, generation_id=None)
and search_catalog(path, query, *, layer=None, year_from=None, year_to=None,
limit=20, timeout=0.25, canonical_id=None, section_type=None, element_type=None,
terms=None). Sources require source_id/title/year/layers/status/publication_revision;
source_url is optional, nullable HTTP(S) metadata, never bibliographic resolution.

origin_revision MUST be SHA-256 of the exact text-owner source bytes, NOT a
publication UUID. Locators address that raw artifact: start/end (exclusive), unit
unicode_codepoint or utf8_byte, optional section_type/element_type and artifact
(an opaque bounded string, e.g. metadata_abstract or latex_source). Rendered text
and summaries need not have the locator span's length. The producer verifies offsets
and explicit citation exact_key/identifier bindings; this module validates shape
and ownership only. Optional representation section_type/element_type/terms are
caller-supplied tags, not inferred from foreign text.

status='done' is NOT a live publication certificate. publication_revision=None is
legacy_unmarked; a SHA-256 revision is published_generation_snapshot, not proof of
current live publication. The integration MUST check the live journal separately.
Canonicalizers are caller-supplied pure ID mappings, never resource/model calls.

PROVENANCE_SCHEMA_VERSION=1 is an additive catalog-v1 extension, not evidence.
Payloads retain exact JSON provenance; occurrences retain every resolved lead,
including duplicate and non-indexed targets. Both tables participate in the digest.
"""
import hashlib
from bisect import insort
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit


KINDS = ("own_summary", "own_passage", "citation_context")
MAX_REPRESENTATIONS = 128
MAX_SOURCES = 20
MAX_ORIGINS = 3
MAX_CONTEXTS = 8
_NATIVE_INELIGIBLE = object()
_APP_ID = 0x53524352
_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+~-]{0,255}\Z")
_DOI = re.compile(r"(?:doi:)?10\.\d{4,9}/[^\s\x00-\x1f\x7f]+\Z", re.IGNORECASE)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_KEYS = {"source_id", "title", "year", "layers", "status", "publication_revision"}
_REP_KEYS = {"kind", "source_id", "origin_id", "origin_revision", "representation_id",
             "locator", "text", "binding", "binding_status"}
_SCHEMA = """
CREATE TABLE catalog (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
 generation TEXT NOT NULL, coverage TEXT NOT NULL);
CREATE TABLE sources (
 source_id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER,
 layers TEXT NOT NULL, status TEXT NOT NULL, publication_revision TEXT, source_url TEXT
);
CREATE TABLE source_layers (
 source_id TEXT NOT NULL REFERENCES sources(source_id), layer TEXT NOT NULL,
 PRIMARY KEY(source_id,layer)
);
CREATE TABLE representations (
 id INTEGER PRIMARY KEY, representation_id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(source_id),
 origin_id TEXT NOT NULL REFERENCES sources(source_id), origin_revision TEXT NOT NULL,
 locator TEXT NOT NULL, text TEXT NOT NULL, binding TEXT, binding_status TEXT NOT NULL,
 section_type TEXT, element_type TEXT, terms TEXT NOT NULL,
 indexed_text_sha256 TEXT NOT NULL
);
CREATE INDEX representations_target ON representations(source_id,kind);
CREATE INDEX representations_origin ON representations(origin_id);
CREATE INDEX representations_owned_text ON representations(source_id,indexed_text_sha256)
 WHERE kind='own_passage' AND origin_id=source_id;
CREATE TABLE representation_terms (
 representation_id INTEGER NOT NULL REFERENCES representations(id), term TEXT NOT NULL,
 PRIMARY KEY(representation_id,term)
);
CREATE TABLE provenance_payloads (
 provenance_id TEXT PRIMARY KEY, origin_revision TEXT NOT NULL,
 bibliography_sha256 TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE provenance_occurrences (
 origin_id TEXT NOT NULL REFERENCES sources(source_id), origin_revision TEXT NOT NULL,
 bibliography_sha256 TEXT NOT NULL,
 context_start INTEGER NOT NULL CHECK(context_start>=0),
 context_end INTEGER NOT NULL CHECK(context_end>=context_start),
 occurrence_ordinal INTEGER NOT NULL CHECK(occurrence_ordinal>=0),
 requested_target TEXT NOT NULL, exact_key TEXT NOT NULL,
 citation_start INTEGER NOT NULL CHECK(citation_start>=context_start),
 citation_end INTEGER NOT NULL CHECK(citation_end>=citation_start AND citation_end<=context_end),
 provenance_id TEXT NOT NULL REFERENCES provenance_payloads(provenance_id),
 PRIMARY KEY(origin_id,origin_revision,bibliography_sha256,occurrence_ordinal)
);
CREATE INDEX provenance_occurrences_payload ON provenance_occurrences(provenance_id);
CREATE VIRTUAL TABLE representation_fts USING fts5(text, content='representations', content_rowid='id');
"""


def _string(value, field, maximum=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"invalid {field}")
    return value


def _identifier(value):
    if (not isinstance(value, str) or len(value) > 256
            or not (_ID.fullmatch(value) or _DOI.fullmatch(value))):
        raise ValueError("invalid identifier")
    return value


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("revision must be lowercase SHA-256")
    return value


def validate_coverage(value):
    """Coverage is snapshot provenance, never scientific/live-publication proof."""
    if value is None:
        return {"version": 1, "scope": "explicit_input_only", "partial": True,
                "input_sha256": None, "export_report_sha256": None}
    keys = {"version", "scope", "partial", "input_sha256", "export_report_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int
            or value["version"] != 1 or value["scope"] not in ("full_snapshot", "explicit_input_only")
            or type(value["partial"]) is not bool):
        raise ValueError("invalid catalog coverage")
    for key in ("input_sha256", "export_report_sha256"):
        if value[key] is not None:
            _sha(value[key])
    if value["scope"] == "full_snapshot" and (value["partial"] or value["input_sha256"] is None
                                             or value["export_report_sha256"] is None):
        raise ValueError("full snapshot requires complete hash-bound export provenance")
    return dict(value)


def coverage_is_complete(value):
    value = validate_coverage(value)
    return value["scope"] == "full_snapshot" and value["partial"] is False


def catalog_coverage(path, *, timeout=0.25):
    deadline = time.monotonic() + timeout
    _check_deadline(deadline)
    conn = _connect(Path(path).absolute())
    try:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        row = conn.execute("SELECT coverage FROM catalog WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("missing catalog coverage")
        result = validate_coverage(json.loads(row[0]))
        _check_deadline(deadline)
        return result
    finally:
        conn.close()


def _canonical(value, canonicalize):
    return _identifier(canonicalize(_identifier(value)))


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 65536
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise ValueError("invalid representation text")
    value.encode("utf-8")
    return value


def _url(value):
    if value is None:
        return None
    _string(value, "source_url", 4096)
    if any(c.isspace() for c in value):
        raise ValueError("invalid source_url")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("invalid source_url")
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid source_url") from exc
    return value


def _terms(values):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ValueError("terms must be a bounded array")
    return sorted({_string(value, "term").casefold() for value in values})


def _source(record, canonicalize):
    if (not isinstance(record, dict) or not _SOURCE_KEYS <= set(record)
            or set(record) - _SOURCE_KEYS - {"source_url"}):
        raise ValueError("source fields do not match schema")
    year = record["year"]
    if year is not None and (type(year) is not int or not 1 <= year <= 9999):
        raise ValueError("invalid year")
    layers = record["layers"]
    if not isinstance(layers, list) or len(layers) > 32:
        raise ValueError("layers must be a bounded JSON array")
    layers = sorted({_string(layer, "layer") for layer in layers})
    return (_canonical(record["source_id"], canonicalize),
            _string(record["title"], "title", 4096), year, _json(layers),
            _string(record["status"], "status"),
            _sha(record["publication_revision"]) if record["publication_revision"] is not None else None,
            _url(record.get("source_url")))


def _representation(record, canonicalize):
    if (not isinstance(record, dict) or not _REP_KEYS <= set(record)
            or set(record) - _REP_KEYS - {"section_type", "element_type", "terms"}):
        raise ValueError("representation fields do not match schema")
    kind = record["kind"]
    if kind not in KINDS or record["binding_status"] != "resolved":
        raise ValueError("unsupported kind or unresolved binding")
    target = _canonical(record["source_id"], canonicalize)
    origin = _canonical(record["origin_id"], canonicalize)
    locator = record["locator"]
    required = {"start", "end", "unit"}
    if (not isinstance(locator, dict) or not required <= set(locator)
            or set(locator) - required - {"section_type", "element_type", "artifact"}
            or type(locator["start"]) is not int or type(locator["end"]) is not int
            or not 0 <= locator["start"] < locator["end"] <= 2**63 - 1
            or locator["unit"] not in ("unicode_codepoint", "utf8_byte")):
        raise ValueError("invalid exact-origin locator")
    for field in ("section_type", "element_type", "artifact"):
        if field in locator:
            _string(locator[field], field)
    binding = record["binding"]
    if kind == "citation_context":
        if (not isinstance(binding, dict) or not {"exact_key", "identifier"} <= set(binding)
                or set(binding) - {"exact_key", "identifier", "self_citation"}):
            raise ValueError("explicit citation key and identifier required")
        _string(binding["exact_key"], "exact_key", 1024)
        if _canonical(binding["identifier"], canonicalize) != target:
            raise ValueError("citation identifier does not bind the target")
        if "self_citation" in binding and type(binding["self_citation"]) is not bool:
            raise ValueError("self_citation must be boolean")
        if origin == target and binding.get("self_citation") is not True:
            raise ValueError("self citation requires explicit binding")
    elif origin != target or binding is not None:
        raise ValueError("own text must belong to target and have no citation binding")
    text = _text(record["text"])
    metadata = {}
    for field in ("section_type", "element_type"):
        value = record.get(field, locator.get(field))
        if value is not None:
            _string(value, field)
            if field in locator and locator[field] != value:
                raise ValueError("conflicting locator and representation tags")
        metadata[field] = value
    terms = _terms(record.get("terms"))
    return (_identifier(record["representation_id"]), kind, target, origin,
            _sha(record["origin_revision"]), _json(locator), text,
            _json(binding) if binding is not None else None, "resolved",
            metadata["section_type"], metadata["element_type"], _json(terms))


def _configure_builder(conn):
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA cache_size=-8192")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA temp.cache_size=-2048")
    conn.execute("PRAGMA mmap_size=0")


def _insert_source(conn, record):
    row = _source(record, lambda value: value)
    prior = conn.execute("SELECT * FROM sources WHERE source_id=?", (row[0],)).fetchone()
    if prior is not None:
        if prior != row:
            raise ValueError("conflicting canonical source metadata")
        return
    conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)", row)
    conn.executemany("INSERT INTO source_layers VALUES (?,?)",
                     [(row[0], layer) for layer in json.loads(row[3])])


def _insert_representation(conn, record):
    row = _representation(record, lambda value: value)
    cursor = conn.execute(
        "INSERT INTO representations(representation_id,kind,source_id,origin_id,"
        "origin_revision,locator,text,binding,binding_status,section_type,element_type,terms,indexed_text_sha256) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*row, hashlib.sha256(row[6].encode()).hexdigest()))
    conn.execute("INSERT INTO representation_fts(rowid,text) VALUES (?,?)", (cursor.lastrowid, row[6]))
    conn.executemany("INSERT INTO representation_terms VALUES (?,?)",
                     [(cursor.lastrowid, term) for term in json.loads(row[11])])


def _insert_provenance_occurrence(conn, owner, origin_revision, bibliography_sha256,
                                  context, lead, ordinal):
    """Append one lead; payload identity hashes [artifact SHA, bibliography SHA, full payload]."""
    _sha(origin_revision)
    _sha(bibliography_sha256)
    provenance = lead["provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise ValueError("resolved lead requires full provenance")
    payload = _json(provenance)
    provenance_id = hashlib.sha256(_json([origin_revision, bibliography_sha256, provenance]).encode("utf-8")).hexdigest()
    stored = (origin_revision, bibliography_sha256, payload)
    conn.execute("INSERT OR IGNORE INTO provenance_payloads VALUES (?,?,?,?)", (provenance_id, *stored))
    if conn.execute("SELECT origin_revision,bibliography_sha256,payload FROM provenance_payloads "
                    "WHERE provenance_id=?", (provenance_id,)).fetchone() != stored:
        raise ValueError("conflicting provenance payload identity")
    conn.execute("INSERT INTO provenance_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (owner, origin_revision, bibliography_sha256, context["start"], context["end"], ordinal,
                  lead["target_id"], lead["key"], lead["citation_start"], lead["citation_end"], provenance_id))


def _finish_catalog(conn, *, generation_id=None, coverage=None, check=lambda: None):
    if generation_id is not None:
        _sha(generation_id)
    digest = hashlib.sha256()
    for table, columns, order in (
            ("sources", "*", "source_id"),
            ("representations", "representation_id,kind,source_id,origin_id,origin_revision,"
             "locator,text,binding,binding_status,section_type,element_type,terms,indexed_text_sha256", "representation_id"),
            ("provenance_payloads", "provenance_id,origin_revision,bibliography_sha256,payload", "provenance_id"),
            ("provenance_occurrences", "origin_id,origin_revision,bibliography_sha256,context_start,context_end,occurrence_ordinal,"
             "requested_target,exact_key,citation_start,citation_end,provenance_id",
             "origin_id,origin_revision,bibliography_sha256,context_start,context_end,occurrence_ordinal")):
        if table == "provenance_payloads":
            digest.update(b"provenance_schema\0" + str(PROVENANCE_SCHEMA_VERSION).encode("ascii") + b"\0")
        digest.update(table.encode("ascii") + b"\0")
        for index, row in enumerate(conn.execute(f"SELECT {columns} FROM {table} ORDER BY {order}")):
            if index % 1024 == 0:
                check()
            digest.update(_json(list(row)).encode("utf-8") + b"\n")
    coverage_provided = coverage is not None
    coverage_record = validate_coverage(coverage() if callable(coverage) else coverage)
    digest.update(b"coverage\0" + _json(coverage_record).encode())
    if generation_id is not None and coverage_provided:
        digest.update(b"generation_seed\0" + generation_id.encode())
    generation = generation_id if generation_id is not None and not coverage_provided else digest.hexdigest()
    check()
    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ValueError("catalog integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("catalog foreign key check failed")
    conn.execute("INSERT INTO representation_fts(representation_fts,rank) VALUES ('integrity-check',1)")
    check()
    conn.execute("INSERT INTO catalog VALUES (1,?,?,?)", (_VERSION, generation, _json(coverage_record)))
    return {"sources": [], "partial": False, "status": "ready", "generation": generation,
            "diagnostics": {"content_sha256": digest.hexdigest()}, "coverage": coverage_record}


def _publish_catalog(temporary, target, *, keep=False):
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.link(temporary, target)
    if not keep:
        temporary.unlink()
    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    if os.name != "nt":
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _build_catalog(generation_path, sources, representations, *, generation_id=None,
                   stage_dir=None, coverage=None):
    """Explicit offline build into a NEW path; atomic, no-clobber publication.

Parent and optional caller-owned staging directory must already exist on the same
filesystem. Only our temporary file is cleaned up. No production DB is opened.
Published generations are read-only; changing inputs requires a different path.
"""
    if generation_id is not None:
        _sha(generation_id)
    target = Path(generation_path).absolute()
    if os.path.lexists(target):
        raise FileExistsError("generation already exists")
    parent = target.parent.resolve(strict=True)
    stage = Path(stage_dir).resolve(strict=True) if stage_dir is not None else parent
    if not stage.is_dir() or stage.stat().st_dev != parent.stat().st_dev:
        raise ValueError("staging directory must be on publication filesystem")
    target = parent / target.name
    fd, name = tempfile.mkstemp(prefix=".source-representations-", suffix=".sqlite", dir=stage)
    os.close(fd)
    temporary = Path(name)
    conn = None
    try:
        conn = sqlite3.connect(temporary)
        _configure_builder(conn)
        conn.execute(f"PRAGMA application_id={_APP_ID}")
        conn.execute(f"PRAGMA user_version={_VERSION}")
        conn.executescript(_SCHEMA)
        for record in sources:
            _insert_source(conn, record)
        for record in representations:
            _insert_representation(conn, record)
        built = _finish_catalog(conn, generation_id=generation_id, coverage=coverage)
        conn.commit()
        conn.close()
        conn = None
        _publish_catalog(temporary, target)
        return built
    finally:
        if conn is not None:
            conn.close()
        temporary.unlink(missing_ok=True)


def build_catalog(path, sources, representations, *, generation_id=None, coverage=None):
    """Build only an explicit new generation path; existing paths are never replaced.

    The temporary build lives beside path. The internal builder also permits a
    caller-owned same-filesystem staging directory, without cleaning its contents.
    generation_id, if supplied, must be SHA-256; otherwise hash normalized record
    content in stable ID order. No filesystem timestamps or corpus DB scans.
    """
    return _build_catalog(path, sources, representations, generation_id=generation_id, coverage=coverage)


def _safe_fts_query(query):
    """Bound literal tokens; no caller FTS operators, column syntax or prefixes."""
    if not isinstance(query, str):
        raise ValueError("query must be text")
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query[:4096], re.UNICODE)))[:32]
    return " OR ".join('"' + token[:128].replace('"', '""') + '"' for token in tokens)


def _cap(value, maximum):
    if type(value) is not int or value < 1:
        raise ValueError("limits must be positive integers")
    return min(value, maximum)


def _aggregate_rank_lists(rank_lists, *, canonicalize, source_limit=MAX_SOURCES,
                          _maximum=MAX_SOURCES):
    """Canonical target aggregation BEFORE RRF: one vote per source per channel.

Lists contain source IDs, not fragments or scores. Duplicate aliases do not occupy
rank positions. At most 128 entries per channel and 20 output sources are consumed.
RRF scores are ranking weights, never probabilities.
"""
    limit = _cap(source_limit, _maximum)
    scores, ranks = {}, {}
    for channel, ids in rank_lists.items():
        _string(channel, "channel")
        seen = set()
        for index, source_id in enumerate(ids):
            if index >= MAX_REPRESENTATIONS:
                break
            source_id = _canonical(source_id, canonicalize)
            if source_id in seen:
                continue
            seen.add(source_id)
            rank = len(seen)
            scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (60 + rank)
            ranks.setdefault(source_id, {})[channel] = rank
    ordered = sorted(scores, key=lambda source_id: (-scores[source_id], source_id))[:limit]
    return [{"source_id": source_id, "rrf_score": scores[source_id],
             "channel_ranks": ranks[source_id]} for source_id in ordered]


class _Deadline(Exception):
    pass


class _NativeMappingLimit(ValueError):
    pass


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise _Deadline


def _connect(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    conn.row_factory = sqlite3.Row
    return conn


class CatalogReader:
    """Bounded immutable reader; exact rep-ID lookups never enumerate the catalog.

    One deadline covers all operations in this context. Caller callbacks must
    honour their passed timeout; Python cannot preempt a synchronous callback.
    """

    def __init__(self, path, generation, *, deadline, canonical_id=None,
                 layer=None, year_from=None, year_to=None, section_type=None,
                 element_type=None, terms=None, independent_alias_revisions=False):
        if type(independent_alias_revisions) is not bool:
            raise ValueError("invalid independent alias revision opt-in")
        self._independent_alias_revisions = independent_alias_revisions
        self.path = Path(path).absolute()
        self.generation = _sha(generation)
        self.deadline = deadline
        self.canonical = canonical_id or (lambda value: value)
        self.filters = (layer, year_from, year_to, section_type, element_type, _terms(terms))
        self.metadata = {}
        self.qualifiers = {}
        self.conn = None

    def __enter__(self):
        _check_deadline(self.deadline)
        self.conn = _connect(self.path)
        try:
            self.conn.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 100)
            self.conn.execute("PRAGMA query_only=ON")
            if (self.conn.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
                    or self.conn.execute("PRAGMA user_version").fetchone()[0] != _VERSION):
                raise ValueError("invalid catalog header")
            row = self.conn.execute("SELECT version,generation FROM catalog WHERE singleton=1").fetchone()
            if row is None or tuple(row) != (_VERSION, self.generation):
                raise ValueError("catalog generation mismatch")
            self.conn.execute("SELECT indexed_text_sha256 FROM representations LIMIT 0")
            coverage = self.conn.execute("SELECT coverage FROM catalog WHERE singleton=1").fetchone()
            self.coverage = validate_coverage(json.loads(coverage[0]))
            self.conn.create_function("catalog_canonical", 1,
                                      lambda value: _canonical(value, self.canonical), deterministic=True)
            return self
        except BaseException:
            self.conn.close()
            self.conn = None
            raise

    def __exit__(self, *args):
        self.conn.close()

    def source(self, source_id):
        _check_deadline(self.deadline)
        return _metadata(self.conn, source_id, self.metadata)

    def qualify(self, record):
        layer, low, high, section, element, terms = self.filters
        target_id = _canonical(record["source_id"], self.canonical)
        owner_id = _canonical(record["origin_id"], self.canonical)
        indexed = self.source(record["source_id"])
        owner = self.source(record["origin_id"])
        target = self.source(target_id) or indexed
        canonical_owner = self.source(owner_id) or owner
        if indexed is None or owner is None or target is None:
            raise ValueError("missing target or owner")
        if any(item["status"] != "done" for item in (indexed, owner, target, canonical_owner)):
            return None
        if record["kind"] == "citation_context":
            if _canonical(record["binding"]["identifier"], self.canonical) != target_id:
                raise ValueError("invalid canonical binding")
            if target_id == owner_id and record["binding"].get("self_citation") is not True:
                raise ValueError("unmarked canonical self citation")
        if layer is not None and layer not in target["layers"]:
            return None
        year = target["year"]
        if (low is not None or high is not None) and (year is None
                or low is not None and year < low or high is not None and year > high):
            return None
        owned = []
        if section is not None or element is not None or terms:
            ids = tuple(dict.fromkeys((target_id, record["source_id"])))
            if ids not in self.qualifiers:
                self.qualifiers[ids] = _owned(self.conn, ids, section, element, terms, 1)
            owned = self.qualifiers[ids]
            if not owned:
                return None
        return target, owner, owned

    def hydrate(self, representation_ids):
        """Return source slots for <=128 exact IDs, validating owner/binding/target."""
        if not isinstance(representation_ids, (list, tuple)) or len(representation_ids) > MAX_REPRESENTATIONS:
            raise ValueError("bounded representation IDs required")
        ids = list(dict.fromkeys(_identifier(value) for value in representation_ids))
        if not ids:
            return {}
        _check_deadline(self.deadline)
        rows = self.conn.execute("SELECT * FROM representations WHERE representation_id IN ("
                                 + ",".join("?" for _ in ids) + ")", ids).fetchall()
        found = {row["representation_id"]: _record(row) for row in rows}
        if set(found) != set(ids):
            raise ValueError("unknown representation IDs")
        slots = {}
        for rep_id in ids:
            _check_deadline(self.deadline)
            record = found[rep_id]
            eligible = self.qualify(record)
            if eligible is None:
                continue
            target, owner, owned = eligible
            source_id = _canonical(record["source_id"], self.canonical)
            slot = slots.setdefault(source_id, {**target, "source_id": source_id,
                "representations": [], "evidence_state": "not_read", "verification_required": True})
            for item in [record, *owned]:
                if any(r["representation_id"] == item["representation_id"] for r in slot["representations"]):
                    continue
                origin = owner if item is record else self.source(item["origin_id"])
                if origin is None or origin["status"] != "done":
                    raise ValueError("invalid qualifying owner")
                rep = {k: v for k, v in item.items() if k not in ("source_id", "origin_id")}
                rep.update(target_source_id=source_id, indexed_source_id=item["source_id"],
                           origin_source_id=item["origin_id"], origin_title=origin["title"],
                           origin_publication_revision=origin["publication_revision"],
                           origin_publication_state=origin["publication_state"], evidence_state="not_read")
                slot["representations"].append(rep)
        return slots

    def lexical(self, query, limit=MAX_REPRESENTATIONS):
        """Adaptive ranked prefixes, eligible canonical heads, then alternatives.

        No chunk cap: duplicate chunks cannot consume another source's slot.
        Finish the boundary rank bucket before deterministic canonical/rep ties.
        Only observed rows are checked; the builder validates the whole catalog.
        """
        limit = _cap(limit, MAX_REPRESENTATIONS)
        match = _safe_fts_query(query)
        if not match:
            return []
        candidates, boundary = {}, None
        window = MAX_REPRESENTATIONS + 1
        while True:
            _check_deadline(self.deadline)
            cursor = self.conn.execute(
                "SELECT rowid,rank FROM representation_fts WHERE representation_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, window))
            fetched, complete = 0, False
            try:
                for hit in cursor:
                    _check_deadline(self.deadline)
                    fetched += 1
                    score = hit["rank"]
                    if type(score) not in (int, float) or not math.isfinite(score):
                        raise ValueError("invalid lexical rank")
                    if boundary is not None and score > boundary[0]:
                        complete = True
                        break
                    row = self.conn.execute("SELECT * FROM representations WHERE id=?", (hit["rowid"],)).fetchone()
                    if row is None:
                        raise ValueError("matching orphan")
                    record = _record(row)
                    eligible = self.qualify(record)
                    for cache, maximum in ((self.metadata, MAX_REPRESENTATIONS * 4),
                                           (self.qualifiers, MAX_REPRESENTATIONS)):
                        while len(cache) > maximum:
                            del cache[next(iter(cache))]
                    if eligible is None:
                        continue
                    source_id = _canonical(record["source_id"], self.canonical)
                    new_source = source_id not in candidates
                    if new_source:
                        if boundary is not None:
                            if (score, source_id) >= boundary:
                                continue
                            del candidates[boundary[1]]
                        candidates[source_id] = []
                    items = candidates[source_id]
                    if not any(rep_id == record["representation_id"] for _, rep_id in items):
                        insort(items, (score, record["representation_id"]))
                        if len(items) > limit:
                            items.pop()
                    if new_source and len(candidates) == limit:
                        boundary = max((items[0][0], sid) for sid, items in candidates.items())
                _check_deadline(self.deadline)
            finally:
                cursor.close()
            if complete or fetched < window:
                break
            window *= 2
        ordered = sorted(candidates, key=lambda sid: (candidates[sid][0][0], sid))
        rows = []
        for depth in range(limit):
            for source_id in ordered:
                _check_deadline(self.deadline)
                items = candidates[source_id]
                if depth < len(items):
                    score, rep_id = items[depth]
                    rows.append({"representation_id": rep_id, "score": -score, "generation": self.generation})
                    if len(rows) == limit:
                        return rows
        return rows

    def owned_payload(self, payload):
        """Exact native fulltext lookup, never paragraph-to-sentence vector reuse.

        An explicit None revision is legacy_unmarked, not a publication certificate.
        A missing revision is unverified, distinct from legacy. Lookup is indexed.
        None means owner AND canonical ID are outside this frozen generation;
        [] means no exact whole-text/owner/revision match; independent alias
        revisions require caller all-ID visibility, never a publication certificate.
        _NATIVE_INELIGIBLE means exact matches were excluded by frozen filters.
        """
        if not isinstance(payload, dict) or "publication_revision" not in payload:
            raise ValueError("native publication revision missing")
        source_id = _identifier(payload["arxiv_id"])
        canonical = _canonical(source_id, self.canonical)
        text = _text(payload["text"])
        revision = payload["publication_revision"]
        if revision is not None:
            _sha(revision)
        physical = self.source(source_id)
        if physical is None and self.source(canonical) is None:
            return None
        if self._independent_alias_revisions and (physical is None or self.source(canonical) is None):
            return []
        digest = hashlib.sha256(text.encode()).hexdigest()
        ids = ((source_id,) if self._independent_alias_revisions
               else tuple(dict.fromkeys((source_id, canonical))))
        _check_deadline(self.deadline)
        rows = self.conn.execute(
            "SELECT * FROM representations WHERE source_id IN (" + ",".join("?" for _ in ids)
            + ") AND indexed_text_sha256=? AND kind='own_passage' AND origin_id=source_id "
              "ORDER BY representation_id LIMIT ?", (*ids, digest, MAX_REPRESENTATIONS + 1)).fetchall()
        if len(rows) > MAX_REPRESENTATIONS:
            raise _NativeMappingLimit("native exact mapping bound exceeded")
        matches = []
        exact = False
        for row in rows:
            _check_deadline(self.deadline)
            record = _record(row)
            owner = self.source(record["origin_id"])
            target = self.source(canonical)
            if not self._independent_alias_revisions:
                target = target or owner
            if owner is None or target is None:
                raise ValueError("native missing publication owner")
            revision_match = (record["source_id"] == source_id and record["origin_id"] == source_id
                              if self._independent_alias_revisions else target["publication_revision"] == revision)
            if (record["text"] == text and owner["publication_revision"] == revision
                    and revision_match):
                exact = True
                if self.qualify(record) is not None:
                    matches.append(record["representation_id"])
        return matches if matches or not exact else _NATIVE_INELIGIBLE


def hydrate_representations(path, representation_ids, *, generation, timeout=0.25,
                            canonical_id=None, **filters):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("invalid timeout")
    with CatalogReader(path, generation, deadline=time.monotonic() + timeout,
                       canonical_id=canonical_id, **filters) as reader:
        return reader.hydrate(representation_ids)


def aggregate_source_ranks(rank_lists, *, canonical_id=None, limit=MAX_SOURCES):
    return _aggregate_rank_lists(rank_lists, canonicalize=canonical_id or (lambda value: value),
                                 source_limit=limit, _maximum=50)


def _metadata(conn, source_id, cache):
    if source_id not in cache:
        row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            cache[source_id] = None
        else:
            record = dict(row)
            record["layers"] = json.loads(record["layers"])
            _source(record, lambda value: value)
            record["publication_state"] = ("legacy_unmarked" if record["publication_revision"] is None
                                           else "published_generation_snapshot")
            cache[source_id] = record
    return cache[source_id]


def _record(row):
    record = {key: row[key] for key in _REP_KEYS}
    record["locator"] = json.loads(record["locator"])
    record["binding"] = json.loads(record["binding"]) if record["binding"] is not None else None
    for field in ("section_type", "element_type"):
        if row[field] is not None:
            record[field] = row[field]
    record["terms"] = json.loads(row["terms"])
    _representation(record, lambda value: value)
    if "indexed_text_sha256" in row.keys() and row["indexed_text_sha256"] != hashlib.sha256(record["text"].encode()).hexdigest():
        raise ValueError("indexed text hash mismatch")
    return record


def _owned(conn, source_ids, section_type, element_type, terms, maximum):
    placeholders = ",".join("?" for _ in source_ids)
    sql = ("SELECT q.* FROM representations q JOIN sources s ON s.source_id=q.origin_id "
           f"WHERE q.source_id IN ({placeholders}) AND q.origin_id=q.source_id "
           "AND q.kind IN ('own_summary','own_passage') AND s.status='done'")
    params = list(source_ids)
    for field, value in (("section_type", section_type), ("element_type", element_type)):
        if value is not None:
            sql += f" AND q.kind='own_passage' AND q.{field}=?"
            params.append(value)
    if terms:
        sql += (" AND EXISTS (SELECT 1 FROM representation_terms rt "
                "WHERE rt.representation_id=q.id AND rt.term IN ("
                + ",".join("?" for _ in terms) + "))")
        params.extend(terms)
    sql += " ORDER BY CASE q.kind WHEN 'own_summary' THEN 0 ELSE 1 END,q.representation_id LIMIT ?"
    return [_record(row) for row in conn.execute(sql, [*params, maximum])]


def _candidate_sql(layer, year_from, year_to, section_type, element_type, terms, strict):
    filters = ["t.status='done'", "o.status='done'",
               "(ct.source_id IS NULL OR ct.status='done')",
               "(co.source_id IS NULL OR co.status='done')"]
    params = []
    for value, operator in ((year_from, ">="), (year_to, "<=")):
        if value is not None:
            filters.append("(CASE WHEN ct.source_id IS NULL THEN t.year ELSE ct.year END)" + operator + "?")
            params.append(value)
    if layer is not None:
        filters.append("EXISTS (SELECT 1 FROM source_layers l WHERE "
                       "l.source_id=coalesce(ct.source_id,t.source_id) AND l.layer=?)")
        params.append(layer)
    if strict:
        qualifier = ("EXISTS (SELECT 1 FROM representations q WHERE "
                     "q.source_id IN (v.canonical_source,v.source_id) AND q.origin_id=q.source_id "
                     "AND q.kind IN ('own_summary','own_passage') AND q.binding_status='resolved'")
        for field, value in (("section_type", section_type), ("element_type", element_type)):
            if value is not None:
                qualifier += f" AND q.kind='own_passage' AND q.{field}=?"
                params.append(value)
        if terms:
            qualifier += (" AND EXISTS (SELECT 1 FROM representation_terms rt "
                          "WHERE rt.representation_id=q.id AND rt.term IN ("
                          + ",".join("?" for _ in terms) + "))")
            params.extend(terms)
        filters.append(qualifier + ")")
    sql = ("WITH matched AS MATERIALIZED ("
           "SELECT rowid AS rid,bm25(representation_fts) AS bm25_score "
           "FROM representation_fts WHERE representation_fts MATCH ?), "
           "identified AS MATERIALIZED (SELECT r.id,r.representation_id,r.source_id,r.origin_id,"
           "m.bm25_score,catalog_canonical(r.source_id) AS canonical_source,"
           "catalog_canonical(r.origin_id) AS canonical_owner "
           "FROM matched m JOIN representations r ON r.id=m.rid "
           "WHERE r.kind=? AND r.binding_status='resolved'), "
           "eligible AS MATERIALIZED (SELECT v.* FROM identified v "
           "JOIN sources t ON t.source_id=v.source_id JOIN sources o ON o.source_id=v.origin_id "
           "LEFT JOIN sources ct ON ct.source_id=v.canonical_source "
           "LEFT JOIN sources co ON co.source_id=v.canonical_owner WHERE " + " AND ".join(filters)
           + "), ranked AS (SELECT e.*,row_number() OVER (PARTITION BY canonical_source "
           "ORDER BY bm25_score,representation_id) AS representation_rank FROM eligible e), "
           "heads AS (SELECT canonical_source,row_number() OVER (ORDER BY bm25_score,canonical_source) "
           "AS source_rank FROM ranked WHERE representation_rank=1), "
           "selected AS (SELECT * FROM heads WHERE source_rank<=32) "
           "SELECT r.*,v.bm25_score FROM selected h JOIN ranked v ON v.canonical_source=h.canonical_source "
           "JOIN representations r ON r.id=v.id ORDER BY v.representation_rank,h.source_rank LIMIT ?")
    return sql, params


def search_catalog(path, query, *, layer=None, year_from=None, year_to=None,
                   limit=20, timeout=0.25, canonical_id=None,
                   section_type=None, element_type=None, terms=None):
    """Return ready/partial discovery, or explicit unavailable/invalid catalog.

    timeout is seconds; one absolute monotonic deadline covers SQL and lookups.
    Year/layer use target metadata, never the owner of a foreign snippet.
    Section/element require an owned passage; terms require explicit owned tags
    matching ANY requested tag by casefolded equality, never words inferred from
    foreign text. Foreign hits need an existing qualifying own representation.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite nonnegative seconds")
    deadline = time.monotonic() + timeout
    limit = _cap(limit, MAX_SOURCES)
    canonical_id = canonical_id or (lambda value: value)
    if not callable(canonical_id):
        raise ValueError("canonical_id must be callable")
    match = _safe_fts_query(query)
    terms = _terms(terms)
    for field, value in (("layer", layer), ("section_type", section_type), ("element_type", element_type)):
        if value is not None:
            _string(value, field)
    for value in (year_from, year_to):
        if value is not None and (type(value) is not int or not 1 <= value <= 9999):
            raise ValueError("invalid year filter")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("inverted year range")
    strict = section_type is not None or element_type is not None or bool(terms)
    return _search_catalog(Path(path).absolute(), match, deadline, canonical_id, limit,
                           layer, year_from, year_to, section_type, element_type, terms, strict)


def _search_catalog(path, match, deadline, canonical_id, limit, layer, year_from, year_to,
                    section_type, element_type, terms, strict):
    generation, conn, ready = None, None, False
    reasons, lexical_scores, slots, metadata, seen_reps, owned_cache = set(), {}, {}, {}, set(), {}
    read_count = 0
    result_status = "unavailable"

    def attach(record, target, bm25_score=None):
        rep_id = record["representation_id"]
        if rep_id in seen_reps:
            return True
        if len(seen_reps) >= MAX_REPRESENTATIONS:
            reasons.add("representation_limit")
            return False
        seen_reps.add(rep_id)
        source_id = target["source_id"]
        slot = slots.setdefault(source_id, {**target, "evidence_state": "not_read",
                                            "representations": [], "verification_required": True})
        origin = _metadata(conn, record["origin_id"], metadata)
        if origin is None or origin["status"] != "done":
            raise ValueError("missing or hidden origin")
        rep = {key: value for key, value in record.items() if key not in ("source_id", "origin_id")}
        rep.update({"target_source_id": source_id, "origin_source_id": record["origin_id"],
                    "indexed_source_id": record["source_id"], "origin_title": origin["title"],
                    "origin_publication_revision": origin["publication_revision"],
                    "origin_publication_state": origin["publication_state"], "evidence_state": "not_read"})
        if bm25_score is not None:
            rep["bm25_score"] = bm25_score
        slot["representations"].append(rep)
        return True

    if not path.is_file():
        reasons.add("missing_catalog")
    else:
        try:
            _check_deadline(deadline)
            conn = _connect(path)
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            conn.create_function("catalog_canonical", 1, lambda value: _canonical(value, canonical_id), deterministic=True)
            conn.execute("PRAGMA query_only=ON")
            if (conn.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
                    or conn.execute("PRAGMA user_version").fetchone()[0] != _VERSION):
                raise ValueError("invalid catalog header")
            manifest = conn.execute("SELECT singleton,version,generation FROM catalog").fetchall()
            if len(manifest) != 1 or tuple(manifest[0])[:2] != (1, _VERSION):
                raise ValueError("invalid manifest")
            generation = _sha(manifest[0]["generation"])
            sql, filter_params = _candidate_sql(layer, year_from, year_to, section_type, element_type, terms, strict)
            conn.execute(sql, ["\"catalog\"", KINDS[0], *filter_params, 0])
            conn.execute("SELECT layer FROM source_layers LIMIT 0")
            conn.execute("SELECT term FROM representation_terms LIMIT 0")
            ready, result_status = True, "ready"
            if match:
                orphan = conn.execute(
                    "SELECT 1 FROM representation_fts "
                    "LEFT JOIN representations r ON r.id=representation_fts.rowid "
                    "LEFT JOIN sources t ON t.source_id=r.source_id "
                    "LEFT JOIN sources o ON o.source_id=r.origin_id "
                    "WHERE representation_fts MATCH ? AND "
                    "(r.id IS NULL OR t.source_id IS NULL OR o.source_id IS NULL) LIMIT 1", (match,)).fetchone()
                if orphan:
                    reasons.add("matching_orphan")
                    raise ValueError("matching representation has no target or owner")
                for kind in KINDS:
                    _check_deadline(deadline)
                    read_cap = min(32, (MAX_REPRESENTATIONS - read_count) // (2 if strict else 1))
                    if read_cap <= 0:
                        reasons.add("representation_limit")
                        break
                    count = 0
                    for row in conn.execute(sql, [match, kind, *filter_params, read_cap]):
                        _check_deadline(deadline)
                        if read_count >= MAX_REPRESENTATIONS:
                            reasons.add("representation_limit")
                            break
                        read_count += 1
                        count += 1
                        score = row["bm25_score"]
                        if type(score) not in (int, float) or not math.isfinite(score):
                            reasons.add("invalid_bm25_score")
                            continue
                        record = _record(row)
                        source_id = _canonical(record["source_id"], canonical_id)
                        canonical_owner = _canonical(record["origin_id"], canonical_id)
                        owner = _metadata(conn, canonical_owner, metadata)
                        if owner is not None and owner["status"] != "done":
                            continue
                        if (kind == "citation_context" and canonical_owner == source_id
                                and record["binding"].get("self_citation") is not True):
                            reasons.add("invalid_binding")
                            continue
                        if kind == "citation_context" and _canonical(record["binding"]["identifier"], canonical_id) != source_id:
                            reasons.add("invalid_binding")
                            continue
                        target = _metadata(conn, source_id, metadata) or _metadata(conn, record["source_id"], metadata)
                        if target is None or target["status"] != "done":
                            continue
                        if layer is not None and layer not in target["layers"]:
                            continue
                        year = target["year"]
                        if (year_from is not None or year_to is not None) and (year is None
                                or year_from is not None and year < year_from
                                or year_to is not None and year > year_to):
                            continue
                        target = {**target, "source_id": source_id}
                        ids = tuple(dict.fromkeys((source_id, record["source_id"])))
                        if strict:
                            if ids not in owned_cache:
                                if read_count >= MAX_REPRESENTATIONS:
                                    reasons.add("representation_limit")
                                    break
                                owned_cache[ids] = _owned(conn, ids, section_type, element_type, terms, 1)
                                read_count += len(owned_cache[ids])
                            if not owned_cache[ids]:
                                continue
                            if not attach(owned_cache[ids][0], target):
                                continue
                        if attach(record, target, score):
                            lexical_scores[source_id] = min(score, lexical_scores.get(source_id, score))
                    if count == read_cap:
                        reasons.add("representation_limit")
                # Preserve owned context for citation-only discovery, within the SAME global cap.
                ordered = sorted(lexical_scores, key=lambda source_id: (lexical_scores[source_id], source_id))[:limit]
                for source_id in ordered:
                    _check_deadline(deadline)
                    slot = slots[source_id]
                    ids = tuple(dict.fromkeys([source_id] + [r["indexed_source_id"] for r in slot["representations"]]))
                    remaining = MAX_REPRESENTATIONS - read_count
                    if remaining <= 0:
                        reasons.add("representation_limit")
                        break
                    owned = _owned(conn, ids, section_type, element_type, terms, min(2, remaining))
                    read_count += len(owned)
                    for record in owned:
                        _check_deadline(deadline)
                        attach(record, slot)
        except _Deadline:
            reasons.add("deadline")
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                reasons.add("deadline")
            else:
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                result_status = "unavailable" if code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_BUSY) else "invalid"
                reasons.add("catalog_read_failed")
                slots.clear()
        except OSError:
            result_status = "unavailable"
            reasons.add("catalog_unavailable")
            slots.clear()
        except (ValueError, TypeError, KeyError, OverflowError):
            result_status = "invalid"
            reasons.add("invalid_catalog")
            slots.clear()
        finally:
            if conn is not None:
                conn.set_progress_handler(None, 0)
                conn.close()
    if "deadline" in reasons:
        result_status = "ready" if ready else "unavailable"
    if len(slots) > limit:
        reasons.add("source_limit")
    ordered = sorted(lexical_scores, key=lambda source_id: (lexical_scores[source_id], source_id))
    ranks = _aggregate_rank_lists({"lexical": ordered}, canonicalize=lambda value: value, source_limit=limit)
    sources = []
    for rank in ranks:
        if rank["source_id"] not in slots:
            continue
        slot = slots[rank["source_id"]]
        reps, origins = [], set()
        for rep in sorted(slot["representations"], key=lambda r: (
                KINDS.index(r["kind"]), r.get("bm25_score", float("inf")), r["representation_id"])):
            owner = _canonical(rep["origin_source_id"], canonical_id)
            foreign = rep["kind"] == "citation_context"
            if len(reps) >= MAX_CONTEXTS or foreign and owner not in origins and len(origins) >= MAX_ORIGINS:
                reasons.add("context_limit")
                continue
            reps.append(rep)
            if foreign:
                origins.add(owner)
        sources.append({**slot, **rank, "representations": reps})
    return {"sources": sources, "partial": bool(reasons), "status": result_status,
            "generation": generation, "diagnostics": {"reasons": sorted(reasons),
                                                     "collected_representations": len(seen_reps),
                                                     "read_representations": read_count}}
