"""Contract tests load only pure AST functions; never import/download a model."""
import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace


def functions():
    path = Path(__file__).parents[1] / "embed_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"_file_sha256", "_embedding_descriptor", "healthz", "embed"}]
    for node in selected:
        node.decorator_list = []
    namespace = {"hashlib": hashlib, "EmbedRequest": object}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace, tree


def test_descriptor_hashes_actual_files_and_is_stable_once_computed(tmp_path):
    namespace, tree = functions()
    model, tokenizer = tmp_path / "model.onnx", tmp_path / "tokenizer.json"
    model.write_bytes(b"actual loaded weight bytes")
    tokenizer.write_bytes(b"actual tokenizer bytes")
    descriptor = namespace["_embedding_descriptor"](model, tokenizer, 768, 384, "query prefix ")
    assert descriptor == {"model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
        "dimension": 768, "max_length": 384, "query_instruction": "query prefix ", "pooling": "CLS/L2"}
    namespace.update(_embedding=descriptor, REPO="Xenova/bge-base-en-v1.5", _small=None)
    before = namespace["healthz"]()
    model.write_bytes(b"different disk contents after startup")
    assert namespace["healthz"]() == before
    assert before["status"] == "ok" and before["small"] is False
    assert before["embedding"] == descriptor
    namespace["_small"] = object()
    assert namespace["healthz"]()["small"] is True
    assignments = {node.targets[0].id: node.value for node in tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert assignments["_embedding"].args[0].id == "_model_path"
    assert assignments["_embedding"].args[1].id == "_tokenizer_path"
    assert assignments["_sess"].args[0].id == "_model_path"
    assert assignments["_tok"].args[0].id == "_tokenizer_path"


def test_embed_response_attaches_loaded_descriptor_without_changing_vector(tmp_path):
    namespace, _ = functions()
    model, tokenizer = tmp_path / "model.onnx", tmp_path / "tokenizer.json"
    model.write_bytes(b"loaded model")
    tokenizer.write_bytes(b"loaded tokenizer")
    descriptor = namespace["_embedding_descriptor"](model, tokenizer, 768, 384, "query prefix ")
    vector, calls = [0.125, -0.0, -0.25], []
    def encode(texts, query=False):
        calls.append((texts, query))
        return [vector]
    namespace.update(_embedding=descriptor, encode=encode, REPO="Xenova/bge-base-en-v1.5", _small=None)
    model.write_bytes(b"changed on disk after startup")
    for query in (False, True):
        response = namespace["embed"](SimpleNamespace(text="same input", query=query))
        assert response["vector"] is vector
        assert response["embedding"] == namespace["healthz"]()["embedding"] == descriptor
        response["embedding"]["model_sha256"] = "0" * 64
        assert namespace["_embedding"]["model_sha256"] == hashlib.sha256(b"loaded model").hexdigest()
    assert calls == [(["same input"], False), (["same input"], True)]
