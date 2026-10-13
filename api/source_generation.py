"""Opt-in discovery on one immutable representation generation, never evidence.

Callbacks are synchronous and must enforce their timeout (remaining seconds).
visible_sources(records, timeout) returns {source_id: current_publication_revision}.
Dense callbacks must use grouped source retrieval and the supplied Qdrant filter
BEFORE their limit. Flat rows or Qdrant {groups: [{hits: [...]}]} are accepted.
query_owned(query, target_filters, limit, timeout) reads native 384 fulltext points;
target_filters contains layer/year_from/year_to/section_type/element_type/terms.
Two physical dense feeds collapse by best canonical source rank, not cosine score.
Workers are globally bounded to two outstanding calls, with no unbounded queue.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading

try:
    from . import source_representations as catalog
except ImportError:
    import source_representations as catalog


MODEL_ID = "Xenova/bge-base-en-v1.5"
DIMENSION = 768
MANIFEST_VERSION = 1
MAX_CANDIDATES = 128
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
MAX_LENGTH = 384
_CHANNEL_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="source-discovery")
_CHANNEL_SLOTS = threading.BoundedSemaphore(2)


def _submit_channel(callback):
    if not _CHANNEL_SLOTS.acquire(blocking=False):
        return None
    def run():
        try:
            return callback()
        finally:
            _CHANNEL_SLOTS.release()
    try:
        return _CHANNEL_POOL.submit(run)
    except BaseException:
        _CHANNEL_SLOTS.release()
        raise


def collection_name(generation):
    return "source_repr_" + catalog._sha(generation)


def load_manifest(path):
    path = Path(path).absolute()
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("manifest too large")
    manifest = json.loads(raw)
    required = {"version", "generation", "catalog_path", "collection", "model_id",
                "dimension", "complete", "counts"}
    if not isinstance(manifest, dict) or not required <= manifest.keys():
        raise ValueError("invalid manifest fields")
    if type(manifest["version"]) is not int or manifest["version"] != MANIFEST_VERSION:
        raise ValueError("unsupported manifest version")
    generation = catalog._sha(manifest["generation"])
    if manifest["collection"] != collection_name(generation):
        raise ValueError("unowned generation collection")
    if manifest["model_id"] != MODEL_ID or type(manifest["dimension"]) is not int or manifest["dimension"] != DIMENSION:
        raise ValueError("unsupported model/dimension")
    if manifest["complete"] is not True:
        raise ValueError("incomplete generation")
    embedding = manifest.get("embedding")
    if not isinstance(embedding, dict):
        raise ValueError("missing embedding identity")
    for field in ("model_sha256", "tokenizer_sha256"):
        catalog._sha(embedding[field])
    if (embedding.get("query_instruction") != QUERY_INSTRUCTION
            or embedding.get("max_length") != MAX_LENGTH
            or embedding.get("pooling") != "CLS/L2"
            or embedding.get("dimension") != DIMENSION):
        raise ValueError("embedding contract mismatch")
    counts = manifest["counts"]
    if not isinstance(counts, dict) or not {"sources", "representations", "points"} <= counts.keys():
        raise ValueError("missing counts")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("invalid counts")
    native = counts.get("native_owned_representations", 0)
    derived = counts.get("new_indexed_representations", counts["representations"])
    if native + derived != counts["representations"]:
        raise ValueError("inconsistent native/derived counts")
    if counts["points"] < derived:
        raise ValueError("incomplete point count")
    catalog._string(manifest["catalog_path"], "catalog_path", 4096)
    catalog_path = Path(manifest["catalog_path"])
    if not catalog_path.is_absolute():
        catalog_path = path.parent / catalog_path
    manifest = {**manifest, "catalog_path": str(catalog_path.resolve(strict=True))}
    coverage = catalog.catalog_coverage(manifest["catalog_path"])
    if not catalog.coverage_is_complete(coverage):
        raise ValueError("catalog coverage is incomplete or uncertified")
    if "coverage" in manifest and manifest["coverage"] != coverage:
        raise ValueError("manifest/catalog coverage mismatch")
    manifest["coverage"] = coverage
    stat = catalog_path.stat()
    identity = [raw.hex(), manifest["catalog_path"], stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    manifest["cache_identity"] = hashlib.sha256(catalog._json(identity).encode()).hexdigest()
    return manifest


def generation_cache_identity(manifest_path):
    """Opaque cache key; None means inactive, never fall back to an old generation."""
    try:
        manifest = load_manifest(manifest_path)
        with catalog.CatalogReader(manifest["catalog_path"], manifest["generation"],
                                  deadline=time.monotonic() + 0.1):
            return manifest["cache_identity"]
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, catalog._Deadline):
        return None


def verify_embedding_descriptor(manifest, health):
    """Validate health of the ACTUAL query runtime, never a CLI/model-name assertion.

    Integration fetches health with its remaining deadline and supplies that proof
    through runtime_descriptor(timeout), or embed_query.embedding_descriptor.
    The latter may be the health dictionary or a timeout-taking health callback.
    """
    if not isinstance(manifest, dict):
        manifest = load_manifest(manifest)
    if (not isinstance(health, dict) or health.get("model") != manifest["model_id"]
            or health.get("embedding") != manifest["embedding"]):
        raise ValueError("query runtime embedding identity unverified/mismatched")
    return True


def dense_filters(generation, *, layer=None, year_from=None, year_to=None,
                  section_type=None, element_type=None, terms=None):
    must = [{"key": "generation", "match": {"value": generation}},
            {"key": "target_status", "match": {"value": "done"}},
            {"key": "owner_status", "match": {"value": "done"}}]
    if layer is not None:
        must.append({"key": "target_layers", "match": {"value": layer}})
    if year_from is not None or year_to is not None:
        bounds = {}
        if year_from is not None:
            bounds["gte"] = year_from
        if year_to is not None:
            bounds["lte"] = year_to
        must.append({"key": "target_year", "range": bounds})
    own = []
    for field, value in (("section_type", section_type), ("element_type", element_type)):
        if value is not None:
            own.append({"key": field, "match": {"value": value}})
    if own:
        own.append({"key": "kind", "match": {"value": "own_passage"}})
    if terms:
        own.append({"key": "terms", "match": {"any": list(terms)}})
    if own:
        must.append({"nested": {"key": "owned_facets", "filter": {"must": own}}})
    return {"must": must}


def _rows(response, generation):
    if isinstance(response, dict):
        response = response.get("result", response)
        groups = response.get("groups")
        if not isinstance(groups, list) or len(groups) > MAX_CANDIDATES:
            raise ValueError("invalid grouped dense response")
        flat = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hits"), list):
                raise ValueError("invalid dense group")
            for hit in group["hits"]:
                if len(flat) >= MAX_CANDIDATES:
                    raise ValueError("unbounded grouped dense candidates")
                flat.append(hit)
        response = flat
    if not isinstance(response, (list, tuple)) or len(response) > MAX_CANDIDATES:
        raise ValueError("unbounded dense candidates")
    rows = []
    for row in response:
        if not isinstance(row, dict):
            raise ValueError("invalid dense row")
        payload = row.get("payload", row)
        if not isinstance(payload, dict):
            raise ValueError("invalid dense payload")
        if payload.get("generation", generation) != generation:
            raise ValueError("dense generation mismatch")
        rep_id = catalog._identifier(payload["representation_id"])
        score = row["score"]
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("invalid dense score")
        rows.append({"representation_id": rep_id, "score": score,
                     **{key: payload[key] for key in ("source_id", "origin_id") if key in payload}})
    return rows


def _native_rows(response):
    if isinstance(response, dict):
        response = response.get("result", response)
        groups = response.get("groups")
        if not isinstance(groups, list) or len(groups) > MAX_CANDIDATES:
            raise ValueError("invalid native groups")
        flat = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hits"), list):
                raise ValueError("invalid native group")
            for hit in group["hits"]:
                if len(flat) >= MAX_CANDIDATES:
                    raise ValueError("native candidate bound exceeded")
                flat.append(hit)
        response = flat
    if not isinstance(response, (list, tuple)) or len(response) > MAX_CANDIDATES:
        raise ValueError("invalid native candidates")
    for row in response:
        if (not isinstance(row, dict) or not isinstance(row.get("payload"), dict)
                or type(row.get("score")) not in (int, float) or not math.isfinite(row["score"])
                or not isinstance(row.get("id"), (str, int))):
            raise ValueError("invalid native point")
    return response


def _native_mappings(reader, response, diagnostics, reasons, limits):
    unique, owners = {}, {}
    for point in sorted(response, key=lambda point: (-point["score"], str(point["id"]))):
        try:
            ids = reader.owned_payload(point["payload"])
        except catalog._NativeMappingLimit:
            reasons.add("native_exact_mapping_limit")
            continue
        except (ValueError, TypeError, KeyError):
            reasons.add("native_payload_invalid")
            continue
        finally:
            for cache, maximum in ((reader.metadata, catalog.MAX_REPRESENTATIONS * 4),
                                   (reader.qualifiers, catalog.MAX_REPRESENTATIONS)):
                while len(cache) > maximum:
                    del cache[next(iter(cache))]
        if ids is None:
            diagnostics["native_outside_generation"] += 1
            continue
        if ids is catalog._NATIVE_INELIGIBLE:
            diagnostics["native_filtered"] += 1
            continue
        if not ids:
            reasons.add("native_unmatched_payload")
            continue
        owner = catalog._canonical(point["payload"]["arxiv_id"], reader.canonical)
        witness = {"point_id": point["id"], "dimension": 384,
                   "text_sha256": hashlib.sha256(point["payload"]["text"].encode()).hexdigest(),
                   "publication_revision": point["payload"]["publication_revision"],
                   "validation": "exact_whole_payload_text_and_owner_revision"}
        for rep_id in ids:
            catalog._check_deadline(reader.deadline)
            row = unique.setdefault(rep_id, {"representation_id": rep_id, "score": point["score"],
                                             "native_provenance": []})
            owners[rep_id] = owner
            row["score"] = max(row["score"], point["score"])
            witnesses = row["native_provenance"]
            if len(witnesses) < 8 and witness not in witnesses:
                witnesses.append(witness)
    groups = {}
    for rep_id, row in unique.items():
        groups.setdefault(owners[rep_id], []).append(row)
    if len(groups) > MAX_CANDIDATES:
        reasons.add("native_mapping_limit")
    elif len(unique) > MAX_CANDIDATES:
        diagnostics["native_alternatives_truncated"] += len(unique) - MAX_CANDIDATES
        limits.add("dense_native_alternative_limit")
    for rows in groups.values():
        rows.sort(key=lambda row: (-row["score"], row["representation_id"]))
    owners = sorted(groups, key=lambda owner: (-groups[owner][0]["score"], owner))
    mapped = []
    for offset in range(MAX_CANDIDATES):
        catalog._check_deadline(reader.deadline)
        for owner in owners:
            if offset < len(groups[owner]):
                mapped.append(groups[owner][offset])
                if len(mapped) == MAX_CANDIDATES:
                    return mapped
        if all(offset + 1 >= len(rows) for rows in groups.values()):
            break
    return mapped


def discover_sources(manifest_path, query, *, embed_query, query_dense, timeout,
                     layer=None, year_from=None, year_to=None, section_type=None,
                     element_type=None, terms=None, canonical_id=None,
                     visible_sources=None, limit=20, runtime_descriptor=None,
                     query_owned=None, hybrid=True):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite nonnegative seconds")
    if not isinstance(query, str) or len(query) > 65536:
        raise ValueError("invalid query")
    if not callable(embed_query) or not callable(query_dense):
        raise ValueError("embedding and dense callbacks required")
    limit = catalog._cap(limit, 50)
    if type(hybrid) is not bool:
        raise ValueError("hybrid must be boolean")
    if canonical_id is not None and not callable(canonical_id):
        raise ValueError("invalid canonical callback")
    if visible_sources is not None and not callable(visible_sources):
        raise ValueError("invalid visibility callback")
    if runtime_descriptor is not None and not callable(runtime_descriptor):
        raise ValueError("invalid runtime descriptor callback")
    if query_owned is not None and not callable(query_owned):
        raise ValueError("invalid native callback")
    for field, value in (("layer", layer), ("section_type", section_type), ("element_type", element_type)):
        if value is not None:
            catalog._string(value, field)
    for value in (year_from, year_to):
        if value is not None and (type(value) is not int or not 1 <= value <= 9999):
            raise ValueError("invalid year")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("inverted year range")
    terms = catalog._terms(terms)
    filters = dict(layer=layer, year_from=year_from, year_to=year_to,
                   section_type=section_type, element_type=element_type, terms=terms)
    deadline = time.monotonic() + timeout
    result = {"status": "unavailable", "generation": None, "sources": [],
              "diagnostics": {"reasons": [], "limits": [], "retrieval_bounded": True, "exhaustive": False,
                              "details_truncated": 0, "native_outside_generation": 0,
                              "native_filtered": 0, "native_alternatives_truncated": 0}, "partial": True}
    reasons, limits = set(), set()

    def remaining():
        catalog._check_deadline(deadline)
        return deadline - time.monotonic()

    try:
        remaining()
        manifest = load_manifest(manifest_path)
        result["generation"] = manifest["generation"]
        result["diagnostics"]["cache_identity"] = manifest["cache_identity"]
        proof = runtime_descriptor or getattr(embed_query, "embedding_descriptor", None)
        try:
            health = proof(remaining()) if callable(proof) else proof
            remaining()
            verify_embedding_descriptor(manifest, health)
        except catalog._Deadline:
            raise
        except Exception:
            result["status"] = "unavailable"
            reasons.add("query_runtime_unverified")
            result["diagnostics"]["reasons"] = sorted(reasons)
            return result
        with catalog.CatalogReader(manifest["catalog_path"], manifest["generation"],
                                  deadline=deadline, canonical_id=canonical_id,
                                  independent_alias_revisions=visible_sources is not None, **filters) as reader:
            result["status"] = "ready"
            channels, slots = {}, {}
            channel_deadline = deadline - min(0.1, remaining() / 4)
            def channel_remaining():
                catalog._check_deadline(channel_deadline)
                return channel_deadline - time.monotonic()
            def run_derived():
                vector = embed_query(query, channel_remaining())
                channel_remaining()
                if (not isinstance(vector, (list, tuple)) or len(vector) != manifest["dimension"]
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
                    raise ValueError("invalid query vector")
                dense = query_dense(vector, manifest["collection"],
                                    dense_filters(manifest["generation"], **filters),
                                    MAX_CANDIDATES, channel_remaining())
                channel_remaining()
                return _rows(dense, manifest["generation"])
            def run_native():
                response = query_owned(query, filters, MAX_CANDIDATES, channel_remaining())
                channel_remaining()
                return _native_rows(response)
            futures = {"dense_derived": _submit_channel(run_derived)}
            if query_owned is not None:
                futures["dense_native"] = _submit_channel(run_native)
            elif manifest["counts"].get("native_owned_representations", 0):
                reasons.add("native_adapter_unavailable")
            responses = {}
            lexical_deadline = channel_deadline
            reader.deadline = lexical_deadline
            try:
                responses["lexical"] = reader.lexical(query) if hybrid else []
            except catalog._Deadline:
                reasons.add("lexical_deadline")
                responses["lexical"] = []
            except sqlite3.Error as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
                    raise
                reasons.add("lexical_deadline")
                responses["lexical"] = []
            finally:
                reader.deadline = deadline
            for channel, future in futures.items():
                label = "dense" if channel == "dense_derived" else "native"
                if future is None:
                    reasons.add(label + "_busy")
                    continue
                try:
                    response = future.result(timeout=max(0, channel_deadline - time.monotonic()))
                    remaining()
                    if len(response) == MAX_CANDIDATES:
                        limits.add(channel + "_candidate_limit")
                    if channel == "dense_derived":
                        responses[channel] = response
                    else:
                        responses[channel] = _native_mappings(reader, response, result["diagnostics"], reasons,
                                                             limits)
                except FutureTimeout:
                    error = future.exception(timeout=0) if future.done() else None
                    reasons.add(label + ("_failed:TimeoutError" if isinstance(error, TimeoutError) else "_deadline"))
                except catalog._Deadline:
                    reasons.add(label + "_deadline")
                except Exception as exc:
                    reasons.add(label + "_failed:" + type(exc).__name__)
            for channel, rows in responses.items():
                remaining()
                if len(rows) == MAX_CANDIDATES:
                    limits.add(channel + "_candidate_limit")
                try:
                    hydrated = reader.hydrate([row["representation_id"] for row in rows])
                    for row in rows:
                        for slot in hydrated.values():
                            for rep in slot["representations"]:
                                if rep["representation_id"] == row["representation_id"]:
                                    if ("source_id" in row and row["source_id"] != rep["indexed_source_id"]
                                            or "origin_id" in row and row["origin_id"] != rep["origin_source_id"]):
                                        raise ValueError("dense owner/target mismatch")
                except (ValueError, TypeError, KeyError):
                    if channel == "lexical":
                        raise
                    reasons.add(channel + "_hydration_invalid")
                    continue
                scores = {row["representation_id"]: row["score"] for row in sorted(rows, key=lambda r: r["score"])}
                ranked = []
                for source_id, slot in hydrated.items():
                    for rep in slot["representations"]:
                        native = [witness for row in rows
                                  if row["representation_id"] == rep["representation_id"]
                                  for witness in row.get("native_provenance", [])]
                        if native:
                            rep["native_provenance"] = native[:8]
                    values = [scores[r["representation_id"]] for r in slot["representations"]
                              if r["representation_id"] in scores]
                    if not values:
                        continue
                    ranked.append((max(values), source_id))
                    if source_id not in slots:
                        slots[source_id] = slot
                    else:
                        indexed = {r["representation_id"]: r for r in slots[source_id]["representations"]}
                        existing = set(indexed)
                        for rep in slot["representations"]:
                            if rep["representation_id"] in indexed and "native_provenance" in rep:
                                indexed[rep["representation_id"]]["native_provenance"] = rep["native_provenance"]
                        slots[source_id]["representations"].extend(
                            r for r in slot["representations"] if r["representation_id"] not in existing)
                channels[channel] = [source_id for _, source_id in sorted(ranked, key=lambda x: (-x[0], x[1]))]
            if visible_sources is not None:
                try:
                    records = [value for value in reader.metadata.values() if value is not None]
                    if len(records) > 1024:
                        raise ValueError("visibility lookup bound")
                    live = visible_sources(records, remaining())
                    remaining()
                    if not isinstance(live, dict):
                        raise ValueError("visibility must return revision map")
                    def visible(source_id):
                        item = reader.metadata.get(source_id)
                        return (item is not None and source_id in live
                                and live[source_id] == item["publication_revision"])
                    for source_id in list(slots):
                        slot = slots[source_id]
                        previous_count = len(slot["representations"])
                        reps = [r for r in slot["representations"]
                                if visible(r["indexed_source_id"]) and visible(r["origin_source_id"])
                                and visible(catalog._canonical(r["origin_source_id"], reader.canonical))]
                        strict = section_type is not None or element_type is not None or bool(terms)
                        if (not visible(source_id) or not reps or strict and not any(
                                r["kind"] != "citation_context" and
                                (section_type is None or r.get("section_type") == section_type) and
                                (element_type is None or r.get("element_type") == element_type) and
                                (not terms or set(terms).intersection(r["terms"])) for r in reps)):
                            del slots[source_id]
                        else:
                            slot["representations"] = reps
                        if len(reps) != previous_count or source_id not in slots:
                            reasons.add("publication_not_visible")
                except catalog._Deadline:
                    raise
                except Exception as exc:
                    slots.clear()
                    reasons.add("visibility_failed:" + type(exc).__name__)
            # A stale foreign hit cannot retain a vote through another live rep.
            for channel in list(channels):
                row_scores = {row["representation_id"]: row["score"] for row in
                              sorted(responses[channel], key=lambda row: row["score"])}
                ranked = []
                for source_id, slot in slots.items():
                    if source_id not in channels[channel]:
                        continue
                    values = [row_scores[r["representation_id"]] for r in slot["representations"]
                              if r["representation_id"] in row_scores]
                    if values:
                        ranked.append((max(values), source_id))
                channels[channel] = [sid for _, sid in sorted(ranked, key=lambda row: (-row[0], row[1]))]
            # Model cosine scores are not cross-calibrated. Best physical rank
            # wins; appearing in both feeds gives no extra dense vote/bonus.
            physical = {}
            for channel in ("dense_derived", "dense_native"):
                for rank, source_id in enumerate(channels.get(channel, []), 1):
                    physical.setdefault(source_id, {})[channel] = rank
            dense_order = sorted(physical, key=lambda sid: (min(physical[sid].values()), sid))
            channels = {"lexical": channels.get("lexical", []), "dense": dense_order}
            for source_id, ranks in physical.items():
                slots[source_id]["dense_physical_ranks"] = ranks
            ranks = catalog.aggregate_source_ranks(channels, limit=limit)
            for rank in ranks:
                slot = slots[rank["source_id"]]
                reps, origins = [], set()
                def priority(rep):
                    qualifying = (rep["kind"] != "citation_context"
                        and (section_type is None or rep["kind"] == "own_passage" and rep.get("section_type") == section_type)
                        and (element_type is None or rep["kind"] == "own_passage" and rep.get("element_type") == element_type)
                        and (not terms or set(terms).intersection(rep["terms"])))
                    return (not qualifying, catalog.KINDS.index(rep["kind"]), rep["representation_id"])
                for rep in sorted(slot["representations"], key=priority):
                    owner = catalog._canonical(rep["origin_source_id"], reader.canonical)
                    if len(reps) >= catalog.MAX_CONTEXTS or (rep["kind"] == "citation_context"
                            and owner not in origins and len(origins) >= catalog.MAX_ORIGINS):
                        result["diagnostics"]["details_truncated"] += 1
                        continue
                    reps.append(rep)
                    if rep["kind"] == "citation_context":
                        origins.add(owner)
                result["sources"].append({**slot, **rank, "representations": reps})
            remaining()
            if load_manifest(manifest_path)["cache_identity"] != manifest["cache_identity"]:
                raise ValueError("manifest changed during discovery")
            remaining()
    except catalog._Deadline:
        reasons.add("deadline")
        result["sources"] = []
    except OSError:
        result["status"] = "unavailable"
        result["sources"] = []
        reasons.add("generation_unavailable")
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            reasons.add("deadline")
        else:
            result["status"] = "invalid"
            reasons.add("generation_invalid")
        result["sources"] = []
    except (ValueError, TypeError, KeyError):
        result["status"] = "invalid"
        result["sources"] = []
        reasons.add("generation_invalid")
    result["partial"] = bool(reasons)
    result["diagnostics"]["reasons"] = sorted(reasons)
    result["diagnostics"]["limits"] = sorted(limits)
    return result
