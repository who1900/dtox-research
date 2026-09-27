"""bge-base-en-v1.5 embedding service (int8 ONNX, CPU), same API as embed/app.py.

Serves the paper-level index only: 768-dim vectors for title + abstract and
for queries. Chunk vectors stay on bge-small; re-embedding 9M chunks with a
model three times slower is not on the table, 164k abstracts is.

  POST /embed        {"text": "...", "query": true}  -> {"vector": [...]}
  POST /embed_batch  {"texts": [...]}                -> {"vectors": [[...], ...]}
  GET  /healthz
"""
import os
import threading

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI
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


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": REPO}
