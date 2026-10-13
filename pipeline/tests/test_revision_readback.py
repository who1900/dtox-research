"""Fake HTTP only. No socket, model, service, or external Qdrant invocation."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from pipeline import publication as p
from pipeline.revision_readback import RevisionReadback, vector_sha256
from pipeline.tests.test_publication_revision import RevisionTests, vector_plan, writer
from pipeline.tests.revision_fixture import admitted_artifact, stored_payload, payload_admission, stage_artifact


def sha(value):
    return hashlib.sha256(p._revision_bytes(value)).hexdigest()


class Response:
    def __init__(self, value, status=200, raw=None):
        self.status_code, self.closed = status, False
        self.raw = raw if raw is not None else json.dumps(value, ensure_ascii=False).encode("utf-8")

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, case):
        self.case, self.closed = case, False
        self.trust_env, self.proxies, self.auth = True, {"http": "forbidden"}, "forbidden"
        self.cookies = requests.cookies.RequestsCookieJar()
        self.responses, self.calls = [], []

    def request(self, method, url, **kwargs):
        self.case.assertFalse(self.trust_env)
        self.case.assertEqual(self.proxies, {})
        self.case.assertIsNone(self.auth)
        self.case.assertIs(kwargs["allow_redirects"], False)
        self.case.assertIs(kwargs["stream"], True)
        self.case.assertEqual(kwargs["timeout"], 1)
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path == "/collections":
            self.case.assertEqual(method, "GET")
            result = {"collections": [{"name": c} for c in self.case.scope]}
        elif method == "GET":
            collection = path.split("/")[-1]
            profile = next(f for f in self.case.profiles.values() if f["collection"] == collection)
            result = {"config": {"params": {"vectors": {"size": profile["dimension"], "distance": "Cosine"}}}}
        else:
            self.case.assertEqual(method, "POST")
            self.case.assertTrue(path.endswith("/points"))
            self.case.assertEqual(kwargs["params"], {"consistency": "all"})
            self.case.assertIs(kwargs["json"]["with_vector"], True)
            self.case.assertIs(kwargs["json"]["with_payload"], True)
            self.case.assertLessEqual(len(kwargs["json"]["ids"]), 2)
            collection = path.split("/")[2]
            result = [copy.deepcopy(row) for row in self.case.rows[collection].values()
                      if row["id"] in kwargs["json"]["ids"]]
        value = {"status": "ok", "result": result}
        response = self.case.hook(method, path, kwargs, value) if self.case.hook else Response(value)
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "http://127.0.0.1:16335"
        self.values = {"fulltext": [0.1, 0.2, 0.3, 0.4], "coarse": [0.1, -0.2, 0.3]}
        self.projection = RevisionTests("runTest").projection("oldtoken", "readback_parent")
        families, _ = self.plan(self.projection)
        self.profiles = {n: {k: v for k, v in f.items() if k != "points"} for n, f in families.items()}
        self.scope = sorted(f["collection"] for f in self.profiles.values())
        self.admission = {"schema": "dtox-vector-ownership-admission-1", "endpoint": self.endpoint,
            "profiles": self.profiles, "collision_scope": self.scope, "registry_sha256": "a" * 64,
            "registry_count": 100, "coverage": "complete_uuid_ownership"}
        self.parent = self.artifact(self.projection)
        projection = RevisionTests("runTest").projection("oldtoken", "readback_target")
        projection["metadata"]["year"] = 2025
        self.target = self.artifact(projection, self.parent)
        self.hook, self.sessions = None, []
        self.limits = {"batch_size": 2, "max_response_bytes": 50000, "max_total_bytes": 500000,
                       "max_requests": 20, "timeout": 1, "max_dimension": 768}
        self.bind()

    def plan(self, projection, parent=None):
        families, recipe = vector_plan(projection, parent, sha(parent) if parent else None)
        for name, family in families.items():
            family["dimension"] = len(self.values[name])
            for point in family["points"]:
                point["vector_sha256"] = vector_sha256(self.values[name], family["dimension"])
        return families, recipe

    def artifact(self, projection, parent=None):
        families, recipe = self.plan(projection, parent)
        return admitted_artifact("p", projection, families, recipe, {"vector_ownership": sha(self.admission)},
            parent_revision=sha(parent) if parent else None, parent_generation=1 if parent else 0,
            payload_extras=getattr(self, "payload_extras", None))

    def bind(self):
        self.job = {"paper_id": "p", "revision": sha(self.target)}
        self.interval = {"schema": "dtox-vector-writer-interval-1", "endpoint": self.endpoint,
            "admission_sha256": sha(self.admission), **self.job, "collision_scope": self.scope,
            "writer_id": "fixture_writer", "lock_id": "fixture_lock", "durability_evidence_sha256": "b" * 64,
            "covers_until": "sqlite_commit_return", "vectors_durable": True}
        self.rows = {c: {} for c in self.scope}
        for artifact in (self.parent, self.target):
            for name, family in artifact["vector_families"].items():
                for point in family["points"]:
                    payload = stored_payload(artifact, name, point["point_id"])
                    self.rows[family["collection"]][point["point_id"]] = {"id": point["point_id"],
                        "vector": self.values[name][:], "payload": copy.deepcopy(payload)}

    def adapter(self, **changes):
        def factory():
            session = Session(self)
            self.sessions.append(session)
            return session
        authority = payload_admission((self.parent, self.target))
        kwargs = dict(endpoint=self.endpoint, profiles=self.profiles, admission=self.admission,
            admission_sha256=sha(self.admission), interval=self.interval, interval_sha256=sha(self.interval),
            interval_guard=lambda pin: pin, parents={sha(self.parent): self.parent}, limits=self.limits,
            session_factory=factory, payload_admission=authority, payload_admission_sha256=sha(authority))
        kwargs.update(changes)
        return RevisionReadback(**kwargs)

    def invoke(self, adapter=None):
        return (adapter or self.adapter())(self.job, self.target)

    def target_row(self, family="coarse"):
        f = self.target["vector_families"][family]
        return self.rows[f["collection"]][f["points"][0]["point_id"]]

    def test_independent_derivation_and_closed_transport(self):
        adapter = self.adapter()
        with patch.object(p, "revision_readiness_expected", side_effect=AssertionError("must not echo")):
            receipt = self.invoke(adapter)
        self.assertEqual(receipt, p.revision_readiness_expected("p", self.job["revision"], self.target))
        self.assertEqual(adapter.readback_evidence["interval_sha256"], sha(self.interval))
        self.assertGreater(adapter.readback_evidence["requests"], 3)
        self.assertTrue(all(s.closed and all(r.closed for r in s.responses) for s in self.sessions))

    def test_loopback_only_no_url_credentials_paths_or_dns(self):
        for endpoint in ("http://example.org:6333", "http://localhost:6333", "http://127.0.0.1:1/x",
                         "http://user:pass@127.0.0.1:1", "http://127.0.0.1:1?secret=x", "http://127.0.0.1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(p.PublicationBlocked):
                self.adapter(endpoint=endpoint)
        self.assertEqual(self.sessions, [])

    def test_empty_unpinned_or_default_ownership_and_interval_denied(self):
        for key, value in (("registry_count", 0), ("registry_sha256", "0" * 64), ("coverage", "unknown")):
            admission = {**self.admission, key: value}
            with self.subTest(key=key), self.assertRaises(p.PublicationBlocked):
                self.adapter(admission=admission, admission_sha256=sha(admission))
        for changes in ({"admission_sha256": "f" * 64}, {"interval_guard": None}, {"interval_sha256": "f" * 64}):
            with self.subTest(changes=changes), self.assertRaises(p.PublicationBlocked):
                self.adapter(**changes)
        self.interval["revision"] = "f" * 64
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        self.assertEqual(self.sessions, [])

    def test_external_profiles_and_collection_schema(self):
        profiles = copy.deepcopy(self.profiles)
        profiles["coarse"]["model_sha256"] = "f" * 64
        admission = {**self.admission, "profiles": profiles}
        with self.assertRaises(p.PublicationBlocked):
            self.invoke(self.adapter(profiles=profiles, admission=admission, admission_sha256=sha(admission)))
        for vectors in ({"named": {"size": 3, "distance": "Cosine"}}, {"size": 4, "distance": "Cosine"},
                        {"size": 3, "distance": "Dot"}, {"size": 3, "distance": "Cosine", "datatype": "float16"}):
            def hook(method, path, kwargs, value):
                if method == "GET" and path.endswith("fixture_coarse"):
                    value["result"]["config"]["params"]["vectors"] = vectors
                return Response(value)
            self.hook = hook
            with self.subTest(vectors=vectors), self.assertRaises(p.PublicationBlocked):
                self.invoke()

    def test_missing_target_and_parent(self):
        for artifact in (self.target, self.parent):
            family = artifact["vector_families"]["coarse"]
            removed = self.rows[family["collection"]].pop(family["points"][0]["point_id"])
            with self.assertRaises(p.PublicationBlocked):
                self.invoke()
            self.rows[family["collection"]][removed["id"]] = removed

    def test_extra_duplicate_or_partial_rows(self):
        for kind in ("extra", "duplicate", "partial"):
            def hook(method, path, kwargs, value):
                if method == "POST" and value["result"]:
                    if kind == "extra":
                        value["result"].append({"id": "unknown"})
                    elif kind == "duplicate":
                        value["result"].append(value["result"][0])
                    else:
                        value["result"].clear()
                return Response(value)
            self.hook = hook
            with self.subTest(kind=kind), self.assertRaises(p.PublicationBlocked):
                self.invoke()

    def test_wrong_owner_revision_input_metadata_or_legacy_payload(self):
        row = self.target_row()
        original = copy.deepcopy(row["payload"])
        for key, value in (("arxiv_id", "peer"), ("publication_revision", sha(self.parent)),
                           ("revision_input_text", "wrong"), ("revision_model_sha256", "f" * 64),
                           ("revision_family", "fulltext"), ("revision_metadata", {"title": "wrong"}), ("year", 2024)):
            row["payload"] = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(p.PublicationBlocked):
                self.invoke()
        row["payload"] = {"arxiv_id": "p", "publication_revision": self.job["revision"]}
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_fulltext_api_text_and_named_point_vector(self):
        row = self.target_row("fulltext")
        row["payload"]["text"] = "wrong API text"
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        row["payload"]["text"] = row["payload"]["revision_input_text"]
        row["vector"] = {"named": self.values["fulltext"]}
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_float32_normalization_not_prequantized_hash_fallback(self):
        self.assertNotEqual(sha([0.1]), vector_sha256([0.1], 1))
        self.assertEqual(vector_sha256([0.1], 1), vector_sha256([0.10000000149011612], 1))
        self.assertEqual(vector_sha256([1], 1), vector_sha256([1.0], 1))
        for value in ([float("nan")], [float("inf")], [1e100], [True], []):
            with self.subTest(value=value), self.assertRaises(p.PublicationBlocked):
                vector_sha256(value, 1)
        self.target["vector_families"]["coarse"]["points"][0]["vector_sha256"] = sha(self.values["coarse"])
        self.bind()
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_bad_vector_dimension_nonfinite_and_changed_parent_value(self):
        row = self.target_row()
        for values in ([0.1], [float("nan"), 0.2, 0.3], [True, 0.2, 0.3], [0.9, 0.2, 0.3]):
            row["vector"] = values
            with self.subTest(values=values), self.assertRaises(p.PublicationBlocked):
                self.invoke()
        row["vector"] = self.values["coarse"][:]
        family = self.parent["vector_families"]["coarse"]
        self.rows[family["collection"]][family["points"][0]["point_id"]]["vector"][0] = 0.9
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_parent_artifact_lineage_and_owner(self):
        with self.assertRaises(p.PublicationBlocked):
            self.invoke(self.adapter(parents={}))
        bad = copy.deepcopy(self.parent)
        bad["paper_id"] = "peer"
        with self.assertRaises(p.PublicationBlocked):
            self.invoke(self.adapter(parents={sha(self.parent): bad}))
        family = self.parent["vector_families"]["coarse"]
        self.rows[family["collection"]][family["points"][0]["point_id"]]["payload"]["arxiv_id"] = "peer"
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_cross_collection_collision_and_inventory_drift(self):
        row = self.target_row()
        other = self.profiles["fulltext"]["collection"]
        self.rows[other][row["id"]] = copy.deepcopy(row)
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        del self.rows[other][row["id"]]
        def hook(method, path, kwargs, value):
            if path == "/collections":
                value["result"]["collections"].append({"name": "unadmitted"})
            return Response(value)
        self.hook = hook
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def test_redirect_timeout_duplicate_json_and_bounds_fail_closed(self):
        self.hook = lambda *args: Response({}, status=302)
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        self.assertEqual(len(self.sessions[-1].calls), 1)
        def timeout(*args):
            raise requests.Timeout("fixture timeout")
        self.hook = timeout
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        self.hook = lambda *args: Response({}, raw=b'{"status":"ok","status":"ok","result":{}}')
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()
        self.hook = None
        for limit, value in (("max_response_bytes", 10), ("max_total_bytes", 10), ("max_requests", 1)):
            with self.subTest(limit=limit), self.assertRaises(p.PublicationBlocked):
                self.invoke(self.adapter(limits={**self.limits, limit: value}))
        self.assertTrue(all(s.closed for s in self.sessions))

    def test_lost_interval_and_nondurable_attestation(self):
        count = 0
        def guard(pin):
            nonlocal count
            count += 1
            return pin if count == 1 else None
        with self.assertRaises(p.PublicationBlocked):
            self.invoke(self.adapter(interval_guard=guard))
        self.interval["vectors_durable"] = False
        with self.assertRaises(p.PublicationBlocked):
            self.invoke()

    def atomic_fixture(self):
        fixture = RevisionTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.parent = self.artifact(fixture.old)
        self.target = self.artifact(fixture.new, self.parent)
        self.bind()
        with writer(fixture.root) as conn:
            conn.execute("DELETE FROM publication_vector_claims WHERE arxiv_id='p'")
            conn.execute("DELETE FROM paper_publications WHERE arxiv_id='p'")
            conn.execute("DELETE FROM paper_active WHERE arxiv_id='p'")
            conn.commit()
            fixture.base = sha(self.parent)
            p._store(conn, fixture.root, "p", fixture.base, "revision", self.parent, fixture.base)
            conn.execute("UPDATE paper_publications SET state='published' WHERE arxiv_id='p'")
            conn.execute("INSERT INTO paper_active VALUES('p',?,1)", (fixture.base,))
            p._claim_revision_vectors(conn, "p", fixture.base, self.parent, reserve=True)
            conn.commit()
            fixture.job = stage_artifact(conn, fixture.root, self.target)
            self.assertEqual(fixture.job["revision"], self.job["revision"])
        return fixture

    def test_atomic_commit_records_adapter_evidence_and_rechecks_interval(self):
        fixture = self.atomic_fixture()
        count = 0
        def guard(pin):
            nonlocal count
            count += 1
            return pin
        adapter = self.adapter(interval_guard=guard, parents={})
        with writer(fixture.root) as conn:
            p.activate_revision(conn, fixture.root, fixture.job, adapter)
            self.assertEqual(p._read_revision_projection(conn, "p"), fixture.new)
            event = json.loads(conn.execute("SELECT completed FROM paper_publications WHERE revision=?", (self.job["revision"],)).fetchone()[0])[-1]
            self.assertEqual(event["qdrant_readback_evidence"]["interval_sha256"], sha(self.interval))
        self.assertEqual(count, 3)

    def test_interval_lost_just_before_sqlite_commit_rolls_back(self):
        fixture = self.atomic_fixture()
        count = 0
        def guard(pin):
            nonlocal count
            count += 1
            return None if count == 3 else pin
        with writer(fixture.root) as conn:
            with self.assertRaises(p.PublicationBlocked):
                p.activate_revision(conn, fixture.root, fixture.job, self.adapter(interval_guard=guard, parents={}))
            fixture.assert_old(conn)


if __name__ == "__main__":
    unittest.main()
