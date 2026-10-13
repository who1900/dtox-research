import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from api import source_representations as catalog
from api.source_generation import load_manifest
from api.tests.test_source_representations import representation, source
from ops import index_source_generation as indexer


DESCRIPTOR = indexer.embedding_descriptor("a" * 64, "b" * 64)
FULL_COVERAGE = {"version": 1, "scope": "full_snapshot", "partial": False,
                 "input_sha256": "a" * 64, "export_report_sha256": "b" * 64}


class Tokenizer:
    def encode(self, text):
        return SimpleNamespace(ids=[1] * (len(text) + 2))


class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status

    def json(self):
        return self.value

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP " + str(self.status_code))


class HTTP:
    def __init__(self):
        self.config = None
        self.points = {}
        self.calls = []
        self.bad_count = False
        self.bad_dimension = False
        self.bad_config = False
        self.fail_upsert = False
        self.bad_hash = False
        self.bad_model = False
        self.retry_once = False

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        assert 0 < timeout <= 30
        if url.endswith("/healthz"):
            return Response({"model": "wrong" if self.bad_model else indexer.MODEL_ID, "embedding": DESCRIPTOR})
        if url.endswith("/embed_batch"):
            if self.retry_once:
                self.retry_once = False
                return Response({}, 503)
            return Response({"vectors": [[0.1] * (384 if self.bad_dimension else 768) for _ in json["texts"]]})
        if method == "GET":
            if self.config is None:
                return Response({}, 404)
            config = {"params": {"vectors": self.config["vectors"],
                                  "on_disk_payload": self.config["on_disk_payload"]},
                      "hnsw_config": self.config["hnsw_config"],
                      "optimizer_config": self.config["optimizers_config"]}
            if self.bad_config:
                config = {**config, "hnsw_config": {"m": 16, "on_disk": False}}
            return Response({"result": {"config": config}})
        if url.endswith("/points/count"):
            return Response({"result": {"count": len(self.points) + int(self.bad_count)}})
        if url.endswith("/points") and method == "POST":
            rows = [self.points[value] for value in json["ids"] if value in self.points]
            if self.bad_hash and rows:
                rows = [{**rows[0], "payload": {**rows[0]["payload"], "generation": "0" * 64}}, *rows[1:]]
            return Response({"result": rows})
        if "/points?" in url:
            if self.fail_upsert:
                raise RuntimeError("interrupted")
            for point in json["points"]:
                self.points[point["id"]] = point
            return Response({"result": {"status": "completed"}})
        if "/index?" in url:
            return Response({"result": {"status": "completed"}})
        if method == "PUT":
            self.config = json
            return Response({"result": True})
        raise AssertionError((method, url))


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "catalog.sqlite"
    text = "First" + "A" * 800 + "TAIL_COMPLETE" + "漢字" * 160
    catalog.build_catalog(path, [source("a"), source("b")], [
        representation("abstract", "a", kind="own_summary", text=text),
        representation("context", "a", origin="b", kind="citation_context", text="Foreign needle")], coverage=FULL_COVERAGE)
    return path, text


def run_index(path, http, **kwargs):
    return indexer.index_generation(path, manifest_path=path.parent / "manifest.json",
        checkpoint_path=path.parent / "ledger.sqlite", embed_endpoint="http://fake-embed",
        qdrant_endpoint="http://fake-qdrant", allow_collection=indexer.collection_name(indexer._catalog_generation(path)),
        request=http.request, tokenizer=Tokenizer(), descriptor=DESCRIPTOR, **kwargs)


def test_plan_is_read_only_no_checkpoint_no_http(corpus):
    path, text = corpus
    before = set(path.parent.iterdir())
    plan = indexer.plan_generation(path, tokenizer=Tokenizer())
    assert set(path.parent.iterdir()) == before
    assert plan["allowed"] and not plan["complete"]
    assert plan["counts"]["representations"] == 2
    assert plan["counts"]["views"] == len(list(indexer.text_views(text, tokenizer=Tokenizer()))) + 1
    assert plan["counts"]["point_upper_bound"] >= plan["counts"]["views"]
    assert plan["estimated_disk_bytes"] > plan["text_bytes"]


def test_heavy_budget_refused_before_collection_or_checkpoint(corpus):
    path, _ = corpus
    http = HTTP()
    plan = indexer.plan_generation(path, max_points=1)
    assert not plan["allowed"]
    with pytest.raises(ValueError, match="heavy generation refused"):
        run_index(path, http, max_points=1)
    assert not http.calls
    assert not (path.parent / "ledger.sqlite").exists()
    assert not indexer.plan_generation(path, max_bytes=1)["allowed"]


def test_views_cover_entire_unicode_and_ascii_without_truncation():
    text = "prefix" + "A" * 1300 + "e\u0301漢字 😀" * 150 + "tail"
    windows = list(indexer.text_views(text, tokenizer=Tokenizer()))
    assert "".join(view[2] for view in windows) == text
    assert windows[0][0] == 0 and windows[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))
    assert all(len(view[2].encode()) <= 4096 and len(Tokenizer().encode(view[2]).ids) <= 384 for view in windows)
    dense_tokenizer = SimpleNamespace(encode=lambda text: SimpleNamespace(ids=[1] * (len(text) * 4 + 2)))
    windows = list(indexer.text_views("A" * 1000, tokenizer=dense_tokenizer))
    assert "".join(view[2] for view in windows) == "A" * 1000
    assert all(len(dense_tokenizer.encode(view[2]).ids) <= 384 for view in windows)


def test_full_index_verifies_counts_ids_hashes_and_disk_config(corpus):
    path, text = corpus
    http = HTTP()
    result = run_index(path, http)
    manifest = load_manifest(path.parent / "manifest.json")
    assert result["complete"] and manifest["embedding"] == DESCRIPTOR
    assert len(http.points) == result["counts"]["points"]
    assert http.config["vectors"]["on_disk"] and http.config["hnsw_config"]["on_disk"]
    assert http.config["hnsw_config"]["max_indexing_threads"] == 1
    assert type(http.config["hnsw_config"]["max_indexing_threads"]) is int
    assert http.config["optimizers_config"] == {"max_optimization_threads": 1}
    assert len([1 for method, url, _ in http.calls
                if method == "PUT" and url.endswith("/collections/" + result["collection"])]) == 1
    assert http.config["on_disk_payload"]
    embedded = [body["texts"] for _, url, body in http.calls if url.endswith("/embed_batch")]
    assert "".join(text for batch in embedded for text in batch).startswith(text)
    foreign = next(point for point in http.points.values() if point["payload"]["representation_id"] == "context")
    assert foreign["payload"]["origin_id"] == "b" and foreign["payload"]["source_id"] == "a"
    assert foreign["payload"]["binding"]["identifier"] == "a"
    assert all(result["collection"] in url or "fake-embed" in url for _, url, _ in http.calls)
    assert sqlite3.connect(path.parent / "ledger.sqlite").execute(
        "SELECT value FROM meta WHERE key='complete'").fetchone() == ("true",)


def test_explicit_limit_never_marks_complete_even_if_equal_to_catalog_size(corpus):
    path, _ = corpus
    result = run_index(path, HTTP(), max_representations=2)
    assert result["partial"] and not result["complete"]
    with pytest.raises(ValueError, match="incomplete"):
        load_manifest(path.parent / "manifest.json")


@pytest.mark.parametrize("failure", ["bad_count", "bad_dimension", "bad_config", "bad_hash", "bad_model"])
def test_failures_never_publish_complete(corpus, failure):
    path, _ = corpus
    http = HTTP()
    setattr(http, failure, True)
    with pytest.raises(ValueError):
        run_index(path, http)
    assert not (path.parent / "manifest.json").exists()
    if (path.parent / "ledger.sqlite").exists():
        conn = sqlite3.connect(path.parent / "ledger.sqlite")
        assert conn.execute("SELECT value FROM meta WHERE key='complete'").fetchone() != ("true",)
        conn.close()


def test_retry_and_resume_are_idempotent(corpus):
    path, _ = corpus
    http = HTTP()
    http.fail_upsert = True
    with pytest.raises(RuntimeError):
        run_index(path, http, batch_size=2)
    assert not (path.parent / "manifest.json").exists()
    http.fail_upsert = False
    http.retry_once = True
    result = run_index(path, http, batch_size=2)
    assert result["complete"]
    assert len(http.points) == result["counts"]["points"]
    assert len({point["id"] for point in http.points.values()}) == len(http.points)
    before = len(http.calls)
    assert run_index(path, http, batch_size=2) == result
    assert all(method != "PUT" and not url.endswith("/embed_batch") for method, url, _ in http.calls[before:])


def test_existing_unowned_collection_is_never_written(corpus):
    path, _ = corpus
    http = HTTP()
    http.config = {"vectors": {"size": 768, "distance": "Cosine", "on_disk": True},
                   "on_disk_payload": True, "hnsw_config": {"m": 16, "ef_construct": 128,
                   "on_disk": True, "max_indexing_threads": 1},
                   "optimizers_config": {"max_optimization_threads": 1}}
    with pytest.raises(ValueError, match="unowned"):
        run_index(path, http)
    assert all(method == "GET" for method, _, _ in http.calls)


@pytest.mark.parametrize("complete", [False, True], ids=["interrupted", "completed"])
@pytest.mark.parametrize("config_key,field", [("hnsw_config", "max_indexing_threads"),
                                             ("optimizers_config", "max_optimization_threads")])
@pytest.mark.parametrize("threads", ["missing", None, 0, 2, 8, -1, True, 1.0, "1", "auto"])
def test_resume_rejects_unsafe_background_threads_without_remote_writes(corpus, complete, config_key, field, threads):
    path, _ = corpus
    http = HTTP()
    http.fail_upsert = not complete
    if complete:
        run_index(path, http)
    else:
        with pytest.raises(RuntimeError, match="interrupted"):
            run_index(path, http)
    manifest_path = path.parent / "manifest.json"
    manifest_before = manifest_path.read_bytes() if complete else None
    http.fail_upsert = False
    http.config[config_key].pop(field)
    if threads != "missing":
        http.config[config_key][field] = threads
    before = len(http.calls)
    with pytest.raises(ValueError, match="collection config mismatch"):
        run_index(path, http)
    assert all(method == "GET" or (method == "POST" and url.endswith(("/points", "/points/count")))
               for method, url, _ in http.calls[before:])
    if complete:
        assert manifest_path.read_bytes() == manifest_before
    else:
        assert not manifest_path.exists()


@pytest.mark.parametrize("stage", ["created", "verified"])
@pytest.mark.parametrize("config_key,field", [("hnsw_config", "max_indexing_threads"),
                                             ("optimizer_config", "max_optimization_threads")])
def test_server_background_thread_drift_never_publishes_complete(corpus, stage, config_key, field):
    path, _ = corpus
    http = HTTP()
    original_request = http.request

    def request(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if (method == "GET" and "/collections/" in url and response.status_code == 200
                and (stage == "created" or http.points)):
            response.value["result"]["config"][config_key][field] = 0
        return response

    http.request = request
    with pytest.raises(ValueError, match="collection config mismatch"):
        run_index(path, http)
    assert not (path.parent / "manifest.json").exists()
    if stage == "created":
        assert not http.points
        assert not any("/index?" in url or url.endswith("/embed_batch") for _, url, _ in http.calls)
        conn = sqlite3.connect(path.parent / "ledger.sqlite")
        try:
            assert conn.execute("SELECT value FROM meta WHERE key='owned'").fetchone() is None
        finally:
            conn.close()


@pytest.mark.parametrize("failure", ["error", "timeout", "conflict", "false", "verify_timeout", "crash", "verified_crash"])
def test_uncertain_creation_never_adopts_matching_collection(corpus, monkeypatch, failure):
    path, _ = corpus
    http = HTTP()
    original_request = http.request
    original_check = indexer._check_config
    creates = []

    def request(method, url, **kwargs):
        if method == "PUT" and url.endswith("/collections/" + indexer.collection_name(indexer._catalog_generation(path))):
            creates.append(kwargs["json"])
            if failure == "error":
                return Response({}, 500)
            if failure == "timeout":
                http.config = kwargs["json"]
                raise TimeoutError("creation outcome unknown")
            if failure == "conflict":
                http.config = kwargs["json"]
                return Response({}, 409)
            if failure == "false":
                http.config = kwargs["json"]
                return Response({"result": False})
        if failure == "crash" and method == "GET" and "/collections/" in url and http.config is not None:
            raise SystemExit("crash before ownership checkpoint")
        if failure == "verify_timeout" and method == "GET" and "/collections/" in url and http.config is not None:
            raise TimeoutError("creation config verification unavailable")
        return original_request(method, url, **kwargs)

    def check(info):
        original_check(info)
        if failure == "verified_crash":
            raise SystemExit("crash after verified config")

    http.request = request
    monkeypatch.setattr(indexer, "_check_config", check)
    expected_error = SystemExit if failure in ("crash", "verified_crash") else ValueError if failure == "false" else RuntimeError
    if failure in ("timeout", "verify_timeout"):
        expected_error = TimeoutError
    with pytest.raises(expected_error):
        run_index(path, http)
    assert len(creates) == 1
    conn = sqlite3.connect(path.parent / "ledger.sqlite")
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='owned'").fetchone() is None
    finally:
        conn.close()
    http.config = creates[0]
    http.request = original_request
    monkeypatch.setattr(indexer, "_check_config", original_check)
    before = len(http.calls)
    with pytest.raises(ValueError, match="unowned"):
        run_index(path, http)
    assert all(method == "GET" for method, _, _ in http.calls[before:])
    assert not http.points and not (path.parent / "manifest.json").exists()


@pytest.mark.parametrize("complete", [False, True])
def test_legacy_ownership_marker_is_not_proof(corpus, complete):
    path, _ = corpus
    http = HTTP()
    http.fail_upsert = not complete
    if complete:
        run_index(path, http)
    else:
        with pytest.raises(RuntimeError):
            run_index(path, http)
    conn = sqlite3.connect(path.parent / "ledger.sqlite")
    try:
        conn.execute("UPDATE meta SET value='1' WHERE key='owned'")
        conn.commit()
    finally:
        conn.close()
    http.fail_upsert = False
    before = len(http.calls)
    with pytest.raises(ValueError, match="unowned"):
        run_index(path, http)
    assert all(method == "GET" for method, _, _ in http.calls[before:])


def test_allowlist_and_checkpoint_identity(corpus):
    path, _ = corpus
    with pytest.raises(ValueError, match="allowlist"):
        indexer.index_generation(path, manifest_path=path.parent / "manifest.json", checkpoint_path=path.parent / "ledger.sqlite",
            embed_endpoint="http://embed", qdrant_endpoint="http://qdrant", allow_collection="papers_coarse",
            tokenizer=Tokenizer(), descriptor=DESCRIPTOR)
    http = HTTP()
    http.fail_upsert = True
    with pytest.raises(RuntimeError):
        run_index(path, http)
    with pytest.raises(ValueError, match="checkpoint identity"):
        run_index(path, http, max_representations=1)


def test_cli_plan_does_not_write(corpus, capsys):
    path, _ = corpus
    before = set(path.parent.iterdir())
    assert indexer.main(["--catalog", str(path), "--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["point_upper_bound"] > 0
    assert set(path.parent.iterdir()) == before


def test_offline_worker_matches_cls_l2_and_never_loads_huggingface(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    model, tokenizer = tmp_path / "model.onnx", tmp_path / "tokenizer.json"
    model.write_bytes(b"offline weights")
    tokenizer.write_bytes(b"offline tokenizer")
    options = SimpleNamespace()
    calls = []
    class Session:
        def __init__(self, path, supplied_options, providers):
            assert path == str(model)
            assert supplied_options is options
            assert providers == ["CPUExecutionProvider"]
            calls.append("loaded-local-model")

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(shape=[None, None, 768])]

        def run(self, outputs, feed):
            return [np.ones((len(feed["input_ids"]), 3, 768), dtype=np.float32)]

    ort = SimpleNamespace(SessionOptions=lambda: options, InferenceSession=Session)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(indexer, "os", SimpleNamespace(name="test", environ={}))
    tokenizer_fake = SimpleNamespace(enable_padding=lambda: None,
        encode_batch=lambda texts: [SimpleNamespace(ids=[1, 2, 3], attention_mask=[1, 1, 1]) for text in texts])
    monkeypatch.setattr(indexer, "load_tokenizer", lambda path: tokenizer_fake)
    messages = iter([["one", "two"], None])
    sent = []
    pipe = SimpleNamespace(recv=lambda: next(messages), send=lambda value: sent.append(value), close=lambda: None)
    indexer._offline_worker(pipe, model, tokenizer)
    assert sent[0] == ("ready", indexer.embedding_descriptor(indexer._file_sha(model), indexer._file_sha(tokenizer)))
    assert sent[1][0] == "vectors" and len(sent[1][1]) == 2
    assert np.linalg.norm(sent[1][1][0]) == pytest.approx(1, rel=1e-6)
    assert options.intra_op_num_threads == options.inter_op_num_threads == 1
    assert not options.enable_cpu_mem_arena and not options.enable_mem_pattern
    assert indexer.os.environ["RAYON_NUM_THREADS"] == "1"
    assert indexer.os.environ["TOKENIZERS_PARALLELISM"] == "false"
    assert calls == ["loaded-local-model"]


def test_offline_assets_use_only_existing_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        indexer.offline_assets(tmp_path / "missing")
    with pytest.raises(ValueError, match="relative"):
        indexer.offline_assets(tmp_path, "../other-model.onnx")


def test_owned_passages_are_never_embedded_copied_or_counted_as_new(tmp_path):
    path = tmp_path / "catalog.sqlite"
    legacy = source("a")
    legacy["publication_revision"] = None
    catalog.build_catalog(path, [legacy], [representation("summary", "a", kind="own_summary", text="summary text"),
        *[representation("own" + str(i), "a", text="existing whole paragraph " + str(i)) for i in range(300)]], coverage=FULL_COVERAGE)
    plan = indexer.plan_generation(path, tokenizer=Tokenizer(), max_points=1)
    assert plan["allowed"]
    assert plan["counts"]["total_representations"] == 301
    assert plan["counts"]["native_owned_representations"] == 300
    assert plan["counts"]["new_indexed_representations"] == plan["counts"]["views"] == 1
    assert plan["native"]["legacy_unmarked_sources"] == 1
    assert plan["native"]["corpus_attested"] is False
    http = HTTP()
    manifest = run_index(path, http, max_points=1)
    assert manifest["complete"] and load_manifest(tmp_path / "manifest.json")
    assert len(http.points) == 1
    assert all(point["payload"]["kind"] != "own_passage" for point in http.points.values())
    assert [body["texts"] for _, url, body in http.calls if url.endswith("/embed_batch")] == [["summary text"]]


def test_normal_abstract_under_token_budget_is_one_complete_view(tmp_path):
    text = "The proposed mechanism preserves all relevant context. " * 25
    tokenizer = SimpleNamespace(encode=lambda value: SimpleNamespace(ids=[1] * (len(value.split()) + 2)))
    assert 256 < len(text.encode()) < 4096
    assert len(tokenizer.encode(text).ids) < 384
    assert list(indexer.text_views(text, tokenizer=tokenizer)) == [(0, len(text), text)]
    path = tmp_path / "catalog.sqlite"
    catalog.build_catalog(path, [source("a")], [representation("summary", "a", kind="own_summary", text=text)],
                          coverage=FULL_COVERAGE)
    plan = indexer.plan_generation(path, tokenizer=tokenizer, max_points=1)
    assert plan["allowed"] and plan["counts"]["views"] == plan["counts"]["point_upper_bound"] == 1
    unknown = indexer.plan_generation(path)
    assert unknown["counts"]["point_upper_bound"] >= plan["counts"]["views"]
    assert unknown["view_count_kind"] == "unicode_codepoint_upper_bound"


def test_pathological_token_splits_have_honest_plan_bound(tmp_path):
    text = "漢字😀x" * 300
    tokenizer = SimpleNamespace(encode=lambda value: SimpleNamespace(ids=[1] * (len(value) * 4 + 2)))
    path = tmp_path / "catalog.sqlite"
    catalog.build_catalog(path, [source("a")], [representation("summary", "a", kind="own_summary", text=text)],
                          coverage=FULL_COVERAGE)
    views = list(indexer.text_views(text, tokenizer=tokenizer))
    assert "".join(view[2] for view in views) == text
    assert all(len(tokenizer.encode(view[2]).ids) <= 384 for view in views)
    exact = indexer.plan_generation(path, tokenizer=tokenizer)
    bound = indexer.plan_generation(path)
    assert exact["counts"]["views"] == exact["counts"]["point_upper_bound"] == len(views)
    assert bound["counts"]["point_upper_bound"] >= len(views)
    assert not indexer.plan_generation(path, tokenizer=tokenizer, max_points=len(views) - 1)["allowed"]


def test_unverified_catalog_cannot_be_promoted_to_complete(tmp_path):
    path = tmp_path / "unverified.sqlite"
    catalog.build_catalog(path, [source("a")], [representation("summary", "a", kind="own_summary")])
    assert indexer.plan_generation(path, tokenizer=Tokenizer())["partial"]
    result = run_index(path, HTTP())
    assert not result["complete"] and result["coverage"]["scope"] == "explicit_input_only"
    with pytest.raises(ValueError):
        load_manifest(tmp_path / "manifest.json")
    forged = {**result, "complete": True}
    (tmp_path / "manifest.json").write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage"):
        load_manifest(tmp_path / "manifest.json")


def test_truncated_export_through_real_producer_never_publishes_complete():
    from ops.tests.test_export_source_packets import ExportTests
    from ops import build_source_representations as producer
    fixture = ExportTests()
    fixture.setUp()
    try:
        fixture.paper("2401.12345", source="spec-markdown")
        fixture.paper("2402.12345", source="spec-markdown")
        report = fixture.run_export(max_sources=1)
        assert report["partial"]
        path = fixture.root / "catalog.sqlite"
        built = producer.produce(fixture.output / report["packet_file"], path,
                                 input_report=fixture.output / "report.json")
        assert built["partial"]
        plan = indexer.plan_generation(path, tokenizer=Tokenizer())
        assert plan["partial"]
        assert plan["coverage"]["export_report_sha256"] == hashlib.sha256(
            (fixture.output / "report.json").read_bytes()).hexdigest()
        assert plan["coverage"]["input_sha256"] == hashlib.sha256(
            (fixture.output / report["packet_file"]).read_bytes()).hexdigest()
        result = run_index(path, HTTP())
        assert not result["complete"]
        with pytest.raises(ValueError):
            load_manifest(fixture.root / "manifest.json")
    finally:
        fixture.doCleanups()


def test_coverage_changes_generation_even_with_same_explicit_seed(tmp_path):
    first = catalog.build_catalog(tmp_path / "one.sqlite", [source("a")], [], generation_id="0" * 64,
                                  coverage=FULL_COVERAGE)
    partial = {**FULL_COVERAGE, "scope": "explicit_input_only", "partial": True}
    second = catalog.build_catalog(tmp_path / "two.sqlite", [source("a")], [], generation_id="0" * 64,
                                   coverage=lambda: partial)
    assert first["generation"] != second["generation"]
    assert catalog.catalog_coverage(tmp_path / "two.sqlite") == partial
