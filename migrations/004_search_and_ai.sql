-- 004_search_and_ai — Phase B (full-text search) + Phase C (AI enrichment).
-- Idempotent. No reserved-keyword columns. See docs/SPEECH-PIPELINE.md.

-- Phase B: a generated tsvector over the transcript (config 'simple' = language-
-- agnostic tokenisation, robust across EN/FR/SV) + a GIN index. Postgres fills it
-- for existing rows on ALTER, and keeps it in sync automatically thereafter.
ALTER TABLE mikevideo.transcripts
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', plain_text)) STORED;

CREATE INDEX IF NOT EXISTS transcripts_search_idx
    ON mikevideo.transcripts USING GIN (search_tsv);

-- Phase C: AI layer written by the free GPU brain (qwen3) from the transcript.
ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS ai_title      text,
    ADD COLUMN IF NOT EXISTS ai_summary    text,
    ADD COLUMN IF NOT EXISTS ai_tags       text[],
    ADD COLUMN IF NOT EXISTS ai_chapters   jsonb,          -- [{start,title}]
    ADD COLUMN IF NOT EXISTS enrich_status text NOT NULL DEFAULT 'pending',  -- pending|running|ready|failed|skipped
    ADD COLUMN IF NOT EXISTS enriched_at   timestamptz;
