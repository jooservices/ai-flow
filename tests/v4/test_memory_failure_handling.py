"""Embedder load failures re-raise the loader's own error and start the retry cooldown."""
import pytest

from aiflow.v4.control import memory


def test_embedder_load_error_is_reraised_unchanged_and_starts_the_cooldown(monkeypatch):
    loader_error = OSError("model file is corrupt")
    attempts = []

    def create(model_name, cache_dir):
        attempts.append(model_name)
        raise loader_error

    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_MODEL", "corrupt-test-model")
    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR", "/tmp/corrupt-model-cache")
    monkeypatch.setattr(memory, "_create_embedder", create)
    monkeypatch.setattr(memory, "_EMBEDDER", None)
    monkeypatch.setattr(memory, "_EMBEDDER_KEY", None)
    monkeypatch.setattr(memory, "_EMBEDDER_FAILURE_KEY", None)
    monkeypatch.setattr(memory, "_EMBEDDER_RETRY_AT", 0.0)

    with pytest.raises(OSError) as raised:
        memory.embed_texts(["claim"])
    assert raised.value is loader_error
    assert memory._EMBEDDER_FAILURE_KEY == ("corrupt-test-model", "/tmp/corrupt-model-cache")
    assert memory._EMBEDDER_RETRY_AT > 0.0

    with pytest.raises(RuntimeError, match="cooldown"):
        memory.embed_texts(["next claim"])
    assert attempts == ["corrupt-test-model"]
