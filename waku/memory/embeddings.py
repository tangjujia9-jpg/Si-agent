"""Explicit embedding backends. Demo vectors test plumbing, not semantic quality."""

import hashlib
import math
import os
import re
from typing import Protocol

DIMENSIONS = 1536


def terms(text: str) -> list[str]:
    """Latin words and CJK unigrams/bigrams, shared by both search branches."""
    non_cjk = re.sub(r"[\u3400-\u9fff]+", " ", text.lower())
    words = re.findall(r"\w+", non_cjk)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run)
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
    return words


def validate_vectors(vectors, count: int, dimensions: int = DIMENSIONS) -> list[list[float]]:
    if len(vectors) != count:
        raise ValueError("embedding response count mismatch")
    out = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError(f"embedding dimensions must be {dimensions}")
        values = [float(v) for v in vector]
        if not all(math.isfinite(v) for v in values) or not any(values):
            raise ValueError("embedding must be finite and nonzero")
        out.append(values)
    return out


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DemoEmbedder:
    model_id = "demo:hash-1536-v1"
    dimensions = DIMENSIONS

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * DIMENSIONS
            for term in terms(text) or ["empty"]:
                digest = hashlib.sha256(term.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
            norm = math.sqrt(sum(v * v for v in vector))
            vectors.append([v / norm for v in vector])
        return vectors


class CompatibleEmbedder:
    """Backend-only, opt-in OpenAI-compatible embeddings with bounded requests."""

    def __init__(
        self, base_url: str, model: str, api_key: str = "", *, dimensions: int | None = None,
        batch_size: int = 16,
    ):
        if dimensions is not None and not 1 <= dimensions <= 4096:
            raise ValueError("embedding dimensions must be between 1 and 4096")
        if not 1 <= batch_size <= 16:
            raise ValueError("embedding batch size must be between 1 and 16")
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.dimensions, self.batch_size = dimensions or DIMENSIONS, batch_size
        self.request_dimensions = dimensions
        # Different endpoints may serve different weights under the same model name.
        endpoint = hashlib.sha256(self.base_url.encode()).hexdigest()[:16]
        # A requested projection may differ from an endpoint's default vector.
        projection = ":requested" if dimensions is not None else ""
        self.model_id = f"compatible:{endpoint}:{model}:{self.dimensions}{projection}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        out = []
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            for offset in range(0, len(texts), self.batch_size):
                batch = texts[offset : offset + self.batch_size]
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                from waku.runtime.telemetry import traced_post

                payload = traced_post(
                    client,
                    f"{self.base_url}/embeddings",
                    kind="embedding",
                    model=self.model,
                    headers=headers,
                    payload={
                        "model": self.model, "input": batch,
                        **({"dimensions": self.request_dimensions} if self.request_dimensions is not None else {}),
                    },
                )
                data = sorted(payload["data"], key=lambda row: row["index"])
                if [row["index"] for row in data] != list(range(len(batch))):
                    raise ValueError("embedding response indexes mismatch")
                out.extend(validate_vectors([row["embedding"] for row in data], len(batch), self.dimensions))
        return out


def configured_embedder() -> Embedder | None:
    """Read settings only at API/worker assembly time; never at import time."""
    backend = os.environ.get("SI_EMBEDDING_BACKEND", "none")
    if backend == "none":
        return None
    if backend == "hash-demo":
        return DemoEmbedder()
    if backend == "openai-compatible":
        url = os.environ.get("SI_EMBEDDING_BASE_URL", "")
        model = os.environ.get("SI_EMBEDDING_MODEL", "")
        if not url.startswith(("https://", "http://")) or not model:
            raise ValueError("configure SI_EMBEDDING_BASE_URL and SI_EMBEDDING_MODEL")
        dimensions = os.environ.get("SI_EMBEDDING_DIMENSIONS", "")
        return CompatibleEmbedder(
            url, model, os.environ.get("SI_EMBEDDING_API_KEY", ""),
            dimensions=int(dimensions) if dimensions else None,
            batch_size=int(os.environ.get("SI_EMBEDDING_BATCH_SIZE", "16")),
        )
    raise ValueError("unsupported SI_EMBEDDING_BACKEND")
