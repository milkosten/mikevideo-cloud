"""AI enrichment — Phase C. See docs/SPEECH-PIPELINE.md.

Turns a finished transcript into a human title, summary, tags, and chapters using
the fleet's FREE GPU brain (Ollama qwen3) at OLLAMA_GPU_URL. Own background queue,
separate from encode/transcribe. Ollama does the *text* reasoning — it cannot do
ASR, which is exactly why transcription stays on the CPU box.

RESILIENT BY DESIGN — the GPU box is reachable only intermittently (home LAN behind
a flaky proxy). If it's unreachable we DO NOT fail: the video stays `enrich_status =
pending` and a periodic retry loop re-enqueues it, so it enriches the moment the GPU
comes back. Nothing in the watch/upload path ever waits on the GPU.
"""
import asyncio
import base64
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from . import config, db

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[str]" = asyncio.Queue()
_RETRY_EVERY = 900          # re-sweep pending enrichments every 15 min (catch the flaky GPU)
_MAX_TRANSCRIPT_CHARS = 12000   # keep the prompt bounded for an 8B model


def enqueue(video_id: str) -> None:
    if config.ENRICH_ENABLED and config.OLLAMA_GPU_URL:
        _queue.put_nowait(video_id)


# ---------------------------------------------------------------------------
# Ollama client — parse the ollama:// URL and talk to /api/chat
# ---------------------------------------------------------------------------
def _parse_gpu_url(url: str) -> tuple[str, dict] | None:
    """`ollama[s|+http]://user:pass@host:port` → (base_http_url, headers). None if unset."""
    if not url:
        return None
    scheme = url.split("://", 1)[0].lower()
    https = scheme in ("ollama", "ollamas")            # default TLS; ollama+http = plain
    p = urlparse("http://" + url.split("://", 1)[1])
    base = f"{'https' if https else 'http'}://{p.hostname}:{p.port or (11443 if https else 11434)}"
    headers = {}
    if p.username:
        raw = f"{p.username}:{p.password or ''}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    return base, headers


async def _chat_json(prompt: str, system: str) -> dict | None:
    """One JSON-mode chat completion. Returns the parsed object, or None on any
    failure (unreachable / bad response) — the caller treats None as 'try later'."""
    parsed = _parse_gpu_url(config.OLLAMA_GPU_URL)
    if parsed is None:
        return None
    base, headers = parsed
    payload = {
        "model": config.OLLAMA_GPU_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",              # force valid JSON out of Ollama
        "options": {"temperature": 0.2},
        "think": False,                # qwen3: skip visible chain-of-thought
    }
    try:
        # Self-signed proxy → verify=False. Generous timeout: an 8B model on a busy GPU is slow.
        async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(180.0)) as client:
            r = await client.post(f"{base}/api/chat", headers=headers, json=payload)
        if r.status_code != 200:
            logger.warning("enrich: GPU chat http %s: %s", r.status_code, r.text[:200])
            return None
        content = r.json().get("message", {}).get("content", "")
    except Exception as e:
        logger.info("enrich: GPU unreachable (%s) — will retry later", type(e).__name__)
        return None
    return _loads_lenient(content)


def _loads_lenient(text: str) -> dict | None:
    """Parse JSON, tolerating a stray ```json fence or leading prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Prompt building + enrichment
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You are a metadata assistant for a personal video library. Given a video's "
    "speech transcript, produce concise, human-friendly metadata. Reply with ONLY a "
    "JSON object, no prose. Use the SAME language as the transcript for title and "
    "summary. Schema: {\"title\": string (<=70 chars, no quotes, no file names), "
    "\"summary\": string (1-2 sentences), \"tags\": string[] (3-8 lowercase topical "
    "keywords), \"chapters\": [{\"start\": number seconds, \"title\": string}] "
    "(only if the video has distinct sections; else [])}."
)


def _timestamped_transcript(segments: list[dict], plain_text: str) -> str:
    """A compact [mm:ss] transcript so the model can place chapter timestamps."""
    if not segments:
        return plain_text[:_MAX_TRANSCRIPT_CHARS]
    lines = []
    total = 0
    for s in segments:
        t = int(float(s.get("start") or 0))
        line = f"[{t // 60:02d}:{t % 60:02d}] {(s.get('text') or '').strip()}"
        total += len(line) + 1
        if total > _MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


async def _enrich_one(video_id: str) -> None:
    row = await db.pool().fetchrow(
        "SELECT v.transcript_status, v.enrich_status, t.plain_text, t.segments, t.language"
        " FROM mikevideo.videos v LEFT JOIN mikevideo.transcripts t ON t.video_id=v.id"
        " WHERE v.id=$1", video_id)
    if row is None:
        return
    if row["transcript_status"] != "ready" or not (row["plain_text"] or "").strip():
        # No speech / no transcript → nothing to enrich. Mark skipped so we don't loop.
        await db.pool().execute(
            "UPDATE mikevideo.videos SET enrich_status='skipped' WHERE id=$1", video_id)
        return

    segments = row["segments"]
    if isinstance(segments, str):
        segments = json.loads(segments)
    transcript = _timestamped_transcript(segments or [], row["plain_text"])

    await db.pool().execute(
        "UPDATE mikevideo.videos SET enrich_status='running' WHERE id=$1", video_id)

    data = await _chat_json(
        prompt=f"Transcript (language={row['language']}):\n{transcript}",
        system=_SYSTEM)
    if data is None:
        # Unreachable / unparseable → leave pending; the retry sweep will pick it up.
        await db.pool().execute(
            "UPDATE mikevideo.videos SET enrich_status='pending' WHERE id=$1", video_id)
        return

    title = (str(data.get("title") or "").strip() or None)
    summary = (str(data.get("summary") or "").strip() or None)
    tags = data.get("tags")
    tags = [str(t).strip().lower() for t in tags][:12] if isinstance(tags, list) else None
    chapters = data.get("chapters")
    chapters = chapters if isinstance(chapters, list) else None

    await db.pool().execute(
        "UPDATE mikevideo.videos SET ai_title=$2, ai_summary=$3, ai_tags=$4,"
        " ai_chapters=$5, enrich_status='ready', enriched_at=now() WHERE id=$1",
        video_id, title, summary, tags, chapters)
    logger.info("enrich: %s READY title=%r tags=%s", video_id, (title or "")[:60],
                (tags or [])[:5])


async def worker_loop() -> None:
    if not (config.ENRICH_ENABLED and config.OLLAMA_GPU_URL):
        logger.info("enrichment dormant (OLLAMA_GPU_URL unset or disabled)")
        return
    logger.info("enrichment worker started (model=%s)", config.OLLAMA_GPU_MODEL)
    while True:
        video_id = await _queue.get()
        try:
            await _enrich_one(video_id)
        except Exception:
            logger.exception("enrich crashed for %s", video_id)
            try:
                await db.pool().execute(
                    "UPDATE mikevideo.videos SET enrich_status='pending' WHERE id=$1", video_id)
            except Exception:
                pass
        finally:
            _queue.task_done()


async def _sweep_pending() -> None:
    rows = await db.pool().fetch(
        "SELECT id FROM mikevideo.videos"
        " WHERE transcript_status='ready' AND enrich_status IN ('pending','failed')"
        " ORDER BY created_at ASC LIMIT 200")
    for r in rows:
        enqueue(str(r["id"]))
    if rows:
        logger.info("enrichment: swept %d pending video(s)", len(rows))


async def retry_loop() -> None:
    """Periodically re-enqueue pending enrichments so a flaky GPU is eventually caught."""
    if not (config.ENRICH_ENABLED and config.OLLAMA_GPU_URL):
        return
    await _sweep_pending()                      # initial backfill on startup
    while True:
        await asyncio.sleep(_RETRY_EVERY)
        await _sweep_pending()
