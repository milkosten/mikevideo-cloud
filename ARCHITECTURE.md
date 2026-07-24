# mikevideo-cloud — architecture (control plane + web UI)  [P2]

Canonical: `mikeos-architecture/docs/services/video.md` §4. Built AFTER mikevideo-ingest.

- Auth X-API-KEY→user_id (resolve_agent_key via mikeoscomputers). Storage QUOTA per user.
- POST /api/videos → quota check → mint HMAC ticket {upload_id,user_id,max_size,file_hash,exp}
  → {upload_id, ingest_url:https://up.osmike.com, ticket}.
- POST /api/internal/complete (CALLBACK_SECRET from ingest) → enqueue encode.
- Encode worker: ffmpeg → MP4 + HLS ladder (1080/720/480 m3u8+ts) + poster thumb + duration/dims.
  Keep original + renditions under quota. Storage /data/mikevideo/{user_id}/{video_id}/.
- GET /api/videos (library) · GET /api/videos/{id} (+HLS url) · GET /api/health.
- Browser web app at / : library grid + watch (hls.js / native HLS) + drag-drop upload (§3 protocol
  from the laptop). Warm MikeOS design.
- Postgres: videos, renditions. Guard rails: ISO-8601 ts, parameterized SQL, never-trust-200,
  no reserved-keyword columns.
