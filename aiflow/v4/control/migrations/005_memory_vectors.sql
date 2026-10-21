-- Local multilingual embeddings for hybrid memory retrieval. Safe for databases with the existing memory table.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS memory_embedding_hnsw
    ON memory USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;
