"""Transcription worker — voice → text (Phase A). See docs/SPEECH-PIPELINE.md.

Runs in its OWN asyncio queue/worker, separate from the encode worker, so it never
blocks encoding: a new upload encodes and goes `ready` immediately, its transcript
fills in later. Quality-first + gentle on the box:

    ready+has_audio ──▶ ffmpeg: original → 16k mono WAV (temp, streamed; paths only)
                    ──▶ scripts/transcribe.py (faster-whisper large-v3, SUBPROCESS)
                    ──▶ persist transcript row + videos cols + captions/{lang}.vtt
                    ──▶ delete temp WAV → transcript_status = ready | no_speech | failed

MEMORY: audio is streamed to a temp WAV by ffmpeg and the transcriber reads that
path — we never load audio into this process. The heavy model lives only in the
subprocess and is freed when it exits.
"""
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from . import config, db

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[str]" = asyncio.Queue()
_TRANSCRIBE_CLI = Path(__file__).resolve().parent.parent / "scripts" / "transcribe.py"


def enqueue(video_id: str) -> None:
    if config.ASR_ENABLED:
        _queue.put_nowait(video_id)


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _out, err = await proc.communicate()
    return proc.returncode, (err.decode(errors="replace")[-4000:] if err else "")


async def _set_status(video_id: str, status: str, **cols) -> None:
    sets = ["transcript_status=$2"]
    vals = [video_id, status]
    i = 3
    for k, v in cols.items():
        sets.append(f"{k}=${i}")
        vals.append(v)
        i += 1
    await db.pool().execute(
        f"UPDATE mikevideo.videos SET {', '.join(sets)} WHERE id=$1", *vals)


def _fmt_ts(t: float) -> str:
    """Seconds → WebVTT timestamp HH:MM:SS.mmm."""
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _write_vtt(path: Path, segments: list[dict]) -> None:
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{_fmt_ts(float(seg['start']))} --> {_fmt_ts(float(seg['end']))}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


async def _transcribe_one(video_id: str) -> None:
    row = await db.pool().fetchrow(
        "SELECT id, user_id, filename, status, has_audio, transcript_status"
        " FROM mikevideo.videos WHERE id=$1", video_id)
    if row is None:
        logger.warning("transcribe: video %s gone", video_id)
        return
    if row["status"] != "ready":
        return                              # only transcribe finished encodes
    if row["has_audio"] is False:
        await _set_status(video_id, "no_speech", has_speech=False)
        return

    out_dir = config.DATA_ROOT / row["user_id"] / str(video_id)
    original = None
    for p in sorted(out_dir.glob("original.*")):
        if p.is_file():
            original = p
            break
    if original is None:
        await _set_status(video_id, "failed")
        logger.error("transcribe: original missing for %s", video_id)
        return

    await _set_status(video_id, "running")

    # 1. Audio breakout → 16 kHz mono WAV (streamed by ffmpeg; paths only).
    wav = out_dir / "audio.16k.wav"
    rc, err = await _run(
        "ffmpeg", "-y", "-i", str(original), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav))
    if rc != 0 or not wav.exists():
        await _set_status(video_id, "failed")
        logger.error("transcribe: ffmpeg audio breakout failed %s: %s", video_id, err[-300:])
        return

    # 2. faster-whisper large-v3 in a subprocess (isolated + freed after).
    out_json = out_dir / "transcript.json"
    config.ASR_MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    cli = [
        sys.executable, str(_TRANSCRIBE_CLI),
        "--audio", str(wav), "--out", str(out_json),
        "--model", config.ASR_MODEL, "--compute-type", config.ASR_COMPUTE_TYPE,
        "--beam-size", str(config.ASR_BEAM_SIZE), "--cpu-threads", str(config.ASR_CPU_THREADS),
        "--cache", str(config.ASR_MODEL_CACHE),
    ]
    if config.ASR_LANGUAGE:
        cli += ["--language", config.ASR_LANGUAGE]
    logger.info("transcribe: %s starting (%s, %d threads)", video_id, config.ASR_MODEL,
                config.ASR_CPU_THREADS)
    rc, err = await _run(*cli)
    wav.unlink(missing_ok=True)             # temp WAV gone whatever happens next
    if rc != 0 or not out_json.exists():
        await _set_status(video_id, "failed")
        logger.error("transcribe: whisper failed %s rc=%s: %s", video_id, rc, err[-400:])
        return

    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception as e:
        await _set_status(video_id, "failed")
        logger.error("transcribe: bad JSON for %s: %s", video_id, e)
        return
    finally:
        out_json.unlink(missing_ok=True)

    segments = data.get("segments") or []
    language = data.get("language")
    confidence = data.get("language_probability")
    plain_text = (data.get("text") or "").strip()
    word_count = int(data.get("word_count") or 0)
    duration = data.get("duration")

    # No speech found (VAD filtered everything / silent-ish clip).
    if not segments or not plain_text:
        await _set_status(video_id, "no_speech", has_speech=False,
                          spoken_language=language, language_confidence=confidence)
        logger.info("transcribe: %s → no speech (lang guess %s)", video_id, language)
        return

    # 3. Persist transcript (upsert — a re-run replaces it).
    await db.pool().execute(
        "INSERT INTO mikevideo.transcripts"
        " (id, video_id, language, engine, model, duration_sec, segments, plain_text, word_count)"
        " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)"
        " ON CONFLICT (video_id) DO UPDATE SET"
        "  language=EXCLUDED.language, engine=EXCLUDED.engine, model=EXCLUDED.model,"
        "  duration_sec=EXCLUDED.duration_sec, segments=EXCLUDED.segments,"
        "  plain_text=EXCLUDED.plain_text, word_count=EXCLUDED.word_count, created_at=now()",
        uuid.uuid4(), video_id, language, "faster-whisper", config.ASR_MODEL,
        duration, segments, plain_text, word_count)

    # 4. WebVTT captions (served by /media like HLS/thumbs).
    try:
        cap_dir = out_dir / "captions"
        cap_dir.mkdir(parents=True, exist_ok=True)
        _write_vtt(cap_dir / f"{language or 'und'}.vtt", segments)
    except Exception as e:
        logger.warning("transcribe: vtt write failed for %s: %s", video_id, e)

    await db.pool().execute(
        "UPDATE mikevideo.videos SET transcript_status='ready', has_speech=true,"
        " spoken_language=$2, language_confidence=$3, transcribed_at=now() WHERE id=$1",
        video_id, language, confidence)
    logger.info("transcribe: %s READY lang=%s conf=%.2f words=%d",
                video_id, language, confidence or 0.0, word_count)


async def worker_loop() -> None:
    if not config.ASR_ENABLED:
        logger.info("transcription disabled (ASR_ENABLED=false)")
        return
    logger.info("transcription worker started (model=%s, threads=%d)",
                config.ASR_MODEL, config.ASR_CPU_THREADS)
    while True:
        video_id = await _queue.get()
        try:
            await _transcribe_one(video_id)
        except Exception as e:
            logger.exception("transcribe crashed for %s", video_id)
            try:
                await _set_status(video_id, "failed")
            except Exception:
                pass
        finally:
            _queue.task_done()


async def requeue_and_backfill() -> None:
    """On startup: re-enqueue anything stuck `running`, then enqueue every `ready`
    video that has audio and no finished transcript yet. Idempotent — a video with
    transcript_status in (ready,no_speech) is skipped, so restarts don't redo work."""
    if not config.ASR_ENABLED:
        return
    rows = await db.pool().fetch(
        "SELECT id FROM mikevideo.videos"
        " WHERE status='ready' AND (has_audio IS NULL OR has_audio=true)"
        "   AND transcript_status IN ('pending','running','failed')"
        " ORDER BY created_at ASC")
    for r in rows:
        enqueue(str(r["id"]))
    if rows:
        logger.info("transcription: enqueued %d video(s) for (re)transcription", len(rows))
