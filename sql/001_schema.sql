-- Clinic Concierge knowledge base: documents, chunks, and their embeddings.
-- Works on Supabase (Database > Extensions has "vector") or any Postgres with pgvector >= 0.5.
-- vector(1536) is a placeholder: the ingest replaces it with the embedder's size when it creates the
-- tables (384 for the free local model, 1536 for OpenAI text-embedding-3-small). If you switch models
-- later, drop the tables and ingest again.

create extension if not exists vector;

create table if not exists documents (
    id              text primary key,                 -- file name without extension
    source          text not null,                    -- file name in data/raw
    title           text not null,
    doc_type        text,
    status          text not null default 'current',  -- current | superseded
    effective_date  date,
    expires         date,
    superseded_by   text,
    restricted      boolean not null default false,   -- indexed, but not for individual advice
    parser          text not null,
    file_sha256     text not null,                    -- unchanged file + same pipeline version => skipped
    pipeline_version text not null,
    warnings        jsonb not null default '[]'::jsonb,
    chunk_count     integer not null default 0,
    ingested_at     timestamptz not null default now()
);

create table if not exists chunks (
    id              bigserial primary key,
    document_id     text not null references documents(id) on delete cascade,
    chunk_index     integer not null,
    content         text not null,                    -- what the LLM is shown
    embed_text      text not null,                    -- content + "Document / Section" header (what was embedded)
    metadata        jsonb not null default '{}'::jsonb,
    content_hash    text not null,
    embedding_model text not null,
    embedding       vector(1536) not null,
    created_at      timestamptz not null default now(),
    unique (document_id, chunk_index)
);

-- Approximate nearest-neighbour search on cosine distance.
create index if not exists chunks_embedding_hnsw on chunks using hnsw (embedding vector_cosine_ops);
-- Keyword search, for hybrid retrieval later (exact codes, names, prices).
create index if not exists chunks_content_fts on chunks using gin (to_tsvector('english', content));
create index if not exists chunks_document_id on chunks (document_id);
create index if not exists chunks_content_hash on chunks (content_hash);
create index if not exists chunks_metadata on chunks using gin (metadata);

-- Supabase serves every table in "public" through its web API. Row-Level Security with no policies closes that:
-- the API's roles (anon, authenticated) see nothing. The app connects as the tables' owner, which RLS doesn't
-- restrict, so it keeps working. (Supabase Security Advisor: rls_disabled_in_public.)
alter table documents enable row level security;
alter table chunks enable row level security;
