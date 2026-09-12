"""Download the public ONNX models used by the embedding service.

Model weights are intentionally not committed to git. This script runs during
the Docker build, pins exact Hugging Face revisions through build arguments,
and creates the filename expected by app.py.
"""
import os
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download
from onnxruntime.quantization import QuantType, quantize_dynamic


def download(repo: str, revision: str, destination: Path, patterns: list[str]) -> None:
    snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=destination,
        allow_patterns=patterns,
    )


embed_dir = Path("/app/model")
rerank_dir = Path("/models/ce")

download(
    "BAAI/bge-small-en-v1.5",
    os.environ["BGE_REVISION"],
    embed_dir,
    ["*.json", "*.txt", "*.model", "onnx/model.onnx"],
)
source = embed_dir / "onnx" / "model.onnx"
quantize_dynamic(
    str(source),
    str(embed_dir / "model_int8.onnx"),
    weight_type=QuantType.QInt8,
    per_channel=True,
    reduce_range=True,
)
shutil.rmtree(embed_dir / "onnx")

download(
    "cross-encoder/ms-marco-MiniLM-L6-v2",
    os.environ["RERANK_REVISION"],
    rerank_dir,
    ["*.json", "*.txt", "*.model", "onnx/model_quint8_avx2.onnx"],
)
shutil.move(
    rerank_dir / "onnx" / "model_quint8_avx2.onnx",
    rerank_dir / "model_quint8_avx2.onnx",
)
shutil.rmtree(rerank_dir / "onnx")
