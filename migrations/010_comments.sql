-- 010_comments — P5. Threaded comments + per-viewer likes + an owner "heart".
-- Idempotent. No reserved-keyword columns (comment text lives in `body`, not `text`).

CREATE TABLE IF NOT EXISTS mikevideo.comments (
    id         uuid PRIMARY KEY,
    video_id   uuid NOT NULL REFERENCES mikevideo.videos(id) ON DELETE CASCADE,
    user_id    text NOT NULL,                                   -- the author
    parent_id  uuid REFERENCES mikevideo.comments(id) ON DELETE CASCADE,  -- NULL = top-level
    body       text NOT NULL,
    hearted    boolean NOT NULL DEFAULT false,                  -- the video owner's ❤
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS comments_video_idx  ON mikevideo.comments (video_id, created_at);
CREATE INDEX IF NOT EXISTS comments_parent_idx ON mikevideo.comments (parent_id, created_at);

-- One like per (comment, viewer); count = number of rows.
CREATE TABLE IF NOT EXISTS mikevideo.comment_likes (
    comment_id uuid NOT NULL REFERENCES mikevideo.comments(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (comment_id, user_id)
);
