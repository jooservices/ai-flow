-- Search by meaning (JAIF-83): vectors made by an embedding model (Ollama), 1024 dimensions (qwen3-embedding, mxbai-embed-large). Filled in the background,
-- an entry without a vector is found by words only. Run apart from schema.sql: a PostgreSQL without pgvector keeps the memory working by words.
create extension if not exists vector;
alter table memory_entry add column if not exists embedding vector(1024);
alter table memory_entry add column if not exists embedding_model text;
alter table memory_entry add column if not exists embedded_at timestamptz;
create index if not exists memory_entry_embedding on memory_entry using hnsw (embedding vector_cosine_ops);
