-- Aria Memory Schema — run once in Supabase SQL editor
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS aria_seller_memory (
    id         SERIAL PRIMARY KEY,
    seller_id  TEXT UNIQUE NOT NULL,
    facts      JSONB NOT NULL DEFAULT '{}',
    embedding  vector(1536),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_seller_id ON aria_seller_memory (seller_id);
