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
4. **Encode worker** (background, `server/encode.py`) → **`analyze()`** (full ffprobe) → ffmpeg →
   MP4 (faststart) + **HLS** ladder + poster thumbnail → move under `/data/mikevideo/{user}/{video}/`,
   keep the original, delete the ingest temp `data.bin` → persist all metadata → status `ready`
   (or `failed`). See **Video analysis & metadata** below — the encode is rotation- and aspect-correct.
5. `GET /api/videos` (library) · `GET /api/videos/{id}` (+ HLS master url + full metadata) · media
   served by `GET /media/{user_id}/{video_id}/{path}` with byte-range.

## Video analysis & metadata (rotation-correct pipeline — read before touching `encode.py`)
Phone clips are recorded **coded landscape with a rotation matrix** (e.g. `1920x1080` + `rotation:-90`
→ must be *displayed* `1080x1920` portrait). The historical bug: we stored coded dims, scaled the HLS
ladder off the wrong axis, and wrote a false `RESOLUTION=1920x1080` into `master.m3u8` — players trust
that attribute, so portrait videos rendered sideways / "only the upper part." **The fix (keep it):**
- **`analyze(path)`** runs `ffprobe -show_format -show_streams -print_format json` and derives:
  rotation (from **Display Matrix side-data** `rotation` AND legacy `tags.rotate`), **display** dims
  (swapped on 90/270), reduced aspect (gcd), orientation, fps, codecs, pix_fmt, bitrates, audio
  stream (or `has_audio=false` → `-an`), color/HDR. Paths only — no bytes into RAM.
- **Ladder is driven off the DISPLAY short-side** (`_plan_rungs`/`_rung_dims`): a portrait 1080p rung
  is `1080x1920`, a landscape one `1920x1080`. Explicit **even** dims + `setsar=1` (square pixels),
  **never upscales**, and each variant's `RESOLUTION=` is written from the **actual** output geometry.
  ffmpeg 7.x auto-applies the display matrix, and we strip the `rotate` tag on output
  (`-metadata:s:v:0 rotate=0`) so nothing double-rotates.
- **Backfill** (`backfill_metadata`, runs on startup, idempotent): re-probes every `ready` video
  missing `display_width`; re-encodes only the rotated / non-16:9 ones (serial through the one worker).
- **Schema:** `migrations/002_video_metadata.sql` added `display_width/height, rotation, orientation,
  aspect_ratio, video_codec, audio_codec, pix_fmt, fps, *_bitrate, has_audio, audio_channels,
  audio_sample_rate, color_*, is_hdr, probe jsonb` (`width/height` stay = coded). `server/db.py`
  registers a json/jsonb codec so `probe` round-trips as a Python dict.

## Speech → text (voice transcription — `server/transcribe.py`, `docs/SPEECH-PIPELINE.md`)
Own background worker (separate queue from encode, so it NEVER blocks encoding). After a video goes
`ready` with `has_audio`, it: ffmpeg-breaks-out a 16 kHz mono WAV (paths only) → runs
`scripts/transcribe.py` (**faster-whisper `large-v3`, int8, CPU**) as a subprocess → stores the
detected **language + timestamped segments + plain text** and writes a **WebVTT** caption file. LID is
folded into ASR (no separate model). **Quality-first**: this is background batch — slow is fine; it's
capped to `ASR_CPU_THREADS` (default 4) to stay gentle on the OSM stack. Schema:
`migrations/003_transcripts.sql` (`videos.spoken_language/transcript_status/…` + a `transcripts`
table). Startup `requeue_and_backfill()` transcribes existing `ready`+`has_audio` videos. Ollama can
NOT do ASR — the free GPU (qwen3) is for the *downstream* AI layer (titles/tags/summaries) in Phase C.

## Endpoints
`GET /api/health` · `POST /api/videos` · `POST /api/internal/complete` · `GET /api/videos` ·
`GET /api/videos/{id}` · `GET /api/videos/{id}/transcript` · `GET /media/{user_id}/{video_id}/{path}`
(incl. `captions/{lang}.vtt`) · `GET /` (web app) · `GET /hls.js`.
`GET /api/videos` and `/api/videos/{id}` now return the metadata above — **clients must use
`display_width/height` (not `width/height`) for anything the viewer sees.**

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

## Work methods (how to verify + operate — proven this session)
**Deploy** (Hetzner, not Railway): `ssh` in per `SERVER-ACCESS.md` → `cd /root/mikevideo-cloud &&
git pull && docker compose up -d --build mikevideo-cloud` (rebuild ONLY the cloud service — leave
`mikevideo-postgres`/`mikevideo-ingest` running). Then `curl -s https://video.osmike.com/api/health`.
- **Query the DB** (own Postgres container): `docker exec -i mikevideo-postgres psql -U mikevideo -d
  mikevideo` — user=db=`mikevideo`. Pipe SQL via a heredoc; inline `\x27` quoting breaks over SSH.
- **Inspect real media** on the box without touching prod: files live at
  `/data/mikevideo/{user}/{video}/`. Probe with the vendored ffmpeg image, e.g.
  `docker run --rm -v /data/mikevideo:/data/mikevideo linuxserver/ffmpeg:latest -i <file>` (look for
  `Display Matrix: rotation`), or `--entrypoint ffprobe ... -show_streams` for JSON. **Always verify a
  real phone clip's rotation shape before trusting the parser** — that's how the bug was pinned.
- **Verify an encode fix for real**: run the exact ffmpeg command your code will run into a scratch
  dir on the box, then ffprobe the output segment — the HLS `.ts` must have the right `width/height`,
  `SAR 1:1`, `DAR`, and **no rotation side-data**; `master.m3u8` `RESOLUTION=` must match the frames.

## The Android app (`../mikevideo-app`, `com.mikeos.video`) — how it consumes this service
Kotlin/Compose + Media3(ExoPlayer)/Coil. It reads this API with its hive `X-API-KEY`; media loads
keyless off the `video_id`. Key files: `net/VideoCloudClient.kt` (parses the metadata above into
`Video`, with `dispW/dispH/aspectRatioF` helpers), `MainActivity.kt` (library + player), `VideoViewModel.kt`.
- **Player** = full-bleed `PlayerView` (`RESIZE_MODE_FIT`), immersive (system bars hidden), starts
  **chrome-free** (`controllerAutoShow=false` + `hideController()`), tap to reveal controls. Uses
  **display** dims. Technical details live behind an **ⓘ Information** panel, not on the video.
- **Library** = orientation-aware `LazyVerticalStaggeredGrid`; **pinch-to-zoom** column density
  (2/3/4, persisted in SharedPreferences `mikevideo_ui/grid_columns`). Clean tiles: frame + duration
  only, **no filenames** (a technical detail users don't care about).
- **Build/install** (dev iterate): `./gradlew :app:assembleDebug --no-daemon --max-workers=2` then
  `adb install -r -g app/build/outputs/apk/debug/app-debug.apk`. Phone (Note 10) is **USB adb only**
  right now (Wi-Fi `:5555` = no route). Relaunch with `adb shell am start -n com.mikeos.video/.MainActivity`
  — NOT `monkey` (monkey tasks have no back-stack, so BACK exits the app). Screenshot:
  `adb exec-out screencap -p > out.png`.
- **Real ship = OTA** via `mikeos-appstore` (bump `versionCode` → build → publish → the daemon
  self-installs), per `mikeos-architecture/docs/PUBLISHING-APP-UPDATES.md` — NOT adb. Current phone
  build is a **debug side-load**; the first OTA (release-signed) may need the debug copy uninstalled.
