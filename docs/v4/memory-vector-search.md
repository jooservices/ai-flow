# Vector search for shared memory

The shared memory service can find related entries by meaning as well as by words. It sends text to the configured Ollama `/api/embed` endpoint and stores 1024-dimensional vectors in PostgreSQL with pgvector. Search combines the existing full-text ranking with cosine similarity using reciprocal rank fusion.

## Configuration and lifecycle

Set these variables in the memory service environment:

- `AIFLOW_MEMORY_EMBED_URL`: Ollama base URL. If it is empty, memory remains keyword-only.
- `AIFLOW_MEMORY_EMBED_MODEL`: model name; defaults to `qwen3-embedding:0.6b`. The selected model must return exactly 1024 values per input.
- `AIFLOW_MEMORY_EMBED_MIN_SIMILARITY`: cosine similarity threshold for a vector hit; defaults to `0.45`.

At startup, the service idempotently enables pgvector, adds `embedding`, `embedding_model` and `embedded_at` to `memory_entry`, and creates an HNSW cosine index. A background worker embeds missing entries in batches, including entries already in the database and entries marked decayed. When the model changes, it refreshes entries whose recorded model differs. Ollama failures leave the entry intact and eligible for a later retry.

Writes only persist the memory entry and signal the background worker; they never wait for Ollama. `/v1/search` and scoped notes search apply their existing company, project and namespace filters to both the word and vector queries. If Ollama is unavailable, search returns the keyword results.

## Privacy and tests

The service does not log the configured embedding URL, request text, vector contents or exception messages from embedding and search operations. The v4 test suite uses a fake Ollama endpoint with pgvector to cover schema setup, batched backfill, model changes, semantic matches without a shared word, scope isolation, retry after outage, keyword fallback, asynchronous writes and log safety.

See [Shared memory setup](../MEMORY.md) for the service and client configuration.
