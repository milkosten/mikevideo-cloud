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
- `server/app.py` — routes + workers. `server/identity.py` — `authenticate()` (OAuth Bearer via JWKS +
  web session + legacy `resolve_agent_key`; see **Auth model**). `encode.py`/`transcribe.py`/`enrich.py`
  = the three background workers.
- `web/index.html` — the browser app; `web/hls.min.js` — vendored player (NO CDN).
- **Media storage:** `/data/mikevideo/{user_id}/{video_id}/` → `original.mp4`, `video.mp4`
  (faststart), `thumb.jpg`, `hls/master.m3u8` + variant `.m3u8` + `.ts`. Postgres data at
  `/data/mikevideo/pgdata`.

## The flow (how a video becomes watchable)
1. `POST /api/videos` (auth via `authenticate()` w/ `video.write`, quota check) → create `videos` row
   (status `uploading`) → mint an **HMAC ticket** → `{video_id, upload_id, ingest_url, ticket, chunk_size}`.
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

## Search + AI layer (`docs/SPEECH-PIPELINE.md`, Phases B/C)
- **Search** — `GET /api/search?q=` (user-scoped FTS over transcripts). `migrations/004` adds a
  generated `transcripts.search_tsv` (`simple` config, multilingual) + GIN index; results include the
  best matching **timestamped segment**, so a hit deep-links to the spoken moment (web + app).
- **AI enrichment** — `server/enrich.py`: own background worker + 15-min retry sweep. After a transcript
  is ready it feeds the timestamped text to the **free GPU** (`OLLAMA_GPU_URL`, qwen3, `/api/chat` JSON
  mode) → `videos.ai_title/ai_summary/ai_tags/ai_chapters`. **Resilient**: if the flaky GPU is
  unreachable it leaves `enrich_status=pending` and retries — never blocks. Dormant if `OLLAMA_GPU_URL`
  unset. The whole app/web renders the AI title/summary/tags/clickable-chapters (falls back to a
  friendly **date** — never the raw filename).

## Sharing — private by default (`migrations/005`)
`videos.visibility` defaults **`private`**. `POST /api/videos/{id}/visibility {visibility}` (owner)
flips it; `GET /api/public/videos` is the anonymous **public gallery** and `GET /api/public/videos/{id}`
returns a single public video with owner-only fields stripped (private → 404 there). Media stays keyless
on the `video_id`, so a public video is watchable by anyone with the link; the web home shows the public
feed to anonymous visitors.

## Endpoints
Reads: `GET /api/health` · `GET /api/videos` · `GET /api/videos/{id}` · `GET /api/videos/{id}/transcript`
· `GET /api/search` · `GET /api/public/videos` · `GET /api/public/videos/{id}` ·
`GET /media/{user_id}/{video_id}/{path}` (incl. `captions/{lang}.vtt`).
Writes (`scope=video.write`): `POST /api/videos` · `POST /api/videos/{id}/visibility`.
Internal/other: `POST /api/internal/complete` (CALLBACK_SECRET) · `POST /api/auth/login` +
`POST /api/auth/register` (proxy the IdP for the web) · `GET /` (web app) · `GET /hls.js`.
**Clients must use `display_width/height` (not `width/height`) for anything the viewer sees.**

## Auth model — OAuth 2.0 dual-auth resource server (`server/identity.py::authenticate`)
The single `_auth(request, scope=None)` chokepoint (→ `identity.authenticate`) accepts, in order:
1. **OAuth RS256 Bearer JWT** from **`account.osmike.com`** — validated **LOCALLY** against the published
   JWKS (`/oauth/jwks.json`), checking `iss`/`aud=mikevideo`/`exp`/`scope` (`video.read`/`video.write`).
   `sub` == the same `user_id` as always. No per-request IdP call. This is the target standard
   (`docs/implement_oauth.md`).
2. **MikeOS web session JWT** — email/password "Sign in with MikeOS" on the website; validated via the
   IdP `GET /api/auth/me` (**cached**, not per-request). This is the browser **bridge** because the
   provider is **device-grant only today** (no `/oauth/authorize`/code grant → no PKCE yet; swap to
   PKCE when it ships, no cloud change).
3. **Legacy `X-API-KEY`** — `resolve_agent_key` (the phone app still uses this; dual-auth keeps it working).
- A present-but-**invalid** Bearer must **401** — never fall through to the key path.
- **Media** (`/media/...`, incl. HLS + thumbs) stays keyless on the **unguessable `video_id`** — the
  app's ExoPlayer/Coil and browser `hls.js` load it directly. Keep it that way, or clients break.
- **Web credential:** the site stores `mikevideo_token` (session JWT) and sends `Authorization: Bearer`
  — the old "paste X-API-KEY" is gone. `pyjwt[crypto]` verifies OAuth tokens.

## Config (`.env` — never commit; live copy `/root/mikevideo-cloud/.env`)
`PORT` (8090, published on `172.17.0.1` for Caddy) · `DATA_ROOT` · `INGEST_DATA_ROOT` ·
`POSTGRES_PASSWORD` · **`INGEST_HMAC_SECRET`** (MUST match ingest) · **`CALLBACK_SECRET`** (MUST match
ingest) · `INGEST_URL=https://up.osmike.com` · `MIKEOSCOMPUTERS_URL` · `USER_QUOTA_BYTES` ·
`TICKET_TTL_SECONDS` · `MAX_UPLOAD_BYTES` · `MIKEOSCOMPUTERS_URL` (IdP; used for legacy resolve, the web
login proxy, and session `/api/auth/me`).
- **Speech/AI:** `ASR_MODEL` (large-v3) · `ASR_COMPUTE_TYPE` (int8) · `ASR_CPU_THREADS` (4, gentle) ·
  `ASR_MODEL_CACHE=/data/mikevideo/models` (persistent) · `OLLAMA_GPU_URL`
  (`ollama://mikeos:<pass>@81.8.177.182:11443`; the pass lives in `../android_mikeos/CLAUDE.md`) · `OLLAMA_GPU_MODEL`.
- **OAuth:** `ACCOUNT_OSMIKE_ISSUER=https://account.osmike.com` · `OAUTH_AUDIENCE=mikevideo` (public
  JWKS only, no secrets — defaults are correct).

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
- **Test OAuth end-to-end** (provider is LIVE): mint a real token with a valid hive key —
  `curl -X POST https://account.osmike.com/oauth/token -H 'Content-Type: application/json'
  -d '{"grant_type":"urn:mikeos:params:oauth:grant-type:device","agent_key":"<hive key>","audience":"mikevideo","scope":"video.read video.write"}'`
  → `{access_token,…}`; then `curl -H "Authorization: Bearer <tok>" https://video.osmike.com/api/videos`.
  Provider is **device-grant + refresh only** (`/.well-known/openid-configuration` shows no authorize
  endpoint). Test the web session path with a throwaway account via `POST /api/auth/register`.
- **Two live phones now** — Note 10 (`R58N4101P2V`, `d1`) + Pixel 9a (`62271JEBF09145`, `tegu`). With
  both attached, **always target by serial**: `adb -s <serial> …` (bare adb errors "more than one device").
- **Verify the WEBSITE visually** without a desktop browser: open it on a phone —
  `adb -s <serial> shell am start -a android.intent.action.VIEW -d 'https://video.osmike.com/'` then
  screenshot. The site is anonymous-by-default (public feed); "Sign in" is email/password.
- **Extract the app's credential** (debug build) to test as that user:
  `adb -s R58N4101P2V shell 'run-as com.mikeos.video cat files/hive_credentials.json'` → `agent_key`.
  It's a secret — pipe it into `input text`/`curl` via a shell var, don't echo it; wipe the scratch copy.

## MikeOS ecosystem context (you are one service in a whole OS)
MikeOS is a **full de-Googled phone OS**, not a SaaS: own ROM + launcher (`com.mikeos.launcher`) +
first-boot **Setup Wizard** (`com.mikeos.setup`, **shipped** — create account + approve device, no keys)
+ on-device **daemon** (the brain/token agent) + **~33 agent apps** + **~26 clouds**. Identity root is
**`account.osmike.com`** (the IdP, becoming the OAuth 2.0 AS). Onboarding 10 users = 10 email signups +
10 device approvals via the wizard; every app then self-provisions. Canonical orientation:
`mikeos-architecture/ecosystem/README.md` (§7 = how an app on a fresh phone integrates) and
`docs/ACCOUNT-OSMIKE-OAUTH-PLAN.md`.

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
- **Also has:** spoken-word **search** (deep-links to the moment), ExoPlayer **subtitle** track from
  `captions_url`, and the **AI** title/summary/tags/chapters in the ⓘ panel.
- **OAuth STEP 2 (pending):** the app still sends `X-API-KEY` (works via dual-auth). Flip it to a
  daemon-minted Bearer (`GET 127.0.0.1:7743/api/auth/token?aud=mikevideo&scope=…`) once the daemon
  token endpoint ships — no coordination needed (cloud accepts both). See `docs/implement_oauth.md` §2.
- **Build/install** (dev iterate): `./gradlew :app:assembleDebug --no-daemon --max-workers=2` then
  `adb install -r -g app/build/outputs/apk/debug/app-debug.apk`. Phone (Note 10) is **USB adb only**
  right now (Wi-Fi `:5555` = no route). Relaunch with `adb shell am start -n com.mikeos.video/.MainActivity`
  — NOT `monkey` (monkey tasks have no back-stack, so BACK exits the app). Screenshot:
  `adb exec-out screencap -p > out.png`.
- **Real ship = OTA** via `mikeos-appstore` (bump `versionCode` → build → publish → the daemon
  self-installs), per `mikeos-architecture/docs/PUBLISHING-APP-UPDATES.md` — NOT adb. Current phone
  build is a **debug side-load**; the first OTA (release-signed) may need the debug copy uninstalled.
