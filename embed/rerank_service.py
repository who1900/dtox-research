"""Cross-encoder rerank service (ms-marco-MiniLM-L6-v2, int8 ONNX), CPU only.

Runs on the Qdrant host rather than next to the embedder: there the model
takes ~22 ms per (query, abstract) pair on two idle ARM cores, against ~400 ms
on the shared ingestion box, which is what kept reranking switched off.

  POST /rerank {"query": "...", "passages": ["...", ...]} -> {"scores": [...]}
  GET  /healthz
"""
import os
import threading
import time

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from huggingface_hub import hf_hub_download
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

REPO = os.getenv("RERANK_REPO", "cross-encoder/ms-marco-MiniLM-L6-v2")
REVISION = os.getenv("RERANK_REVISION", "233902d25c440f23af6f7d6e94d2946bac0bee0a")
MODEL_FILE = os.getenv("RERANK_MODEL_FILE", "onnx/model_qint8_arm64.onnx")
THREADS = int(os.getenv("RERANK_THREADS", "2"))
MAX_LEN = int(os.getenv("RERANK_MAX_LEN", "320"))
MAX_PAIRS = int(os.getenv("RERANK_MAX_PAIRS", "40"))

tokenizer = Tokenizer.from_file(hf_hub_download(REPO, "tokenizer.json", revision=REVISION))
tokenizer.enable_truncation(MAX_LEN)
tokenizer.enable_padding()
opts = ort.SessionOptions()
opts.intra_op_num_threads = THREADS
session = ort.InferenceSession(hf_hub_download(REPO, MODEL_FILE, revision=REVISION), opts,
                               providers=["CPUExecutionProvider"])
input_names = {i.name for i in session.get_inputs()}
# one rerank at a time: two cores, and a burst of callers should queue rather
# than split them and all finish late
lock = threading.Lock()

app = FastAPI()


class RerankRequest(BaseModel):
    query: str
    passages: list[str] = Field(..., min_length=1)


@app.post("/rerank")
def rerank(body: RerankRequest):
    if len(body.passages) > MAX_PAIRS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_PAIRS} passages")
    enc = tokenizer.encode_batch([(body.query, p) for p in body.passages])
    feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
    start = time.monotonic()
    with lock:
        logits = session.run(None, feed)[0]
    return {"scores": [float(x) for x in np.asarray(logits).reshape(-1)],
            "ms": round((time.monotonic() - start) * 1000, 1)}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_FILE}
