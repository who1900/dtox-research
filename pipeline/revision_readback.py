"""Bounded read-only Qdrant readback; external lock/durability admission is mandatory."""
import hashlib
import ipaddress
import json
import math
import re
import struct
from urllib.parse import urlsplit, quote

import requests

from api.source_attribution import stored_source_url, valid_source_url

from .publication import (PublicationBlocked, _revision_bytes, _revision_digest,
                          _revision_vectors, _revision_coarse_input, _revision_payload_snapshot,
                          _revision_payload_bytes, REVISION_API_SOURCE_SHA256)


def _need(condition, code):
    if not condition:
        raise PublicationBlocked(code, code)


def _sha(value):
    return hashlib.sha256(_revision_bytes(value)).hexdigest()


def vector_sha256(values, dimension):
    """qdrant-float32-json-v1: finite binary32 values serialized as canonical JSON."""
    _need(type(dimension) is int and dimension > 0 and isinstance(values, list) and len(values) == dimension, "vector_dimension")
    normalized = []
    for value in values:
        _need(type(value) in (int, float), "vector_not_numeric")
        try:
            value = struct.unpack("!f", struct.pack("!f", value))[0]
        except (OverflowError, struct.error):
            raise PublicationBlocked("vector_not_finite", "float32 overflow") from None
        _need(math.isfinite(value), "vector_not_finite")
        normalized.append(value)
    return _sha(normalized)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "duplicate_json_key")
        result[key] = value
    return result


class RevisionReadback:
    def __init__(self, *, endpoint, profiles, admission, admission_sha256, interval,
                 interval_sha256, interval_guard, parents, limits, session_factory=requests.Session,
                 payload_admission=None, payload_admission_sha256=None):
        parsed = urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            port = parsed.port
        except ValueError:
            local, port = False, None
        _need(local and parsed.scheme in ("http", "https") and port is not None and 0 < port < 65536
              and parsed.username is None and parsed.password is None and not parsed.query
              and not parsed.fragment and parsed.path in ("", "/"), "endpoint_not_closed_loopback")
        self.endpoint = endpoint.rstrip("/")
        self.profiles = json.loads(_revision_bytes(profiles))
        self.admission = json.loads(_revision_bytes(admission))
        self.interval = json.loads(_revision_bytes(interval))
        self.parents = json.loads(_revision_bytes(parents))
        self.limits = dict(limits)
        _need(set(limits) == {"batch_size", "max_response_bytes", "max_total_bytes", "max_requests", "timeout", "max_dimension"}
              and all(type(limits[k]) is int and limits[k] > 0 for k in limits if k != "timeout")
              and type(limits["timeout"]) in (int, float) and math.isfinite(limits["timeout"])
              and limits["timeout"] > 0 and limits["batch_size"] <= 128, "invalid_limits")
        _need(_revision_digest(admission_sha256) and _sha(self.admission) == admission_sha256
              and _revision_digest(interval_sha256) and _sha(self.interval) == interval_sha256, "admission_pin")
        _need(set(self.profiles) == {"fulltext", "coarse"}, "profile_scope")
        for profile in self.profiles.values():
            _need(isinstance(profile, dict) and set(profile) == {"collection", "model_id", "model_sha256", "dimension", "recipe_sha256"}
                  and isinstance(profile["collection"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile["collection"])
                  and isinstance(profile["model_id"], str) and bool(profile["model_id"])
                  and _revision_digest(profile["model_sha256"]) and _revision_digest(profile["recipe_sha256"])
                  and type(profile["dimension"]) is int and 0 < profile["dimension"] <= limits["max_dimension"], "profile_pin")
        admission_keys = {"schema", "endpoint", "profiles", "collision_scope", "registry_sha256", "registry_count", "coverage"}
        _need(set(self.admission) == admission_keys and self.admission["schema"] == "dtox-vector-ownership-admission-1"
              and self.admission["endpoint"] == self.endpoint and self.admission["profiles"] == self.profiles
              and _revision_digest(self.admission["registry_sha256"])
              and self.admission["registry_sha256"] != "0" * 64
              and type(self.admission["registry_count"]) is int and self.admission["registry_count"] > 0
              and self.admission["coverage"] == "complete_uuid_ownership", "ownership_not_admitted")
        scope = self.admission["collision_scope"]
        _need(isinstance(scope, list) and bool(scope) and all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", c) for c in scope)
              and len(scope) == len(set(scope)) and set(p["collection"] for p in self.profiles.values()).issubset(scope)
              and len({p["collection"] for p in self.profiles.values()}) == 2, "collision_scope")
        self.admission_sha256, self.interval_sha256 = admission_sha256, interval_sha256
        self.interval_guard, self.session_factory = interval_guard, session_factory
        _need(callable(interval_guard), "interval_guard_missing")
        self.readback_evidence = None
        self.payload_admission = json.loads(_revision_bytes(payload_admission))
        self.payload_admission_sha256 = payload_admission_sha256

    def _payload_authority(self, artifact, revision):
        admission = self.payload_admission
        snapshot = _revision_payload_snapshot(artifact)
        _need(isinstance(admission, dict) and set(admission) == {"schema", "api_source_sha256", "revisions"}
              and admission["schema"] == "dtox-api-payload-admission-1"
              and admission["api_source_sha256"] == REVISION_API_SOURCE_SHA256
              and _revision_digest(self.payload_admission_sha256)
              and _sha(admission) == self.payload_admission_sha256
              and isinstance(admission["revisions"], dict) and bool(admission["revisions"])
              and admission["revisions"].get(revision) == _sha(snapshot), "api_payload_not_admitted")
        return snapshot

    def assert_interval(self, job, artifact):
        receipt = self.interval
        keys = {"schema", "endpoint", "admission_sha256", "paper_id", "revision", "collision_scope",
                "writer_id", "lock_id", "durability_evidence_sha256", "covers_until", "vectors_durable"}
        _need(_sha(receipt) == self.interval_sha256 and set(receipt) == keys
              and receipt["schema"] == "dtox-vector-writer-interval-1" and receipt["endpoint"] == self.endpoint
              and receipt["admission_sha256"] == self.admission_sha256
              and receipt["paper_id"] == job["paper_id"] and receipt["revision"] == job["revision"]
              and receipt["collision_scope"] == self.admission["collision_scope"]
              and all(isinstance(receipt[k], str) and bool(receipt[k]) for k in ("writer_id", "lock_id"))
              and _revision_digest(receipt["durability_evidence_sha256"])
              and receipt["covers_until"] == "sqlite_commit_return" and receipt["vectors_durable"] is True
              and artifact["artifact_bindings"]["vector_ownership"] == self.admission_sha256,
              "writer_interval_not_bound")
        _need(self.interval_guard(self.interval_sha256) == self.interval_sha256, "writer_interval_lost")

    def _http(self, session, method, path, body=None):
        self.requests += 1
        _need(self.requests <= self.limits["max_requests"], "request_budget")
        response = None
        try:
            response = session.request(method, self.endpoint + path, json=body,
                params={"consistency": "all"} if method == "POST" else None,
                timeout=self.limits["timeout"], allow_redirects=False, stream=True)
            _need(response.status_code == 200, "http_status_" + str(response.status_code))
            raw = bytearray()
            for part in response.iter_content(chunk_size=65536):
                self.total_bytes += len(part)
                _need(len(raw) + len(part) <= self.limits["max_response_bytes"]
                      and self.total_bytes <= self.limits["max_total_bytes"], "response_budget")
                raw.extend(part)
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            _need(isinstance(value, dict) and value.get("status") == "ok" and "result" in value, "qdrant_response")
            self.response_hashes.append(hashlib.sha256(raw).hexdigest())
            return value["result"]
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            raise PublicationBlocked("readback_transport", type(exc).__name__) from None
        finally:
            if response is not None:
                response.close()

    def _schema(self, session, collection, profile):
        result = self._http(session, "GET", "/collections/" + quote(collection, safe=""))
        try:
            params = result["config"]["params"]
            vectors = params["vectors"]
            _need(isinstance(vectors, dict) and type(vectors.get("size")) is int
                  and vectors["size"] == profile["dimension"] and vectors.get("distance") == "Cosine"
                  and vectors.get("datatype") in (None, "float32") and not vectors.get("multivector_config")
                  and not params.get("sparse_vectors"), "collection_vector_schema")
        except (TypeError, KeyError):
            raise PublicationBlocked("collection_vector_schema", "unnamed dense float32 Cosine only") from None

    def _retrieve(self, session, collection, ids):
        result = {}
        for start in range(0, len(ids), self.limits["batch_size"]):
            batch = ids[start:start + self.limits["batch_size"]]
            rows = self._http(session, "POST", "/collections/" + quote(collection, safe="") + "/points",
                              {"ids": batch, "with_vector": True, "with_payload": True})
            _need(isinstance(rows, list), "retrieve_shape")
            for row in rows:
                _need(isinstance(row, dict) and isinstance(row.get("id"), str)
                      and row["id"] in batch and row["id"] not in result, "retrieve_extra_duplicate")
                result[row["id"]] = row
        return result

    def _point(self, row, family_name, profile, point, artifact, revision):
        payload = row.get("payload")
        meta = artifact["projection"]["metadata"]
        _need(isinstance(payload, dict), "payload_missing")
        snapshot = self._payload_authority(artifact, revision)
        expected_payload = snapshot["families"][family_name][point["point_id"]]
        payload_sha = hashlib.sha256(_revision_payload_bytes(payload)).hexdigest()
        _need(payload_sha == expected_payload["sha256"]
              and _revision_payload_bytes(payload) == _revision_payload_bytes(expected_payload["payload"])
              and payload.get("api_payload_sha256") == payload_sha, "api_payload_snapshot_mismatch")
        required = {"arxiv_id": artifact["paper_id"], "publication_revision": revision, "revision_family": family_name,
                    "revision_model_id": profile["model_id"], "revision_model_sha256": profile["model_sha256"],
                    "revision_recipe_sha256": profile["recipe_sha256"], "revision_metadata": meta,
                    "revision_reuse_from": point["reuse_from"], "title": meta["title"], "year": meta["year"],
                    "layers": meta["layers"].split(",") if meta["layers"] else []}
        for key, expected in required.items():
            _need(key in payload and _revision_bytes(payload[key]) == _revision_bytes(expected), "payload_binding_" + key)
        source_url = meta["source_url"]
        _need(source_url is not None and valid_source_url(source_url) == source_url, "source_url_unsupported")
        _need("source_url" in payload and payload["source_url"] == source_url
              and stored_source_url(payload) == source_url, "payload_binding_source_url")
        text = payload.get("revision_input_text")
        _need(isinstance(text, str), "input_text_missing")
        input_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _need(input_sha == point["input_text_sha256"], "input_text_hash")
        if family_name == "fulltext":
            _need(payload.get("text") == text, "api_text_binding")
            chunk = next((c for c in artifact["projection"]["chunks"] if c["point_id"] == point["point_id"]), None)
            _need(chunk is not None, "projection_chunk_missing")
            for key in ("section_type", "element_type"):
                _need(key in payload and _revision_bytes(payload[key]) == _revision_bytes(chunk[key]), "payload_binding_" + key)
        else:
            _need(text == _revision_coarse_input(artifact["projection"], artifact["coarse_recipe"], artifact["artifact_bindings"]), "coarse_input_binding")
        digest = vector_sha256(row.get("vector"), profile["dimension"])
        _need(digest == point["vector_sha256"], "vector_hash_float32")
        return {"point_id": row["id"], "input_text_sha256": input_sha, "vector_sha256": digest,
                "reuse_from": payload["revision_reuse_from"], "owner_paper_id": payload["arxiv_id"],
                "owner_revision": payload["publication_revision"], "metadata_sha256": _sha(payload["revision_metadata"]),
                "api_payload_sha256": payload_sha}

    def __call__(self, job, artifact):
        self.readback_evidence = None
        _need(artifact["paper_id"] == job["paper_id"] and _sha(artifact) == job["revision"], "artifact_binding")
        families = _revision_vectors(artifact)
        self._payload_authority(artifact, job["revision"])
        for name, family in families.items():
            _need({k: v for k, v in family.items() if k != "points"} == self.profiles[name], "external_profile_pin")
        self.assert_interval(job, artifact)
        ids = {p["point_id"] for family in families.values() for p in family["points"]}
        target_ids = set(ids)
        owners = {p["point_id"]: family["collection"] for family in families.values() for p in family["points"]}
        parents = {}
        for name, family in families.items():
            for point in family["points"]:
                ids.add(point["point_id"])
                owners[point["point_id"]] = family["collection"]
                reuse = point["reuse_from"]
                if reuse is not None:
                    parent_revision = reuse["revision"]
                    parent = self.parents.get(parent_revision)
                    _need(isinstance(parent, dict) and _sha(parent) == parent_revision
                          and parent_revision == artifact["parent_revision"]
                          and parent["paper_id"] == job["paper_id"] and parent_revision != job["revision"], "parent_artifact_binding")
                    parent_families = _revision_vectors(parent)
                    self._payload_authority(parent, parent_revision)
                    old_family = parent_families[name]
                    _need({k: v for k, v in old_family.items() if k != "points"} == self.profiles[name]
                          and reuse["collection"] == family["collection"], "parent_profile")
                    old = next((p for p in old_family["points"] if p["point_id"] == reuse["point_id"]), None)
                    _need(old is not None and old["input_text_sha256"] == point["input_text_sha256"]
                          and old["vector_sha256"] == point["vector_sha256"] and old["point_id"] not in target_ids
                          and owners.get(old["point_id"], old_family["collection"]) == old_family["collection"], "parent_lineage")
                    ids.add(old["point_id"])
                    owners[old["point_id"]] = old_family["collection"]
                    parents[parent_revision] = parent
        _need(set(parents) == set(self.parents), "parent_scope")
        self.requests, self.total_bytes, self.response_hashes = 0, 0, []
        session = self.session_factory()
        session.trust_env, session.auth, session.proxies = False, None, {}
        session.headers = {"Accept": "application/json"}
        session.cookies.clear()
        try:
            inventory = self._http(session, "GET", "/collections")
            _need(isinstance(inventory, dict) and isinstance(inventory.get("collections"), list)
                  and all(isinstance(r, dict) and isinstance(r.get("name"), str) for r in inventory["collections"]), "collection_inventory_shape")
            names = [r["name"] for r in inventory["collections"]]
            _need(len(names) == len(set(names)) and set(names) == set(self.admission["collision_scope"]), "collection_inventory_drift")
            for name, profile in self.profiles.items():
                self._schema(session, profile["collection"], profile)
            found = {}
            for collection in self.admission["collision_scope"]:
                found[collection] = self._retrieve(session, collection, sorted(ids))
                _need(all(owners[point_id] == collection for point_id in found[collection]), "cross_collection_collision")
            observed = {}
            for name, family in families.items():
                profile = self.profiles[name]
                points = []
                for point in family["points"]:
                    row = found[profile["collection"]].get(point["point_id"])
                    _need(row is not None, "target_missing")
                    result = self._point(row, name, profile, point, artifact, job["revision"])
                    reuse_sha = None
                    if point["reuse_from"] is not None:
                        reuse = point["reuse_from"]
                        parent = parents[reuse["revision"]]
                        old = next(p for p in parent["vector_families"][name]["points"] if p["point_id"] == reuse["point_id"])
                        source = found[profile["collection"]].get(old["point_id"])
                        _need(source is not None, "reuse_source_missing")
                        reuse_sha = self._point(source, name, profile, old, parent, reuse["revision"])["vector_sha256"]
                        _need(reuse_sha == result["vector_sha256"], "reuse_source_hash")
                    result["reuse_readback_sha256"] = reuse_sha
                    points.append(result)
                observed[name] = {**profile, "points": points}
            self.assert_interval(job, artifact)
            self.readback_evidence = {"admission_sha256": self.admission_sha256, "interval_sha256": self.interval_sha256,
                "payload_admission_sha256": self.payload_admission_sha256,
                "profiles_sha256": _sha(self.profiles), "collision_scope": self.admission["collision_scope"],
                "response_sha256": self.response_hashes, "requests": self.requests, "response_bytes": self.total_bytes,
                "vector_normalization": "qdrant-float32-json-v1"}
            return {"paper_id": artifact["paper_id"], "revision": job["revision"], "artifact_sha256": _sha(artifact),
                    "ownership_admission_sha256": self.admission_sha256,
                    "collision_scope": sorted(p["collection"] for p in observed.values()), "vector_families": observed,
                    "durable": self.interval["vectors_durable"], "revision_isolated": True, "verified": True}
        finally:
            session.close()
