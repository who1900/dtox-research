"""bge-base-en-v1.5 embedding service (int8 ONNX, CPU), same API as embed/app.py.

Serves the paper-level index only: 768-dim vectors for title + abstract and
for queries. Chunk vectors stay on bge-small; re-embedding 9M chunks with a
model three times slower is not on the table, 164k abstracts is.

  POST /embed        {"text": "...", "query": true}  -> {"vector": [...]}
  POST /embed_batch  {"texts": [...]}                -> {"vectors": [[...], ...]}
  GET  /healthz

Optional second engine (off unless SMALL_MODEL_DIR is set): the very same
bge-small int8 ONNX the Contabo embed-small container serves, so a second
host can share the chunk-embedding load of the pipeline. Own session, own
lock, SMALL_THREADS=1 by default so it cannot starve bge-base queries.

  POST /small/embed_batch  {"texts": [...]}                -> {"vectors": [[...], ...]}
  POST /small/embed        {"text": "...", "query": true}  -> {"vector": [...]}
  (503 when the engine is not configured)
"""
import os
import threading

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from huggingface_hub import hf_hub_download
from pydantic import BaseModel
from tokenizers import Tokenizer

REPO = os.getenv("EMBED_REPO", "Xenova/bge-base-en-v1.5")
REVISION = os.getenv("EMBED_REVISION") or None
MODEL_FILE = os.getenv("EMBED_MODEL_FILE", "onnx/model_quantized.onnx")
THREADS = int(os.getenv("EMBED_THREADS", "2"))
MAX_LEN = int(os.getenv("EMBED_MAX_LEN", "384"))
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

_tok = Tokenizer.from_file(hf_hub_download(REPO, "tokenizer.json", revision=REVISION))
_tok.enable_truncation(MAX_LEN)
_tok.enable_padding()
_opts = ort.SessionOptions()
_opts.intra_op_num_threads = THREADS
_sess = ort.InferenceSession(hf_hub_download(REPO, MODEL_FILE, revision=REVISION), _opts,
                             providers=["CPUExecutionProvider"])
_names = {i.name for i in _sess.get_inputs()}
_lock = threading.Lock()


def encode(texts, query=False):
    """CLS-pooled, L2-normalised vectors, as bge expects."""
    if query:
        texts = [QUERY_INSTRUCTION + t for t in texts]
    out = []
    for i in range(0, len(texts), 32):
        enc = _tok.encode_batch(texts[i:i + 32])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in _names:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        with _lock:
            cls = _sess.run(None, feed)[0][:, 0]
        cls = cls / np.linalg.norm(cls, axis=1, keepdims=True)
        out.extend(v.astype(float).tolist() for v in cls)
    return out


# ---------------------------------------------------------------------------
# Optional bge-small engine (same files, pooling and max_len as embed/app.py)
# ---------------------------------------------------------------------------
SMALL_MODEL_DIR = os.getenv("SMALL_MODEL_DIR", "")
SMALL_MODEL_FILE = os.getenv("SMALL_MODEL_FILE", "model_int8.onnx")
SMALL_THREADS = int(os.getenv("SMALL_THREADS", "1"))
SMALL_MAX_LEN = int(os.getenv("SMALL_MAX_LEN", "512"))
# 32 chunks of 512 tokens held ~2.5 GB of activations in the ORT arena, which
# sat at the unit's memory cap beside an 8.8 GB Qdrant on a 12 GB host. Eight
# at a time and no arena keeps the peak small and gives it back after a batch.
SMALL_SUB_BATCH = int(os.getenv("SMALL_SUB_BATCH", "8"))
_small = None
_small_lock = threading.Lock()

if SMALL_MODEL_DIR:
    _stok = Tokenizer.from_file(os.path.join(SMALL_MODEL_DIR, "tokenizer.json"))
    _stok.enable_truncation(SMALL_MAX_LEN)
    _stok.enable_padding()
    _sopts = ort.SessionOptions()
    _sopts.intra_op_num_threads = SMALL_THREADS
    _sopts.inter_op_num_threads = 1
    _sopts.enable_cpu_mem_arena = False
    _sopts.enable_mem_pattern = False
    _small = ort.InferenceSession(os.path.join(SMALL_MODEL_DIR, SMALL_MODEL_FILE), _sopts,
                                  providers=["CPUExecutionProvider"])
    _snames = {i.name for i in _small.get_inputs()}


def encode_small(texts, query=False):
    """bge-small: CLS-pooled, L2-normalised. Documents get no instruction prefix."""
    if query:
        texts = [QUERY_INSTRUCTION + t for t in texts]
    out = []
    for i in range(0, len(texts), SMALL_SUB_BATCH):
        enc = _stok.encode_batch(texts[i:i + SMALL_SUB_BATCH])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in _snames:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        with _small_lock:
            cls = _small.run(None, feed)[0][:, 0]
        cls = cls / np.linalg.norm(cls, axis=1, keepdims=True)
        out.extend(v.astype(float).tolist() for v in cls)
    return out


app = FastAPI()


class EmbedRequest(BaseModel):
    text: str
    query: bool = False


class EmbedBatchRequest(BaseModel):
    texts: list[str]


@app.post("/embed")
def embed(body: EmbedRequest):
    return {"vector": encode([body.text], query=body.query)[0]}


@app.post("/embed_batch")
def embed_batch(body: EmbedBatchRequest):
    return {"vectors": encode(body.texts)}


@app.post("/small/embed")
def small_embed(body: EmbedRequest):
    if _small is None:
        raise HTTPException(503, "small engine not configured")
    return {"vector": encode_small([body.text], query=body.query)[0]}


@app.post("/small/embed_batch")
def small_embed_batch(body: EmbedBatchRequest):
    if _small is None:
        raise HTTPException(503, "small engine not configured")
    return {"vectors": encode_small(body.texts)}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": REPO, "small": _small is not None}
