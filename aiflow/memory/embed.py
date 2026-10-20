"""Vectors for the memory search, made by an Ollama server (`/api/embed`). Any failure gives None: the memory then works by words only,
a write is never refused and nothing is lost (the background fill tries again)."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

DIMENSIONS = 1024  # the size of memory_entry.embedding; the model must answer exactly this
MAX_CHARS = 4000
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_MIN_SIMILARITY = 0.45  # cosine similarity a vector hit needs to count


class Embedder:
    def __init__(self, url: str, model: str = DEFAULT_MODEL, min_similarity: float = DEFAULT_MIN_SIMILARITY, timeout: float = 30.0) -> None:
        self.url, self.model, self.min_similarity, self.timeout = url.rstrip("/"), model, min_similarity, timeout

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Embedder | None":
        url = (env.get("AIFLOW_MEMORY_EMBED_URL") or "").strip()
        if not url:
            return None
        try:
            minimum = float(env.get("AIFLOW_MEMORY_EMBED_MIN_SIMILARITY") or DEFAULT_MIN_SIMILARITY)
        except ValueError:
            minimum = DEFAULT_MIN_SIMILARITY
        return cls(url, (env.get("AIFLOW_MEMORY_EMBED_MODEL") or DEFAULT_MODEL).strip(), minimum)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": [t[:MAX_CHARS] for t in texts]}).encode()
        request = urllib.request.Request(self.url + "/api/embed", data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                vectors = json.loads(response.read()).get("embeddings")
        except Exception:
            return None
        if not isinstance(vectors, list) or len(vectors) != len(texts) or any(not isinstance(v, list) or len(v) != DIMENSIONS for v in vectors):
            return None
        return vectors


def literal(vector: list[float]) -> str:
    """The text form PostgreSQL reads as a vector (`%s::vector`)."""
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def entry_text(title: str, body: str, tags: Any) -> str:
    tag_text = " ".join(tags) if isinstance(tags, (list, tuple)) else str(tags or "")
    return f"{title}\n{body}\n{tag_text}".strip()
