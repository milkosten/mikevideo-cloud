-- 011_notifications — P8. Per-user notification inbox. Idempotent.
-- No reserved-keyword columns (`kind` not `type`, `is_read` not `read`).

CREATE TABLE IF NOT EXISTS mikevideo.notifications (
    id         uuid PRIMARY KEY,
    user_id    text NOT NULL,                       -- the recipient
    kind       text NOT NULL,                       -- new_video | comment | subscriber
    payload    jsonb,                               -- {video_id, actor_*, title, thumb_url, handle, ...}
    is_read    boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_user_idx
    ON mikevideo.notifications (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS notifications_unread_idx
    ON mikevideo.notifications (user_id) WHERE is_read = false;
