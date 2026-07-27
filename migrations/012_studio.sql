-- 012_studio — P12 Creator Studio. A lightweight per-view event log so we can
-- chart views-over-time (view_count stays the fast counter). Idempotent.

CREATE TABLE IF NOT EXISTS mikevideo.video_view_events (
    id         uuid PRIMARY KEY,
    video_id   uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS view_events_video_idx ON mikevideo.video_view_events (video_id, created_at);
CREATE INDEX IF NOT EXISTS view_events_time_idx  ON mikevideo.video_view_events (created_at);
