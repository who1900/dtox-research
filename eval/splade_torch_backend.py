"""Optional CUDA/FP16 SPLADE adapter for isolated retrieval experiments."""
from dataclasses import dataclass


DEFAULT_MODEL_SOURCE = "prithivida/Splade_PP_en_v1"
DEFAULT_MAX_LENGTH = 512
DEFAULT_CACHE_DIR = "C:/Users/User/Documents/work/Claude/.scratch_dtox/splade-20261003/models"


@dataclass
class SparseEmbedding:
    indices: list
    values: list


def pool_splade_numpy(logits, attention_mask):
    """Reference SPLADE pooling, kept NumPy-only for lightweight tests."""
    import numpy as np

    logits = np.asarray(logits)
    mask = np.asarray(attention_mask)
    if logits.ndim != 3 or mask.shape != logits.shape[:2]:
        raise ValueError("expected logits [batch, sequence, vocab] and matching attention mask")
    return np.max(np.log1p(np.maximum(logits, 0)) * mask[..., None], axis=1)


def sparse_embeddings(pooled):
    """Convert pooled vectors to sorted, positive sparse coordinates."""
    import numpy as np

    pooled = np.asarray(pooled)
    if pooled.ndim == 1:
        pooled = pooled[None, :]
    if pooled.ndim != 2:
        raise ValueError("pooled SPLADE vectors must have one or two dimensions")
    result = []
    for row in pooled:
        indices = np.flatnonzero(np.isfinite(row) & (row > 0))
        result.append(SparseEmbedding(
            indices=indices.astype(int).tolist(),
            values=row[indices].astype(float).tolist(),
        ))
    return result


class SpladeTorchBackend:
    """FastEmbed-shaped encoder using an explicitly selected CUDA device."""

    def __init__(self, model_name=DEFAULT_MODEL_SOURCE, cache_dir=DEFAULT_CACHE_DIR, threads=1,
                 max_length=DEFAULT_MAX_LENGTH, precision="fp16", **kwargs):
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be an explicit Hugging Face model source")
        if max_length < 1:
            raise ValueError("max_length must be positive")
        if precision != "fp16":
            raise ValueError("CUDA SPLADE backend supports precision='fp16' only")
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"unsupported SPLADE torch options: {unsupported}")

        try:
            import torch
            from transformers import AutoModelForMaskedLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("Torch and Transformers are required for the CUDA SPLADE backend") from exc
        if not torch.cuda.is_available():
            raise RuntimeError("SPLADE torch backend requires CUDA; CPU fallback is disabled")

        self.model_source = model_name
        self.max_length = int(max_length)
        self.cache_dir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
        self.device = torch.device("cuda")
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=self.cache_dir)
        self.model = AutoModelForMaskedLM.from_pretrained(
            model_name, cache_dir=self.cache_dir, torch_dtype=torch.float16
        ).to(self.device)
        self.model.eval()
        self.max_memory_allocated_bytes = 0
        self.metadata = {
            "backend": "pytorch_cuda",
            "model_source": self.model_source,
            "precision": "float16",
            "precision_argument": precision,
            "cache_dir": self.cache_dir,
            "device": str(self.device),
            "max_length": self.max_length,
            "inference_batch_size": 1,
            "parity_status": "not_checked",
            "max_memory_allocated_bytes": 0,
        }

    def _encode_one(self, text):
        if not isinstance(text, str):
            raise TypeError("SPLADE input must be text")
        torch = self._torch
        tokens = self.tokenizer(
            text, return_tensors="pt", truncation=True, padding=True,
            max_length=self.max_length,
        )
        tokens = {key: value.to(self.device) for key, value in tokens.items()}
        with torch.no_grad():
            logits = self.model(**tokens).logits
            pooled = torch.log1p(torch.relu(logits.float()))
            pooled = pooled * tokens["attention_mask"].unsqueeze(-1)
            pooled = pooled.max(dim=1).values
        self.max_memory_allocated_bytes = max(
            self.max_memory_allocated_bytes,
            int(torch.cuda.max_memory_allocated(self.device)),
        )
        self.metadata["max_memory_allocated_bytes"] = self.max_memory_allocated_bytes
        return sparse_embeddings(pooled[0].float().cpu().numpy())[0]

    def embed(self, texts, batch_size=1, **kwargs):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"unsupported encode options: {unsupported}")
        return (self._encode_one(text) for text in texts)

    def passage_embed(self, texts, batch_size=1, **kwargs):
        return self.embed(texts, batch_size=batch_size, **kwargs)

    def query_embed(self, texts, batch_size=1, **kwargs):
        return self.embed(texts, batch_size=batch_size, **kwargs)


TorchSpladeEncoder = SpladeTorchBackend
