-- 013_gps — P-GPS G1. Per-video location track + derived stats. Idempotent.
-- The track (jsonb) is PRIVATE: it lives in the DB (never under the keyless /media
-- path) and is only served through the owner-gated /api/videos/{id}/gps endpoint.
-- No reserved-keyword columns.

ALTER TABLE mikevideo.videos
    ADD COLUMN IF NOT EXISTS has_gps         boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS gps_status      text NOT NULL DEFAULT 'none',   -- none | ready | failed
    ADD COLUMN IF NOT EXISTS gps             jsonb,                          -- normalized {meta, samples:[{t,lat,lon,...}]}
    ADD COLUMN IF NOT EXISTS gps_point_count integer,
    ADD COLUMN IF NOT EXISTS start_lat       double precision,
    ADD COLUMN IF NOT EXISTS start_lng       double precision,
    ADD COLUMN IF NOT EXISTS centroid_lat    double precision,
    ADD COLUMN IF NOT EXISTS centroid_lng    double precision,
    ADD COLUMN IF NOT EXISTS bbox            jsonb,                          -- [minLng,minLat,maxLng,maxLat]
    ADD COLUMN IF NOT EXISTS distance_m      double precision,
    ADD COLUMN IF NOT EXISTS moving_seconds  integer,
    ADD COLUMN IF NOT EXISTS avg_speed       double precision,               -- m/s
    ADD COLUMN IF NOT EXISTS max_speed       double precision;               -- m/s

-- Find "videos near here" fast later (G4). Partial: only rows that have a fix.
CREATE INDEX IF NOT EXISTS videos_geo_idx
    ON mikevideo.videos (centroid_lat, centroid_lng) WHERE has_gps;
