-- 006_engagement — P1 engagement primitives: views, likes, watch progress.
-- Idempotent. No reserved-keyword columns.

ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS view_count bigint NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS mikevideo.likes (
    video_id   uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (video_id, user_id)
);
CREATE INDEX IF NOT EXISTS likes_video_idx ON mikevideo.likes (video_id);

CREATE TABLE IF NOT EXISTS mikevideo.watch_progress (
    video_id     uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    user_id      text NOT NULL,
    position_sec double precision NOT NULL DEFAULT 0,
    duration_sec double precision,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (video_id, user_id)
);
CREATE INDEX IF NOT EXISTS watch_progress_user_idx
    ON mikevideo.watch_progress (user_id, updated_at DESC);
