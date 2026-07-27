# MikeVideo GPS pipeline — plan (per-video location capture → analysis)

**Goal:** ingest the camera's per-clip `<basename>.gps.json` sidecar, store it, and turn it
into real location intelligence — route maps, place names, distance, location-aware AI,
"near me" discovery, personal heatmaps/trips — *accurately* (from real coordinates, not
guessed from speech).

## Two-box architecture (why + which does what)

| Box | Address | Role in this pipeline |
|---|---|---|
| **A — app cloud** | `144.76.45.114` (Hetzner) | `mikevideo-cloud` + Postgres + the video media (`/data/mikevideo`). Stores each clip's **raw GPS json** (small) next to it + the derived summary in Postgres. Runs a background **`geo_enrich`** worker (same resilient pattern as `enrich.py`). |
| **B — geo/compute** | `91.98.177.242` (media/OSRM box: 24c / 125 GB / **115 TB free RAID6**) | The heavy geo work, co-located with **Valhalla** (`127.0.0.1:8002`, map-matching + routing) and **tiles** (`:8080`). New **`mikevideo-geo`** service does map-matching, reverse-geocoding, and route-map rendering; bulk artifacts (matched geometry, rendered maps, geocoder DB) live on the **RAID6** (`/data/mikevideo-geo/`). 24 cores for batch/backfill. |

**Data flow:** app uploads `clip.mp4` + `clip.gps.json` → Box A stores raw + cheap stats →
Box A `geo_enrich` calls Box B (`mikevideo-geo`) → Valhalla map-match + geocode + map render
→ Box A stores `place / distance_m / route_map_url / gps_analysis(jsonb)` → UX consumes it.

**Privacy (baked in from G1):** precise location is **owner-only by default**. Public videos
expose at most a **coarse place name** (city-level) unless the owner explicitly opts to share
the precise route. Never leak exact coordinates on a public endpoint.

## House-rules / box constraints (must hold)
- Parameterized SQL, no reserved-keyword columns, **idempotent** migrations, never-trust-200,
  ISO-8601, keyless media on `video_id`. GPS json is small (KB–few MB) so it may be read to
  parse, but **stream** the video/map renders (never a video into RAM).
- **Do NOT disturb the OSM stack on Box B** — never touch `valhalla`, `mikeos-chat-media`,
  `deploy-*`; no reboots. `mikevideo-geo` is a new, isolated container.
- Bulk geo artifacts go under a dedicated `/data/mikevideo-geo/` on the RAID6 (the space the
  user wants used); keep hot/small state (Postgres) on Box A. Avoid random-I/O storms on md10.

## Open items (updated after inspecting a real phone — Samsung Note10 R58N4101P2V)
1. **~~Schema~~ RESOLVED** — the sidecar is `schema:"mikeos.video.gpstrack/1"`, a JSON object:
   `{schema, video, videoUri, device, startedAt(epoch ms), durationSec, intervalSec(~1),
   source:"MikeOS-daemon", samples:[ {t(sec), ts(epoch ms), lat, lon, alt, accuracy, bearing,
   speed(m/s), stale, fixAgeMs, source} … ]}`. ~1 Hz. Note **`lon`** (not `lng`); WGS84. Small
   (25 s clip ≈ 5 KB; a 12-min clip ≈ ~150 KB). Real data seen: Côte d'Azur, ~43.71,7.34, driving.
2. **~~Location/naming~~ RESOLVED** — `/sdcard/Android/data/com.mikeos.camera/files/Movies/
   MikeCamera/<base>.gps.json` (extension **replaced**, not appended: `MIKE_x.mp4` →
   `MIKE_x.gps.json`).
3. **~~Scoped-storage broker~~ DECIDED → the DAEMON** (coordinator reply
   `mikeos-infrastructure/support/to-mikevideo-response-gps-issue.md`). The daemon runs as
   **root** on the ROM, so scoped storage doesn't apply to it — it reads the camera's
   `Android/data/…/<base>.gps.json` directly. Contract the **daemon team** is adding:
   `POST /api/videos/gps` (camera→daemon on finalize) + `GET /api/videos/{id}/gps` /
   `?uri=` / `?from=&to=` (MikeVideo pulls over the loopback it already uses) + a **root
   backfill sweep** across the whole fleet's sidecars. **MikeVideo's job:** pull from the daemon
   and forward to the G1 cloud endpoint — no storage permission, works while unpaired.
   *Blocked on: the daemon endpoint shipping.* Pre-convention clips (`2026-07-22-…`) = `gps:none`
   (GPS provider was dead before 2026-07-27; nothing to recover).
4. **~~Valhalla coverage~~ CONFIRMED** — full **planet loaded** (covers Côte d'Azur), but the
   **routing tiles are still building** (checked 2026-07-27: graph-edge/sort phase, `/status`→000).
   **Wait** for the build; then `/route` + `/trace_route` (map-match) on Box B `:8002` work
   worldwide. **Reverse geocoder = the SHARED Nominatim** on the OSM box:
   `https://osm.osmike.com/nominatim/reverse?lat=&lon=&format=json` (`Bearer OSM_TOKEN`,
   reachable — returns 401 without the token). **Do NOT** stand up a second geocoder.
   *Split:* routing/map-match = Valhalla (media box `:8002`); place names = Nominatim (osm box).
5. **~~RAID6 carve-out~~ APPROVED** — `/data/mikevideo-geo/` on Box B is the intended use of the
   RAID6 (the "nothing on /data" line is **test-bench-scoped** only). Constraints: isolated dir;
   never touch `valhalla`/`mikeos-chat-media`/`deploy-*`; no reboots; gentle during RAID6 resync;
   cap CPU so G2 doesn't starve Valhalla's tile build.

*Still needed before G2 build starts:* (a) Valhalla tile build finishes, (b) `OSM_TOKEN` for
Nominatim, (c) daemon `/api/videos/gps` endpoint for the live app path (G1 cloud + backfill
already work without it).

---

## Phase G1 — Ingest & foundational storage — ✅ DONE (2026-07-27)
*Cloud ingest + storage + stats + privacy shipped; 5 real tracks backfilled.*

**Shipped & verified:** migration `013_gps` (has_gps/gps_status/gps jsonb/point_count/
start+centroid lat-lng/bbox/distance_m/moving_seconds/avg+max_speed + partial geo index).
`POST /api/videos/{id}/gps` (owner) validates `mikeos.video.gpstrack/1`, normalizes samples
(lat/lon/alt/spd/brg/acc, downsamples >4000), derives haversine distance/bbox/centroid/speed →
stored **in the DB (never under keyless /media)**. `GET /api/videos/{id}/gps` — **owner: full
precise track; public video: coarse ~1 km centroid + distance only, no samples; private→404**
(all three proven live). `has_gps`+`distance_m` added to video detail. **Backfill:** pulled the
5 MikeCamera sidecars off the Samsung, matched by filename, ingested — real Côte d'Azur data
(two drives 220 m / 460 m @ ~49 km/h, three stationary shots). Local GPS copies wiped after.

**Deferred (needs a cross-repo decision — see open item #3):** the *automatic app-side* ingest.
Under scoped storage MikeVideo cannot read the camera's private `Android/data` folder, so the
production auto-path needs the **camera or daemon** to expose the sidecar. The backfill proves
the cloud pipeline; the app hook lands once the broker is chosen.

### Original G1 design (for reference)
*Get the data flowing with zero external dependencies.*

- **App (sync):** for each clip, resolve its sibling `<base>.gps.json` via MediaStore (same
  relative path), read it (small), and upload it with the video (new step in the upload flow
  or a follow-up `POST /api/videos/{id}/gps`). Ledger records gps-synced state.
- **Cloud (Box A):** migration `013_gps` → `videos.has_gps`, `gps_status`
  (`pending|ready|none|failed`), `gps jsonb` (normalized track), `gps_point_count`,
  `lat`,`lng` (start), `centroid_lat/lng`, `bbox jsonb`, `distance_m`, `moving_seconds`,
  `avg_speed`,`max_speed`. Endpoint `POST /api/videos/{id}/gps` (owner, `video.write`) stores
  the raw as `gps.json` in the media dir + **normalizes** the track to a canonical
  `[{t,lat,lng}]` and computes cheap stats inline (haversine distance, bbox, centroid, speed).
- **Read:** `GET /api/videos/{id}/gps` (owner: full track; public: coarse only, per privacy).
- **Test:** upload a real sidecar → row shows `has_gps`, point count, distance, bbox; anon
  read returns no precise coords. Bench-verify the app finds+uploads the sidecar.

## Phase G2 — Geo Analysis Service on Box B (Valhalla + geocode, RAID6)
*Turn raw points into a snapped route + real place names.*

- Stand up **`mikevideo-geo`** (FastAPI, Docker) on Box B, isolated, artifacts on
  `/data/mikevideo-geo/`. Internal endpoints:
  - `POST /match` → Valhalla `/trace_attributes` (map-match to roads) → snapped polyline,
    matched distance, road/street names, confidence.
  - `POST /geocode` → reverse-geocode start / midpoint / end → `{place, city, area, country}`.
  - `POST /summary` → route summary (matched distance, moving vs stopped, corridor of places).
- **Box A `geo_enrich` worker** (resilient + retry sweep, like `enrich.py`): after a track is
  `ready`, call Box B → store `place`, `place_start`, `place_end`, `road_names`,
  `matched_distance_m`, `gps_analysis jsonb`, `geo_status`. Dormant/degrades gracefully if
  Box B is unreachable (never blocks).
- **Test:** a Cannes clip resolves to real street/place names + a snapped distance close to the
  raw haversine; retry works when Box B is toggled.

## Phase G3 — Route maps + on-video UX (web + app)
*Show it.*

- Box B renders a **static route-map image** (polyline over the `:8080` tiles) per video → PNG
  on the RAID6, served (thumbnail-style, cached). `route_map_url` stored on Box A.
- **Web watch page:** a route **mini-map** + "Filmed near **{place}** · {distance} · {duration}",
  click → larger map. Owner-only precise route; public shows coarse place + a soft map.
- **App:** a **Location** card in the ⓘ info panel (place, distance, a map image; tap to open).
- **Test:** chrome-pool + bench screenshots show the map + place line on a real clip.

## Phase G4 — Location-aware discovery + AI
*Make everything else smarter with real geography.*

- **AI enrichment (`enrich.py`):** feed the resolved **places + route corridor** into the
  prompt → titles/summaries/tags that are *actually* location-correct (e.g. "A drive from
  Cannes to Antibes along the Croisette"), not inferred from speech.
- **Explore/Search (P11 extension):** "videos **near here**" (bbox/centroid distance) + place
  chips; optional map view of public videos.
- **Creator Studio (P12 extension):** places filmed, total distance covered, a places list.
- **Test:** an AI title reflects the real route; "near me" returns the right clips.

## Phase G5 — Fleet-scale analysis on the RAID6/24-core box
*Use the compute + space headroom.*

- **Cross-video** analysis (batch on Box B): a personal **heatmap** of everywhere you've filmed;
  **auto-trip clustering** (group clips into "trips" by place + time — "Trip to the Riviera,
  3 days, 12 clips"); "**on this day / near here**" resurfacing; **per-place galleries**.
- **Backfill worker**: re-run G1–G4 over all existing videos as sidecars arrive; idempotent,
  serial, gentle on the RAID6.
- **Test:** heatmap + at least one auto-detected trip render from the real library.

---

### Rollout order & safety
G1 is independent and ships first (data starts accumulating immediately). G2 depends on Box B
bring-up (Valhalla coverage + geocoder). G3–G5 layer on top and are individually shippable.
Every phase: cloud (idempotent migration + routes) + app where relevant + verify on the test
bench (`91.98.177.242:6520`) and chrome-pool, then deploy. Location stays owner-private by
default throughout.
