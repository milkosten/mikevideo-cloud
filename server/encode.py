"""Encode worker — ffmpeg turns a ready ingest blob into MP4 + HLS + thumb.

MEMORY: ffmpeg streams from/to file paths; we NEVER read a whole video into RAM.
We only pass paths on the command line. The single Python-side hash pass (none
here) is avoided entirely — ingest already verified the whole-file sha256.

Layout produced under {DATA_ROOT}/{user_id}/{video_id}/:
    original.<ext>          the kept source (moved from ingest data.bin)
    video.mp4              H.264 faststart progressive MP4
    hls/master.m3u8       HLS master playlist (variant ladder)
    hls/<h>p.m3u8 + *.ts  per-rendition playlists + segments
    thumb.jpg             poster (~t=1s)
"""
import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path

from . import config, db

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[str]" = asyncio.Queue()

# Ladder: (height, video kbps, audio kbps). Only rungs whose height <= source
# height are produced, plus always at least one (the smallest that fits).
_LADDER = [
    (1080, 5000, 128),
    (720, 2800, 128),
    (480, 1400, 96),
]


def enqueue(video_id: str) -> None:
    _queue.put_nowait(video_id)


async def _run(*args: str) -> tuple[int, str]:
    """Run a subprocess, return (rc, combined_stderr_tail). No file bytes in RAM."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _out, err = await proc.communicate()
    return proc.returncode, (err.decode(errors="replace")[-4000:] if err else "")


async def _ffprobe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=width,height,codec_type",
        str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    sout, _serr = await proc.communicate()
    info = {"duration": None, "width": None, "height": None}
    try:
        data = json.loads(sout.decode())
        dur = data.get("format", {}).get("duration")
        info["duration"] = float(dur) if dur is not None else None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"] = int(s["width"]) if s.get("width") else None
                info["height"] = int(s["height"]) if s.get("height") else None
                break
    except Exception as e:
        logger.warning("ffprobe parse failed: %s", e)
    return info


async def _set_status(video_id: str, status: str, error: str | None = None,
                      **cols) -> None:
    sets = ["status=$2", "updated_at=now()"]
    vals = [video_id, status]
    i = 3
    if error is not None:
        sets.append(f"error=${i}")
        vals.append(error)
        i += 1
    for k, v in cols.items():
        sets.append(f"{k}=${i}")
        vals.append(v)
        i += 1
    await db.pool().execute(
        f"UPDATE mikevideo.videos SET {', '.join(sets)} WHERE id=$1", *vals)


async def _add_rendition(video_id: str, label: str, kind: str, path: Path) -> None:
    size = path.stat().st_size if path.exists() else 0
    await db.pool().execute(
        "INSERT INTO mikevideo.renditions(id, video_id, label, kind, path, bytes)"
        " VALUES($1,$2,$3,$4,$5,$6)",
        uuid.uuid4(), video_id, label, kind, str(path), size)


async def _encode_one(video_id: str) -> None:
    row = await db.pool().fetchrow(
        "SELECT id, user_id, upload_id, filename, content_type, status"
        " FROM mikevideo.videos WHERE id=$1", video_id)
    if row is None:
        logger.warning("encode: video %s gone", video_id)
        return
    user_id = row["user_id"]
    upload_id = row["upload_id"]

    src_blob = config.INGEST_DATA_ROOT / upload_id / "data.bin"
    if not src_blob.exists():
        await _set_status(video_id, "failed", error="ingest blob missing")
        logger.error("encode: blob missing for %s at %s", video_id, src_blob)
        return

    await _set_status(video_id, "encoding")
    out_dir = config.DATA_ROOT / user_id / str(video_id)
    hls_dir = out_dir / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)

    # Keep the original (move the blob out of the ingest temp dir).
    ext = Path(row["filename"] or "").suffix or ".mp4"
    original = out_dir / f"original{ext}"
    try:
        shutil.move(str(src_blob), str(original))
    except Exception as e:
        await _set_status(video_id, "failed", error=f"move failed: {e}")
        return
    await _add_rendition(video_id, "original", "mp4", original)

    # Probe.
    info = await _ffprobe(original)
    src_h = info.get("height") or 720

    # Progressive MP4 (faststart, H.264 + AAC).
    mp4 = out_dir / "video.mp4"
    rc, err = await _run(
        "ffmpeg", "-y", "-i", str(original),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(mp4))
    if rc != 0:
        await _set_status(video_id, "failed", error=f"mp4 encode rc={rc}: {err[-500:]}")
        logger.error("encode mp4 failed %s: %s", video_id, err[-500:])
        return
    await _add_rendition(video_id, "mp4", "mp4", mp4)

    # HLS ladder: rungs that fit the source, always at least the smallest.
    rungs = [r for r in _LADDER if r[0] <= src_h] or [_LADDER[-1]]
    variant_lines = []
    for (h, vkbps, akbps) in rungs:
        name = f"{h}p"
        rc, err = await _run(
            "ffmpeg", "-y", "-i", str(original),
            "-vf", f"scale=-2:{h}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-maxrate", f"{vkbps}k", "-bufsize", f"{vkbps*2}k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{akbps}k",
            "-hls_time", "4", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(hls_dir / f"{name}_%03d.ts"),
            str(hls_dir / f"{name}.m3u8"))
        if rc != 0:
            logger.warning("hls rung %s failed for %s: %s", name, video_id, err[-300:])
            continue
        await _add_rendition(video_id, name, "hls", hls_dir / f"{name}.m3u8")
        bw = (vkbps + akbps) * 1000
        # approximate width for a 16:9 source
        w = int(round(h * 16 / 9))
        variant_lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h}\n{name}.m3u8")

    if not variant_lines:
        await _set_status(video_id, "failed", error="no HLS rungs produced")
        return

    master = hls_dir / "master.m3u8"
    master.write_text("#EXTM3U\n#EXT-X-VERSION:3\n" + "\n".join(variant_lines) + "\n")
    await _add_rendition(video_id, "master", "hls", master)

    # Poster thumbnail (~t=1s, clamp to duration).
    thumb = out_dir / "thumb.jpg"
    dur = info.get("duration") or 0
    ts = "00:00:01" if dur > 1.2 else "00:00:00"
    rc, err = await _run(
        "ffmpeg", "-y", "-ss", ts, "-i", str(original),
        "-frames:v", "1", "-q:v", "3", str(thumb))
    if rc == 0 and thumb.exists():
        await _add_rendition(video_id, "thumb", "thumb", thumb)

    await _set_status(
        video_id, "ready",
        duration_sec=info.get("duration"),
        width=info.get("width"),
        height=info.get("height"))

    # Delete the ingest temp session dir (blob already moved out).
    try:
        sess = config.INGEST_DATA_ROOT / upload_id
        if sess.exists():
            shutil.rmtree(sess, ignore_errors=True)
    except Exception:
        pass
    logger.info("encode: video %s READY (%.1fs %sx%s)", video_id,
                info.get("duration") or 0, info.get("width"), info.get("height"))


async def worker_loop() -> None:
    logger.info("encode worker started")
    while True:
        video_id = await _queue.get()
        try:
            await _encode_one(video_id)
        except Exception as e:
            logger.exception("encode crashed for %s", video_id)
            try:
                await _set_status(video_id, "failed", error=str(e)[:500])
            except Exception:
                pass
        finally:
            _queue.task_done()


async def requeue_pending() -> None:
    """On startup, re-enqueue anything stuck in encoding (crash recovery)."""
    rows = await db.pool().fetch(
        "SELECT id FROM mikevideo.videos WHERE status='encoding'")
    for r in rows:
        enqueue(str(r["id"]))
    if rows:
        logger.info("requeued %d stuck encode(s)", len(rows))
