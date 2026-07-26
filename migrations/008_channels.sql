-- 008_channels — P3 channels (@handle) + public creator profile. Idempotent.
-- One channel per user, keyed on user_id. `handle` is the public @slug; it is
-- unique case-insensitively (@Mike == @mike). No reserved-keyword columns.

CREATE TABLE IF NOT EXISTS mikevideo.channels (
    user_id      text PRIMARY KEY,
    handle       text NOT NULL,
    display_name text NOT NULL DEFAULT '',
    bio          text,
    avatar_url   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness + fast lookup for GET /api/channels/{handle}.
CREATE UNIQUE INDEX IF NOT EXISTS channels_handle_lower_idx
    ON mikevideo.channels (lower(handle));
