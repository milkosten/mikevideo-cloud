-- 003_transcripts — speech→text (Phase A). Idempotent. No reserved-keyword columns.
-- See docs/SPEECH-PIPELINE.md. Language ID is folded into ASR (Whisper), so the
-- detected language lives on the video row; the transcript body lives in its own table.

ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS spoken_language     text,              -- ISO-639-1, e.g. en/fr/sv (null until transcribed)
    ADD COLUMN IF NOT EXISTS language_confidence real,              -- 0..1 (Whisper language_probability)
    ADD COLUMN IF NOT EXISTS has_speech          boolean,           -- null=unknown, false=silent/music, true=speech found
    ADD COLUMN IF NOT EXISTS transcript_status   text NOT NULL DEFAULT 'pending',  -- pending|running|ready|no_speech|failed
    ADD COLUMN IF NOT EXISTS transcribed_at      timestamptz;

-- One transcript per video (a re-run replaces it). `plain_text` avoids the bare
-- `text` column name; `segments` holds [{start,end,text,words[]}] for subtitles/search.
CREATE TABLE IF NOT EXISTS mikevideo.transcripts (
    id           uuid PRIMARY KEY,
    video_id     uuid NOT NULL UNIQUE REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    language     text,                       -- ISO-639-1
    engine       text,                       -- e.g. faster-whisper
    model        text,                       -- e.g. large-v3
    duration_sec double precision,           -- audio duration Whisper saw
    segments     jsonb NOT NULL DEFAULT '[]'::jsonb,
    plain_text   text NOT NULL DEFAULT '',
    word_count   integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS transcripts_video_idx ON mikevideo.transcripts (video_id);
