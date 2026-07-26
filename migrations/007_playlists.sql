-- 007_playlists — P2 playlists + Watch Later. Idempotent. No reserved-keyword columns
-- (using `sort_order`, not `position`).

CREATE TABLE IF NOT EXISTS mikevideo.playlists (
    id         uuid PRIMARY KEY,
    user_id    text NOT NULL,
    title      text NOT NULL,
    is_system  text,                       -- 'watch_later' for the built-in, else NULL
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS playlists_user_idx ON mikevideo.playlists (user_id, updated_at DESC);
-- One system playlist of each kind per user (so Watch Later is a singleton).
CREATE UNIQUE INDEX IF NOT EXISTS playlists_user_system_idx
    ON mikevideo.playlists (user_id, is_system) WHERE is_system IS NOT NULL;

CREATE TABLE IF NOT EXISTS mikevideo.playlist_items (
    playlist_id uuid NOT NULL REFERENCES mikevideo.playlists(id) ON DELETE CASCADE,
    video_id    uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    sort_order  integer NOT NULL DEFAULT 0,
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS playlist_items_idx
    ON mikevideo.playlist_items (playlist_id, sort_order, added_at);
