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

## Open items to resolve at the start of G1 (need before coding ingest)
1. **Exact `.gps.json` schema** — a track `[{t,lat,lng,alt?,speed?,acc?}]`, a single fix, or
   a GeoJSON `LineString`? Pull one sample off a phone (`/sdcard/DCIM/…/clip.mp4.gps.json`).
2. **Sidecar location + naming** — same folder as the clip, `clip.mp4.gps.json` vs
   `clip.gps.json`? (Drives how the app finds it under scoped storage / MediaStore.)
3. **Valhalla region coverage** — confirm the tile extract loaded on Box B covers the footage
   region (e.g. France/Europe for the Cannes clips). If not, load the extract onto the RAID6.
4. **Reverse geocoder** — reuse an existing MikeOS geocoder (places/basemap) if present, else
   stand up **Nominatim** on Box B (the planet/regional DB fits the RAID6).

---

## Phase G1 — Ingest & foundational storage (Box A + app)
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
