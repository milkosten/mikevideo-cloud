-- 002_video_metadata — full video analysis captured at encode time.
-- Idempotent (ADD COLUMN IF NOT EXISTS). No reserved-keyword columns.
--
-- Distinction that fixes the "sideways / upper-part-only" bug:
--   width/height        = CODED pixel dimensions (as the stream stores them).
--   display_width/height = what the viewer must actually SHOW, i.e. coded dims
--                          with the rotation matrix applied (swapped on 90/270).
-- Everything the player, the library UI, and downstream apps need to lay a
-- video out correctly lives here, plus a full ffprobe dump for future use.

ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS rotation          smallint,          -- 0|90|180|270 (normalised, clockwise-to-display)
    ADD COLUMN IF NOT EXISTS display_width     integer,           -- post-rotation width  (what to render)
    ADD COLUMN IF NOT EXISTS display_height    integer,           -- post-rotation height (what to render)
    ADD COLUMN IF NOT EXISTS orientation       text,              -- portrait | landscape | square
    ADD COLUMN IF NOT EXISTS aspect_ratio      text,              -- reduced display AR, e.g. 9:16, 16:9, 4:3
    ADD COLUMN IF NOT EXISTS video_codec       text,              -- h264, hevc, vp9, av1 …
    ADD COLUMN IF NOT EXISTS audio_codec       text,              -- aac, opus … (null if silent)
    ADD COLUMN IF NOT EXISTS pix_fmt           text,              -- yuv420p …
    ADD COLUMN IF NOT EXISTS fps               double precision,  -- frames per second (from avg/r_frame_rate)
    ADD COLUMN IF NOT EXISTS video_bitrate     bigint,            -- bits/sec (video stream)
    ADD COLUMN IF NOT EXISTS audio_bitrate     bigint,            -- bits/sec (audio stream)
    ADD COLUMN IF NOT EXISTS overall_bitrate   bigint,            -- bits/sec (container)
    ADD COLUMN IF NOT EXISTS has_audio         boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS audio_channels    smallint,          -- 1 mono, 2 stereo …
    ADD COLUMN IF NOT EXISTS audio_sample_rate integer,           -- Hz
    ADD COLUMN IF NOT EXISTS color_primaries   text,              -- bt709, bt2020 …
    ADD COLUMN IF NOT EXISTS color_transfer    text,              -- bt709, smpte2084 (PQ), arib-std-b67 (HLG) …
    ADD COLUMN IF NOT EXISTS color_space       text,              -- bt709, bt2020nc …
    ADD COLUMN IF NOT EXISTS is_hdr            boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS probe             jsonb;             -- full ffprobe streams+format dump
