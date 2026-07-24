# mikevideo-cloud

The **control plane + browser experience** for MikeVideo — auth, storage quota, upload tickets,
Postgres metadata, the ffmpeg encode worker, the watch/feed API, and the `video.osmike.com` web app.

- **Host:** `video.osmike.com` (Hetzner box — **not** Railway). Deploy: `SERVER-ACCESS.md`.
- **Pairs with:** `mikevideo-ingest` (data plane) — it mints the signed upload ticket the client hands
  to ingest, and receives the "blob complete" callback → enqueues encode.
- **Stack:** FastAPI + asyncpg + Postgres + ffmpeg (in-image), Docker. Browser UI = static + hls.js.
- Full design: `ARCHITECTURE.md` and `mikeos-architecture/docs/services/video.md` §4.
