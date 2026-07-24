"""Encode worker — ffmpeg turns a source blob into MP4 + HLS + thumb + metadata.

MEMORY: ffmpeg streams from/to file paths; we NEVER read a whole video into RAM.
We only pass paths on the command line. Ingest already verified the whole-file
sha256, so there is no Python-side hash pass here either.

The pipeline is ROTATION- AND ASPECT-CORRECT (this is what fixed the "video plays
sideways / only the upper part shows" bug):
  * ffprobe reports CODED dimensions and a rotation matrix separately. A phone
    portrait clip is coded 1920x1080 with `rotation: -90` — it must be *displayed*
    1080x1920. We compute the display geometry and drive the whole ladder off it.
  * ffmpeg (7.x) auto-applies the display matrix, so the decoded frames the filter
    graph sees are already upright. We scale off the DISPLAY short-side, force
    square pixels (setsar=1), never upscale, and write each HLS variant's
    RESOLUTION= from the ACTUAL output geometry — no more "assume 16:9".
  * We strip the rotate tag on every output so no downstream player double-rotates.

Layout produced under {DATA_ROOT}/{user_id}/{video_id}/:
    original.<ext>        the kept source (moved from ingest data.bin, or reused)
    video.mp4             H.264 faststart progressive MP4 (download / fallback)
    hls/master.m3u8       HLS master playlist (variant ladder)
    hls/<p>p.m3u8 + *.ts  per-rendition playlists + segments
    thumb.jpg             poster (~t=1s), correctly oriented
"""
import asyncio
import json
import logging
import math
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[str]" = asyncio.Queue()

# Ladder rungs keyed by the DISPLAY short-side (the classic "p" number: a portrait
# 1080x1920 clip and a landscape 1920x1080 clip are both "1080p"). Only rungs whose
# height <= source short-side are produced (we NEVER upscale); if the source is
# smaller than the smallest rung we emit a single native-resolution rung.
#   short_side -> (video kbps, audio kbps)
_RUNGS = [
    (1080, 5000, 128),
    (720, 2800, 128),
    (480, 1400, 96),
    (360, 800, 96),
]


def enqueue(video_id: str) -> None:
    _queue.put_nowait(video_id)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
@dataclass
class Analysis:
    """Everything we learn about a source, ready to persist and to drive encode."""
    duration: float | None = None
    # Coded (as-stored) pixel size.
    width: int | None = None
    height: int | None = None
    # Display (post-rotation) size — what a player must actually show.
    display_width: int | None = None
    display_height: int | None = None
    rotation: int = 0                      # 0|90|180|270, normalised
    orientation: str | None = None         # portrait | landscape | square
    aspect_ratio: str | None = None        # reduced display AR, e.g. "9:16"
    video_codec: str | None = None
    audio_codec: str | None = None
    pix_fmt: str | None = None
    fps: float | None = None
    video_bitrate: int | None = None
    audio_bitrate: int | None = None
    overall_bitrate: int | None = None
    has_audio: bool = False
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    is_hdr: bool = False
    probe: dict = field(default_factory=dict)

    @property
    def short_side(self) -> int:
        w = self.display_width or self.width or 0
        h = self.display_height or self.height or 0
        return min(w, h) if (w and h) else (w or h or 0)

    def db_cols(self) -> dict:
        """The subset persisted onto mikevideo.videos (column -> value)."""
        return {
            "duration_sec": self.duration,
            "width": self.width,
            "height": self.height,
            "display_width": self.display_width,
            "display_height": self.display_height,
            "rotation": self.rotation,
            "orientation": self.orientation,
            "aspect_ratio": self.aspect_ratio,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "pix_fmt": self.pix_fmt,
            "fps": self.fps,
            "video_bitrate": self.video_bitrate,
            "audio_bitrate": self.audio_bitrate,
            "overall_bitrate": self.overall_bitrate,
            "has_audio": self.has_audio,
            "audio_channels": self.audio_channels,
            "audio_sample_rate": self.audio_sample_rate,
            "color_primaries": self.color_primaries,
            "color_transfer": self.color_transfer,
            "color_space": self.color_space,
            "is_hdr": self.is_hdr,
            "probe": self.probe,
        }


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_rate(v) -> float | None:
    """Parse an ffprobe rate string like '289/12' or '30' -> float, or None."""
    if not v or v in ("0/0",):
        return None
    try:
        if isinstance(v, str) and "/" in v:
            num, den = v.split("/", 1)
            den = float(den)
            return (float(num) / den) if den else None
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _stream_rotation(vs: dict) -> int:
    """Normalised clockwise display rotation (0/90/180/270) for a video stream.

    Reads the modern Display Matrix side data (`rotation`, typically -90/90/180)
    AND the legacy container tag `tags.rotate`. ffprobe reports the matrix rotation
    as the angle applied to reach the coded frame; the on-screen rotation is its
    negation. We fold everything into 0..359 clockwise."""
    raw = None
    for sd in vs.get("side_data_list", []) or []:
        if sd.get("side_data_type") == "Display Matrix" and sd.get("rotation") is not None:
            # Matrix `rotation` (e.g. -90) → display rotation is the negation.
            raw = -float(sd["rotation"])
            break
    if raw is None:
        tag = (vs.get("tags") or {}).get("rotate")
        if tag is not None:
            try:
                raw = float(tag)
            except ValueError:
                raw = None
    if raw is None:
        return 0
    return int(round(raw / 90.0)) * 90 % 360


def _reduce_ratio(w: int, h: int) -> str | None:
    if not w or not h:
        return None
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


async def analyze(path: Path) -> Analysis:
    """Full ffprobe of a source file into an Analysis. Never reads frame bytes into
    our RAM — ffprobe streams the container itself and prints JSON."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    sout, serr = await proc.communicate()
    a = Analysis()
    if proc.returncode != 0:
        logger.warning("ffprobe rc=%s for %s: %s", proc.returncode, path,
                       (serr.decode(errors="replace")[-300:] if serr else ""))
        return a
    try:
        data = json.loads(sout.decode())
    except Exception as e:
        logger.warning("ffprobe parse failed for %s: %s", path, e)
        return a
    a.probe = data

    fmt = data.get("format", {}) or {}
    a.duration = _parse_rate(fmt.get("duration"))
    a.overall_bitrate = _to_int(fmt.get("bit_rate"))

    vs = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
               and not (s.get("disposition") or {}).get("attached_pic")), None)
    as_ = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    if vs is not None:
        a.width = _to_int(vs.get("width"))
        a.height = _to_int(vs.get("height"))
        a.video_codec = vs.get("codec_name")
        a.pix_fmt = vs.get("pix_fmt")
        a.video_bitrate = _to_int(vs.get("bit_rate"))
        a.color_primaries = vs.get("color_primaries")
        a.color_transfer = vs.get("color_transfer")
        a.color_space = vs.get("color_space")
        a.is_hdr = (a.color_transfer in ("smpte2084", "arib-std-b67")) \
            or (a.color_primaries == "bt2020")
        a.fps = _parse_rate(vs.get("avg_frame_rate")) or _parse_rate(vs.get("r_frame_rate"))
        a.rotation = _stream_rotation(vs)
        # Display geometry: swap on quarter-turns.
        if a.width and a.height:
            if a.rotation in (90, 270):
                a.display_width, a.display_height = a.height, a.width
            else:
                a.display_width, a.display_height = a.width, a.height
            a.aspect_ratio = _reduce_ratio(a.display_width, a.display_height)
            if a.display_width > a.display_height:
                a.orientation = "landscape"
            elif a.display_width < a.display_height:
                a.orientation = "portrait"
            else:
                a.orientation = "square"

    if as_ is not None:
        a.has_audio = True
        a.audio_codec = as_.get("codec_name")
        a.audio_bitrate = _to_int(as_.get("bit_rate"))
        a.audio_channels = _to_int(as_.get("channels"))
        a.audio_sample_rate = _to_int(as_.get("sample_rate"))

    return a


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
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


async def _reset_renditions(video_id: str) -> None:
    """Clear rendition rows so a (re)encode never accumulates duplicates."""
    await db.pool().execute(
        "DELETE FROM mikevideo.renditions WHERE video_id=$1", video_id)


async def _add_rendition(video_id: str, label: str, kind: str, path: Path,
                         width: int | None = None, height: int | None = None) -> None:
    size = path.stat().st_size if path.exists() else 0
    await db.pool().execute(
        "INSERT INTO mikevideo.renditions(id, video_id, label, kind, path, bytes)"
        " VALUES($1,$2,$3,$4,$5,$6)",
        uuid.uuid4(), video_id, label, kind, str(path), size)


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
async def _run(*args: str) -> tuple[int, str]:
    """Run a subprocess, return (rc, combined_stderr_tail). No file bytes in RAM."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _out, err = await proc.communicate()
    return proc.returncode, (err.decode(errors="replace")[-4000:] if err else "")


def _even(x: float) -> int:
    """Nearest even int >= 2 (H.264 needs even dimensions)."""
    return max(2, int(round(x / 2.0)) * 2)


def _rung_dims(a: Analysis, short: int) -> tuple[int, int]:
    """Output (w,h) for a rung whose short-side is `short`, preserving the display
    aspect ratio with square pixels and even dimensions."""
    dw = a.display_width or a.width or short
    dh = a.display_height or a.height or short
    s = _even(short)
    if dw >= dh:                      # landscape / square: short side is height
        return _even(dw * short / dh), s
    return s, _even(dh * short / dw)  # portrait: short side is width


def _plan_rungs(a: Analysis) -> list[tuple[int, int, int, int, int]]:
    """List of (short, out_w, out_h, vkbps, akbps). Never upscales; if the source
    is below the smallest rung, emits one native rung."""
    short = a.short_side or 720
    chosen = [(s, vk, ak) for (s, vk, ak) in _RUNGS if s <= short]
    if not chosen:
        # Source smaller than the smallest rung → single native-res rung.
        smallest = min(_RUNGS, key=lambda r: r[0])
        chosen = [(short, smallest[1], smallest[2])]
    out = []
    for (s, vk, ak) in chosen:
        w, h = _rung_dims(a, s)
        out.append((s, w, h, vk, ak))
    return out


def _audio_args(a: Analysis, akbps: int) -> list[str]:
    return ["-c:a", "aac", "-b:a", f"{akbps}k"] if a.has_audio else ["-an"]


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------
def _find_original(out_dir: Path) -> Path | None:
    if out_dir.exists():
        for p in sorted(out_dir.glob("original.*")):
            if p.is_file():
                return p
    return None


async def _encode_one(video_id: str) -> None:
    row = await db.pool().fetchrow(
        "SELECT id, user_id, upload_id, filename, content_type, status"
        " FROM mikevideo.videos WHERE id=$1", video_id)
    if row is None:
        logger.warning("encode: video %s gone", video_id)
        return
    user_id = row["user_id"]
    upload_id = row["upload_id"]

    out_dir = config.DATA_ROOT / user_id / str(video_id)
    hls_dir = out_dir / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the source: a fresh ingest blob (move it in) or an existing original
    # (re-encode/backfill). Either way we keep exactly one original.<ext>.
    src_blob = config.INGEST_DATA_ROOT / upload_id / "data.bin"
    original = _find_original(out_dir)
    if src_blob.exists():
        ext = Path(row["filename"] or "").suffix or ".mp4"
        original = out_dir / f"original{ext}"
        try:
            shutil.move(str(src_blob), str(original))
        except Exception as e:
            await _set_status(video_id, "failed", error=f"move failed: {e}")
            return
    elif original is None:
        await _set_status(video_id, "failed", error="no source (ingest blob + original both missing)")
        logger.error("encode: no source for %s", video_id)
        return

    await _set_status(video_id, "encoding", error=None)
    await _reset_renditions(video_id)

    # Analyse first — the ladder and RESOLUTION lines are computed from this.
    a = await analyze(original)
    await _add_rendition(video_id, "original", "mp4", original)

    common_v = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-metadata:s:v:0", "rotate=0"]

    # Progressive MP4 (faststart) — full display resolution, upright, square pixels.
    mp4 = out_dir / "video.mp4"
    rc, err = await _run(
        "ffmpeg", "-y", "-i", str(original),
        "-vf", "setsar=1", *common_v, *_audio_args(a, 128),
        "-movflags", "+faststart", str(mp4))
    if rc != 0:
        await _set_status(video_id, "failed", error=f"mp4 encode rc={rc}: {err[-500:]}")
        logger.error("encode mp4 failed %s: %s", video_id, err[-500:])
        return
    await _add_rendition(video_id, "mp4", "mp4", mp4,
                         a.display_width, a.display_height)

    # HLS ladder — each rung scaled to exact even display geometry, square pixels,
    # and the master RESOLUTION written from the ACTUAL output dims.
    variant_lines = []
    for (short, w, h, vkbps, akbps) in _plan_rungs(a):
        name = f"{short}p"
        rc, err = await _run(
            "ffmpeg", "-y", "-i", str(original),
            "-vf", f"scale={w}:{h}:flags=lanczos,setsar=1",
            *common_v,
            "-maxrate", f"{vkbps}k", "-bufsize", f"{vkbps*2}k",
            *_audio_args(a, akbps),
            "-hls_time", "4", "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(hls_dir / f"{name}_%03d.ts"),
            str(hls_dir / f"{name}.m3u8"))
        if rc != 0:
            logger.warning("hls rung %s failed for %s: %s", name, video_id, err[-300:])
            continue
        await _add_rendition(video_id, name, "hls", hls_dir / f"{name}.m3u8", w, h)
        bw = (vkbps + akbps) * 1000
        codecs = 'avc1.640028,mp4a.40.2' if a.has_audio else 'avc1.640028'
        variant_lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},CODECS="{codecs}"\n{name}.m3u8')

    if not variant_lines:
        await _set_status(video_id, "failed", error="no HLS rungs produced")
        return

    master = hls_dir / "master.m3u8"
    master.write_text("#EXTM3U\n#EXT-X-VERSION:3\n" + "\n".join(variant_lines) + "\n")
    await _add_rendition(video_id, "master", "hls", master,
                         a.display_width, a.display_height)

    # Poster thumbnail (~t=1s, clamped) — autorotated, square pixels.
    thumb = out_dir / "thumb.jpg"
    dur = a.duration or 0
    ts = "00:00:01" if dur > 1.2 else "00:00:00"
    rc, err = await _run(
        "ffmpeg", "-y", "-ss", ts, "-i", str(original),
        "-vf", "setsar=1", "-frames:v", "1", "-q:v", "3", str(thumb))
    if rc == 0 and thumb.exists():
        await _add_rendition(video_id, "thumb", "thumb", thumb,
                             a.display_width, a.display_height)

    await _set_status(video_id, "ready", error=None, **a.db_cols())

    # Delete the ingest temp session dir (blob already moved out). Safe no-op on re-encode.
    try:
        sess = config.INGEST_DATA_ROOT / upload_id
        if sess.exists():
            shutil.rmtree(sess, ignore_errors=True)
    except Exception:
        pass
    logger.info("encode: video %s READY (%.1fs %sx%s disp=%sx%s rot=%s %s)",
                video_id, a.duration or 0, a.width, a.height,
                a.display_width, a.display_height, a.rotation, a.orientation)


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


async def backfill_metadata() -> None:
    """One-time (idempotent) pass over already-encoded videos.

    For every ready video missing the new analysis (display_width IS NULL):
      * re-probe its kept original.* and fill all metadata columns;
      * if it was rotated or is not 16:9, its old HLS advertises the wrong
        RESOLUTION (the "upper part only" bug) → re-enqueue a full re-encode.
    Idempotent: once display_width is set a row is never revisited, so a restart
    won't redo the work. Runs in the background so startup isn't blocked; the
    single encode worker serialises any re-encodes so the box CPU isn't thrashed.
    """
    rows = await db.pool().fetch(
        "SELECT id, user_id, status FROM mikevideo.videos"
        " WHERE status='ready' AND display_width IS NULL")
    if not rows:
        return
    logger.info("backfill: %d ready video(s) need analysis", len(rows))
    reencoded = 0
    filled = 0
    for r in rows:
        video_id = str(r["id"])
        out_dir = config.DATA_ROOT / r["user_id"] / video_id
        original = _find_original(out_dir)
        if original is None:
            logger.warning("backfill: original missing for %s (%s)", video_id, out_dir)
            continue
        try:
            a = await analyze(original)
        except Exception as e:
            logger.warning("backfill: analyze failed for %s: %s", video_id, e)
            continue
        needs_reencode = a.rotation != 0 or a.aspect_ratio != "16:9"
        if needs_reencode:
            # Flip to encoding + enqueue; the encode fills metadata itself and
            # rewrites the HLS with correct RESOLUTION.
            await _set_status(video_id, "encoding", error=None)
            enqueue(video_id)
            reencoded += 1
        else:
            # HLS is already correct (true 16:9, no rotation) → just record metadata.
            await _set_status(video_id, "ready", error=None, **a.db_cols())
            filled += 1
    logger.info("backfill: metadata-only=%d, re-encode queued=%d", filled, reencoded)
