-- mikevideo-cloud schema. Idempotent (IF NOT EXISTS). No reserved-keyword columns.
-- Tracked in mikevideo._migrations by the self-migrator in server/db.py.

CREATE SCHEMA IF NOT EXISTS mikevideo;

CREATE TABLE IF NOT EXISTS mikevideo.videos (
    id            uuid PRIMARY KEY,
    user_id       text NOT NULL,
    device_id     text,
    upload_id     text NOT NULL,
    filename      text NOT NULL,
    content_type  text,
    bytes         bigint NOT NULL DEFAULT 0,
    sha256        text,
    duration_sec  double precision,
    width         integer,
    height        integer,
    status        text NOT NULL DEFAULT 'uploading',  -- uploading|encoding|ready|failed
    error         text,
    taken_at      timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS videos_user_created_idx
    ON mikevideo.videos (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS videos_upload_idx
    ON mikevideo.videos (upload_id);

CREATE TABLE IF NOT EXISTS mikevideo.renditions (
    id        uuid PRIMARY KEY,
    video_id  uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    label     text NOT NULL,          -- e.g. 1080p / 720p / 480p / mp4 / thumb / master
    kind      text NOT NULL,          -- mp4 | hls | thumb
    path      text NOT NULL,          -- absolute path under /data/mikevideo/{user}/{video}/
    bytes     bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS renditions_video_idx
    ON mikevideo.renditions (video_id);
