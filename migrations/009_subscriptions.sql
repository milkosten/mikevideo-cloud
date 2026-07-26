-- 009_subscriptions — P4. A viewer subscribes to a creator's channel (both are
-- user_ids). Idempotent. No reserved-keyword columns.

CREATE TABLE IF NOT EXISTS mikevideo.subscriptions (
    subscriber_user_id text NOT NULL,
    channel_user_id    text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subscriber_user_id, channel_user_id)
);

-- Count subscribers of a channel fast.
CREATE INDEX IF NOT EXISTS subscriptions_channel_idx
    ON mikevideo.subscriptions (channel_user_id);
-- List the channels a viewer follows (newest first) for the subs feed.
CREATE INDEX IF NOT EXISTS subscriptions_subscriber_idx
    ON mikevideo.subscriptions (subscriber_user_id, created_at DESC);
