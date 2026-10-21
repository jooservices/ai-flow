# Multilingual memory search

AI Flow memory indexes tickets, finished run summaries and verify findings. It keeps PostgreSQL full-text search
and adds semantic search through pgvector. The control host generates embeddings locally with FastEmbed and
ONNX Runtime; it does not send memory text to an external embedding API.

## Setup and settings

The control database must have the pgvector extension available to its PostgreSQL server. Migration
`005_memory_vectors.sql` enables the extension, adds a nullable `vector(384)` column to the existing `memory`
table, and creates an HNSW cosine index. It is safe to apply when the table already contains indexed entries;
the control-loops indexer fills embeddings for existing rows on its next pass.

The default model is `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, a multilingual 384-dimension
model supported by [FastEmbed](https://qdrant.github.io/fastembed/examples/Supported_Models/). FastEmbed runs
the ONNX model on the control host. It downloads model files when first needed and reuses its local cache.

Configure the model and writable cache directory in the control service environment:

| Setting | Default | Purpose |
|---|---|---|
| `AIFLOW4_MEMORY_EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | FastEmbed model name; use a compatible 384-dimension model |
| `AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR` | `~/.cache/fastembed` | Local directory for downloaded model files |

The model name and cache directory are settings, not credentials. No embedding endpoint or API key is used.
Ticket and memory text is embedded inside the control host. Related snippets are included in dev and verify
prompts and are sent to the configured AI provider as part of those prompts, like the ticket content.
Choose the model before indexing memory. Changing model families later requires rebuilding stored vectors so
queries and entries use the same embedding space.

## Indexing and ranking

The control-loops memory job indexes tickets, finished run summaries and verify findings every minute. It
embeds new or changed text; entries with unchanged text and an existing vector are left alone. If embedding
fails, the text is still indexed for full-text search and its missing vector can be filled on a later pass.

`GET /v1/memory/search` keeps `project`, `q` and `limit` (1 to 50), and returns `kind`, `ref`, `ticket`,
`project`, `rank`, `created_at` and `snippet` for each hit. Search combines PostgreSQL `ts_rank` ordering and
pgvector cosine similarity with reciprocal-rank fusion. This lets an entry found by either method appear in the
same ranked list. Related hits use the same ranking, are limited to three entries from the same project, and
exclude entries belonging to the current ticket.

Dev and verify prompts show related hits under **Related past work** with the same advice-only wording: the
ticket wins when past work differs.

## Fallback

Memory remains advice. If the local model cannot load, query embedding fails, or vector search fails, search
uses full-text results. Indexing still writes text for full-text search when embedding is unavailable. If
full-text memory access also fails during a claim, the claim continues with no related hits.
