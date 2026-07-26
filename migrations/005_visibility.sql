-- 005_visibility — videos are PRIVATE by default; the owner can make them public
-- (shareable link). Idempotent. No reserved-keyword columns.
--
-- Media (HLS/thumb/captions) stays served on the unguessable video_id (house rule),
-- so a public video is watchable by anyone with the link; a private one is only ever
-- surfaced to its owner (authed API) and 404s on the public endpoint.

ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS visibility   text NOT NULL DEFAULT 'private',  -- private | public
    ADD COLUMN IF NOT EXISTS published_at timestamptz;                      -- first time made public

CREATE INDEX IF NOT EXISTS videos_visibility_idx
    ON mikevideo.videos (visibility) WHERE visibility = 'public';
