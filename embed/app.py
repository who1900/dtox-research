"""CPU-only bge-small-en-v1.5 dense embedding service (int8 ONNX), 384-dim, CLS-pooled, L2-normalized.

For search queries, pass {"query": true} to prepend the BGE recommended instruction prefix
"Represent this sentence for searching relevant passages: " before embedding. Passages
(title+abstract, /embed_batch) should be embedded WITHOUT the prefix (default query=false).
"""
import os
import threading

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoTokenizer

MODEL_DIR = "/app/model"
ORT_THREADS = int(os.getenv("ORT_THREADS", "2"))

QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

app = FastAPI()

tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)

sess_opts = ort.SessionOptions()
sess_opts.intra_op_num_threads = ORT_THREADS
# One inter-op thread: the graph is sequential, so extra ones buy nothing and
# each carries its own memory pool.
sess_opts.inter_op_num_threads = 1
# The CPU arena stays ON: it is where the speed comes from (disabling it
# measured 981ms/chunk against 620ms with it). It does grow to fit the largest
# batch ever seen and never hands that memory back, so the rule is to keep
# batch sizes steady -- the 14GB footprint seen earlier came from one-off
# oversized probe batches, not from normal work.
session = ort.InferenceSession(
    f"{MODEL_DIR}/model_int8.onnx",
    sess_options=sess_opts,
    providers=["CPUExecutionProvider"],
)
_input_names = {i.name for i in session.get_inputs()}


class EmbedRequest(BaseModel):
    text: str
    query: bool = False


class EmbedResponse(BaseModel):
    vector: list[float]


class EmbedBatchRequest(BaseModel):
    texts: list[str]


class EmbedBatchResponse(BaseModel):
    vectors: list[list[float]]


def _pool_and_norm(last_hidden_state: np.ndarray) -> np.ndarray:
    cls = last_hidden_state[:, 0]
    norm = cls / np.linalg.norm(cls, axis=1, keepdims=True)
    return norm


def embed(text: str, query: bool = False) -> list[float]:
    if query:
        text = QUERY_INSTRUCTION + text
    inputs = tokenizer(text, return_tensors="np", padding=True, truncation=True, max_length=512)
    feed = {k: v for k, v in inputs.items() if k in _input_names}
    outputs = session.run(None, feed)
    norm = _pool_and_norm(outputs[0])
    return norm[0].astype(float).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    inputs = tokenizer(texts, return_tensors="np", padding=True, truncation=True, max_length=512)
    feed = {k: v for k, v in inputs.items() if k in _input_names}
    outputs = session.run(None, feed)
    norm = _pool_and_norm(outputs[0])
    return norm.astype(float).tolist()


@app.post("/embed", response_model=EmbedResponse)
def embed_endpoint(req: EmbedRequest) -> EmbedResponse:
    return EmbedResponse(vector=embed(req.text, req.query))


@app.post("/embed_batch", response_model=EmbedBatchResponse)
def embed_batch_endpoint(req: EmbedBatchRequest) -> EmbedBatchResponse:
    return EmbedBatchResponse(vectors=embed_batch(req.texts))


# ---------------------------------------------------------------------------
# Cross-encoder reranking
# ---------------------------------------------------------------------------
# A bi-encoder scores a query and a passage separately, so it can only say they
# are about the same things. A cross-encoder reads them together and can tell
# "does this" from "writes about this", which is the one distinction the bands
# have never been able to make.
#
# It is expensive here and the numbers are not hidden: measured on this host,
# 400 ms per pair at two threads, no AVX512 for the quantised model to use. The
# embedder is the ingestion bottleneck and is already short of CPU, so this
# must never take cores from it: two threads, and a lock so exactly one rerank
# runs at a time no matter how many callers arrive.
RERANK_DIR = "/models/ce"
RERANK_THREADS = int(os.getenv("RERANK_THREADS", "2"))
RERANK_MAX_PAIRS = 12
RERANK_MAX_LEN = 320

_rerank_lock = threading.Lock()
_rerank_state = {"sess": None, "tok": None}


def _rerank_session():
    if _rerank_state["sess"] is None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = RERANK_THREADS
        opts.inter_op_num_threads = 1
        _rerank_state["sess"] = ort.InferenceSession(
            f"{RERANK_DIR}/model_qint8_avx512.onnx", opts,
            providers=["CPUExecutionProvider"])
        _rerank_state["tok"] = AutoTokenizer.from_pretrained(RERANK_DIR)
    return _rerank_state["sess"], _rerank_state["tok"]


class RerankRequest(BaseModel):
    query: str
    passages: list[str]


class RerankResponse(BaseModel):
    scores: list[float]


@app.post("/rerank", response_model=RerankResponse)
def rerank_endpoint(req: RerankRequest) -> RerankResponse:
    passages = req.passages[:RERANK_MAX_PAIRS]
    if not passages:
        return RerankResponse(scores=[])
    with _rerank_lock:
        sess, tok = _rerank_session()
        enc = tok([req.query] * len(passages), passages, padding=True,
                  truncation=True, max_length=RERANK_MAX_LEN, return_tensors="np")
        names = {i.name for i in sess.get_inputs()}
        feed = {k: v.astype(np.int64) for k, v in enc.items() if k in names}
        logits = sess.run(None, feed)[0]
    flat = logits.reshape(len(passages), -1)[:, 0]
    return RerankResponse(scores=[float(x) for x in flat])


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
