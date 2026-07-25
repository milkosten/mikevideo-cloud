# SPEECH-PIPELINE.md — voice → text for MikeVideo

The plan for turning a video's **audio** into a **detected language + transcript + subtitles +
searchable text**, and (later) AI titles/tags/summaries. Runs entirely on the **Hetzner box CPU**,
self-hosted, no paid APIs. Backed by a deep-research pass (2024–2026 model landscape) — see the
recommendation summary at the end.

## Design principles (why it's built this way)
- **Quality before speed.** This is a *background batch* job that blocks nothing. We use the most
  accurate open model (**Whisper `large-v3`**), full beam search, VAD, and word-level timestamps.
  If 5 minutes of speech takes an hour of CPU, that is fine — nothing waits on it.
- **CPU-only, on the box.** No GPU. faster-whisper (CTranslate2) `int8` on the Ryzen 9 5950X.
  int8 is within ~0.1% WER of float — effectively free quality, less RAM.
- **Gentle neighbour.** The box also runs the OSM stack (nominatim/overpass/osrm) + imports. The
  transcriber is **capped to a few CPU threads** (`ASR_CPU_THREADS`, default 4) so it never starves
  them. "Do NOT disturb the OSM stack" (house rule) holds.
- **Never blocks encoding.** Transcription has its **own asyncio queue + worker**, separate from the
  encode worker. A new upload still encodes and goes `ready` immediately; the transcript fills in later.
- **House rules.** Paths only — audio is streamed to a temp WAV via ffmpeg, never read into RAM;
  ISO-8601 timestamps; parameterized SQL; idempotent migrations; never-trust-200; no paid services.

## Model & engine choice
- **LID (language identification) is folded into ASR.** Whisper emits a language token in the same
  decoder pass as transcription — one model detects EN/FR/SV *and* transcribes. EN/FR/SV are all
  high-resource, where Whisper's detection is strong. **No separate LID model** in the pipeline.
  (Escape hatch if mixed-language clips ever misdetect: SpeechBrain VoxLingua107 ECAPA, Apache-2.0.)
- **ASR = faster-whisper `large-v3`, `compute_type=int8`, `beam_size=5`, `vad_filter=on`,
  `word_timestamps=on`, `condition_on_previous_text=on`.** VAD (bundled Silero) suppresses Whisper's
  classic hallucinations over silence/music at no accuracy cost. Word timestamps give click-to-seek
  search and precise subtitles.
- **Not** Canary/Parakeet (NVIDIA): they beat Whisper on accuracy but every speed number is
  GPU-measured — no benefit on a CPU box. Reserved for the optional GPU offload (Phase D).
- **Licenses** all permissive: Whisper MIT · faster-whisper MIT · CTranslate2 MIT · Silero VAD MIT ·
  (later) WhisperX BSD-2 · pyannote diarization MIT model (free, HF-gated). Nothing paid or research-only.

## Architecture
```
encode worker (ready, has_audio) ──enqueue──▶ transcribe queue ──▶ transcribe worker (background)
                                                                      │ 1. ffmpeg: original → 16k mono WAV (temp, streamed)
                                                                      │ 2. scripts/transcribe.py (faster-whisper large-v3, subprocess)
                                                                      │    → {language, prob, duration, segments[+words], text}
                                                                      │ 3. persist transcript row + videos cols
                                                                      │ 4. write captions/{lang}.vtt (served like other media)
                                                                      ▼ 5. delete temp WAV → transcript_status = ready
```
- The transcriber runs as a **subprocess** (`scripts/transcribe.py`), so the ~1.5 GB model memory is
  isolated and freed after each job, and a crash can't take down the API. Model weights are cached on
  the **`/data` volume** (`ASR_MODEL_CACHE`), so container rebuilds don't re-download.
- **Gate:** only videos with `has_audio = true` are transcribed (we already probe this at encode).
  Silent clips → `transcript_status = no_speech`, no wasted CPU.
- **Crash-safe:** on startup, `requeue_pending()` re-enqueues anything stuck `running`; `backfill()`
  enqueues existing `ready` + `has_audio` videos that have no transcript yet. Idempotent.

## Storage / output format (migration 003)
On `mikevideo.videos`: `spoken_language` (ISO-639-1, e.g. `en`/`fr`/`sv`), `language_confidence`
(0–1), `has_speech` (bool), `transcript_status` (`pending|running|ready|no_speech|failed`),
`transcribed_at`.

New `mikevideo.transcripts`: `id, video_id, language, engine, model, duration_sec, segments jsonb`
(`[{start,end,text,words[]}]`), `plain_text`, `word_count`, `created_at`. One row per video (re-runs
replace it).

Also written to disk: `{DATA_ROOT}/{user}/{video}/captions/{lang}.vtt` — a **WebVTT** file served by
the existing `/media/...` route (with `text/vtt` mime), so the web `<track>` and app player can load
subtitles directly, same keyless-video_id model as HLS/thumbs.

## API
`GET /api/videos/{id}` gains `spoken_language`, `transcript_status`, `captions_url` (when ready).
`GET /api/videos/{id}/transcript` returns `{language, text, segments}` (auth-gated like the rest of `/api`).

## Config (`.env`)
`ASR_ENABLED` (default true) · `ASR_MODEL` (default `large-v3`) · `ASR_COMPUTE_TYPE` (default `int8`) ·
`ASR_BEAM_SIZE` (default 5) · `ASR_CPU_THREADS` (default 4 — gentle on the OSM stack) ·
`ASR_MODEL_CACHE` (default `/data/mikevideo/models`) · `ASR_LANGUAGE` (default empty = auto-detect).

---

## Phased rollout

### Phase A — Voice→text core  ← **building now**
Audio breakout + built-in LID + `large-v3` transcript + WebVTT, in a background worker gated on
`has_audio`. Delivers: **detected language, full transcript, subtitle file, searchable text.** This is
the foundation and the bulk of the value.

### Phase B — Surface it  ← **SHIPPED**
- Web + app players load the `captions/{lang}.vtt` track (WebVTT `<track>` / ExoPlayer
  `SubtitleConfiguration`), default-selected, with a CC toggle.
- **Full-text search** over transcripts: migration `004` adds a generated `search_tsv` (`simple`
  config, multilingual) + GIN index; `GET /api/search?q=` returns matching videos + the best
  timestamped segment, so a hit **deep-links straight to the spoken moment** (web + app).

### Phase C — The AI layer (free GPU brain)  ← **BUILT; dormant until `OLLAMA_GPU_URL` is set**
`server/enrich.py`: own background worker + 15-min retry sweep. Feeds the timestamped transcript to
the fleet GPU (`OLLAMA_GPU_URL`, qwen3, `/api/chat` JSON mode) → **auto-title, summary, tags,
chapters** (videos.ai_*). **Resilient**: if the flaky GPU is unreachable it leaves `enrich_status =
pending` and retries — nothing blocks. Ollama does the *text* reasoning; it **cannot** do ASR (LLM/
vision only), which is exactly why ASR stays on the CPU box. The web + app already render the AI
title/summary/tags/clickable-chapters (they fall back to the capture date until enrichment runs).
Optional later: speaker **diarization** (pyannote, multi-speaker) via WhisperX.

**To activate C:** set `OLLAMA_GPU_URL=ollama://mikeos:<pass>@81.8.177.182:11443` in
`/root/mikevideo-cloud/.env` on the box and `docker compose up -d mikevideo-cloud` — the retry sweep
enriches the backlog automatically.

### Phase D — GPU ASR offload (only if volume grows)
Stand up a faster-whisper (or NeMo Canary/Parakeet) service on the GPU box behind an
OpenAI-API-compatible server (`whisper-asr-webservice` / `Speaches`). Route with a `VIDEO_ASR_URL`
reachability probe — the same "use GPU if reachable, else CPU" pattern as `OLLAMA_GPU_URL`. Not needed
for a single user; the CPU box is plenty.

---

## Research basis (deep-research, 2024–2026, adversarially verified)
- Whisper folds LID into the ASR decoder pass → no separate LID model needed; strong for high-resource
  EN/FR/SV (built-in FLEURS headline 64.5% is depressed by 20 untrained langs; ~80%+ on trained ones).
- faster-whisper & whisper.cpp load identical Whisper weights → WER within ~0.1%; int8 barely moves WER.
- Canary-1B-v2 / Parakeet-TDT beat large-v3 on accuracy but their speed is GPU-only → CPU box stays on Whisper.
- WhisperX (BSD-2) is the add-on for word timestamps + VAD + optional pyannote diarization (MIT, HF-gated).
- Serving for offload: `whisper-asr-webservice` (MIT) / `Speaches` — OpenAI-API-compatible, CPU+GPU images.
- **Open gap:** no verified CPU real-time-factor for the 5950X survived verification (a Ryzen 7 7700X
  int8 anchor: ~10× realtime for large-v3). We don't care — quality-first, background. Benchmark on the
  box for curiosity, not gating.
