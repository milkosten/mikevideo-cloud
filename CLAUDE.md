# mikevideo-cloud — CLAUDE.md

## What this repo is
The **control plane + browser experience** for **MikeVideo** (MikeOS's self-hosted YouTube). Owns
auth, storage quota, upload **tickets**, the Postgres metadata, the **ffmpeg** encode pipeline, the
watch/feed API, and the **`video.osmike.com`** web app.

- **Live:** `https://video.osmike.com` — Docker on the **Hetzner box** (NOT Railway).
- **Siblings:** `mikevideo-ingest` (data plane @ `up.osmike.com`, receives the chunks) ·
  `mikevideo-app` (Android auto-sync client).
- **Canonical architecture:** `mikeos-architecture/docs/services/video.md` (§4 = this service).
- **Server access + deploy:** `SERVER-ACCESS.md` in this repo.

## Stack & layout
FastAPI + asyncpg + **own Postgres container** (`mikevideo-postgres`) + **ffmpeg in-image**, Docker.
- `server/app.py` — routes + encode worker. `server/identity.py` — `resolve_agent_key`
  (X-API-KEY → user_id via mikeoscomputers), copied from the Railway cloud template.
- `web/index.html` — the browser app; `web/hls.min.js` — vendored player (NO CDN).
- **Media storage:** `/data/mikevideo/{user_id}/{video_id}/` → `original.mp4`, `video.mp4`
  (faststart), `thumb.jpg`, `hls/master.m3u8` + variant `.m3u8` + `.ts`. Postgres data at
  `/data/mikevideo/pgdata`.

## The flow (how a video becomes watchable)
1. `POST /api/videos` (auth `X-API-KEY`, quota check) → create `videos` row (status `uploading`) →
   mint an **HMAC ticket** → returns `{video_id, upload_id, ingest_url, ticket, chunk_size}`.
2. Client uploads chunks to **ingest** (`up.osmike.com`) with the ticket (see `mikevideo-ingest`).
3. Ingest finishes → `POST /api/internal/complete` (signed with `CALLBACK_SECRET`) → we mark the blob
   ready and **enqueue encode**. *(The callback carries `upload_id`; we match on the row stored at
   ticket mint.)*
4. **Encode worker** (background) → ffmpeg → MP4 (faststart) + **HLS** ladder + poster thumbnail +
   probe duration/w/h → move under `/data/mikevideo/{user}/{video}/`, keep the original, delete the
   ingest temp `data.bin` → status `ready` (or `failed`).
5. `GET /api/videos` (library) · `GET /api/videos/{id}` (+ HLS master url) · media served by
   `GET /media/{user_id}/{video_id}/{path}` with byte-range.

## Endpoints
`GET /api/health` · `POST /api/videos` · `POST /api/internal/complete` · `GET /api/videos` ·
`GET /api/videos/{id}` · `GET /media/{user_id}/{video_id}/{path}` · `GET /` (web app) · `GET /hls.js`.

## Auth model (important for clients)
- **API** (`/api/*`, non-internal) is gated by `X-API-KEY → user_id`; users only see their own rows.
- **Media** (`/media/...`, incl. HLS + thumbs) is served on the **unguessable `video_id`** path —
  **no X-API-KEY header required** — so the app's ExoPlayer and Coil (and browser `hls.js`) can load
  it directly. Keep it that way, or clients break.

## Config (`.env` — never commit; live copy `/root/mikevideo-cloud/.env`)
`PORT` (8090, published on `172.17.0.1` for Caddy) · `DATA_ROOT` · `INGEST_DATA_ROOT` ·
`POSTGRES_PASSWORD` · **`INGEST_HMAC_SECRET`** (MUST match ingest) · **`CALLBACK_SECRET`** (MUST match
ingest) · `INGEST_URL=https://up.osmike.com` · `MIKEOSCOMPUTERS_URL` · `USER_QUOTA_BYTES` ·
`TICKET_TTL_SECONDS` · `MAX_UPLOAD_BYTES`.

## Build / run / deploy
```bash
docker compose up -d --build          # local (cloud + postgres)
# on the box (SERVER-ACCESS.md): SSH in, then
cd /root/mikevideo-cloud && git pull && docker compose up -d --build
curl -s https://video.osmike.com/api/health          # {"status":"ok","db":true}
# end-to-end (needs a real X-API-KEY resolved from the IdP):
python3 scripts/e2e_test.py            # ticket → chunks → callback → encode → ready → HLS
```
To change the **browser UI**: edit `web/index.html` (self-contained; vendored `hls.min.js`), commit,
push, then `git pull && docker compose up -d --build` on the box. It's Hetzner, not Railway — no auto-deploy.

## House rules
- **Never load whole files into RAM** — ffmpeg streams (you only pass paths); never read a video into
  memory. **ISO-8601** timestamps. **Parameterized SQL** only, **no reserved-keyword columns**,
  **idempotent** migrations. **Never-trust-200** (confirm rows/ids). **No paid services** (encode is
  CPU ffmpeg on the box — no GPU). Secrets never in git.
- **Do NOT disturb the OSM stack** on the box (`mikeos-*` containers + tmux imports). We reach Caddy
  via `172.17.0.1:8090`; add/keep the `video.osmike.com` Caddy block, `docker restart mikeos-caddy`.
