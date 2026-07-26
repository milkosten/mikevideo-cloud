"""mikevideo-cloud — the MikeVideo control plane + browser experience.

FastAPI + asyncpg + Postgres + ffmpeg. Auth: X-API-KEY -> user_id (mikeoscomputers).
Mints HMAC upload tickets ingest trusts; ingest calls back on completion; an
ffmpeg worker produces MP4 + HLS + thumbnail.

Guard rails: ISO-8601 timestamps, parameterized SQL, never-trust-200,
never load a whole file into RAM (ffmpeg streams; media served with byte-range).
"""
import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response, StreamingResponse

from . import config, db, encode, transcribe, enrich
from .identity import authenticate
from .tickets import mint_ticket, verify_callback_signature

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mikevideo-cloud")

app = FastAPI(title="mikevideo-cloud")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# OAuth Authorization Server (account.osmike.com) — the web PKCE token exchange
# is proxied through here so the browser hits one origin (no CORS on the AS).
OAUTH_ISSUER = os.environ.get("ACCOUNT_OSMIKE_ISSUER", "https://account.osmike.com").rstrip("/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(dt)


def _jsonb(v):
    """jsonb column → Python object. The pool registers a jsonb codec so this is
    usually already parsed; tolerate a raw string just in case."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def _parse_ts(v):
    """Tolerant timestamp parse -> aware datetime or None. Accepts ISO-8601
    strings and epoch millis/seconds."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            secs = v / 1000.0 if v > 1e11 else float(v)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        s = str(v).strip()
        if s.isdigit():
            n = int(s)
            secs = n / 1000.0 if n > 1e11 else float(n)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _auth(request: Request, scope: str | None = None) -> str | None:
    # Dual-auth: OAuth Bearer JWT (via JWKS) preferred, legacy X-API-KEY fallback.
    return await authenticate(request, scope)


async def _user_usage(user_id: str) -> int:
    val = await db.pool().fetchval(
        "SELECT COALESCE(SUM(bytes),0) FROM mikevideo.videos"
        " WHERE user_id=$1 AND status<>'failed'", user_id)
    return int(val or 0)


def _thumb_url(user_id: str, video_id: str) -> str:
    return f"/media/{user_id}/{video_id}/thumb.jpg"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    await db.connect()
    await db.migrate()
    asyncio.create_task(encode.worker_loop())
    await encode.requeue_pending()
    # Backfill analysis for pre-existing videos in the background (idempotent;
    # re-encodes only the rotated / non-16:9 ones). Never blocks startup.
    asyncio.create_task(encode.backfill_metadata())
    # Voice→text runs in its own background worker (never blocks encoding).
    asyncio.create_task(transcribe.worker_loop())
    asyncio.create_task(transcribe.requeue_and_backfill())
    # AI enrichment (Phase C) — own worker + a retry sweep for the flaky GPU.
    asyncio.create_task(enrich.worker_loop())
    asyncio.create_task(enrich.retry_loop())
    logger.info("mikevideo-cloud started")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    ok_db = False
    try:
        await db.pool().fetchval("SELECT 1")
        ok_db = True
    except Exception as e:
        logger.warning("health db check failed: %s", e)
    return {"status": "ok" if ok_db else "degraded", "service": "mikevideo-cloud",
            "db": ok_db}


# ---------------------------------------------------------------------------
# "Sign in with MikeOS" — Authorization Code + PKCE (ecosystem/AUTHENTICATION.md
# ROLE 3). The browser does the /oauth/authorize redirect itself; only the code
# (and refresh) exchange is proxied here so it isn't blocked by CORS on the AS.
# The client is public (PKCE, no secret), so there's nothing sensitive to hold.
# ---------------------------------------------------------------------------
@app.post("/api/oauth/token")
async def oauth_token(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    # Only forward the standard OAuth token-exchange fields (public client, PKCE).
    allowed = ("grant_type", "code", "code_verifier", "client_id", "redirect_uri",
               "refresh_token", "scope")
    payload = {k: body[k] for k in allowed if k in body}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{OAUTH_ISSUER}/oauth/token", json=payload)
        data = r.json()
    except Exception as e:
        logger.warning("oauth token exchange failed: %s", e)
        return JSONResponse({"error": "auth server unavailable"}, status_code=502)
    return JSONResponse(data, status_code=r.status_code)


# ---------------------------------------------------------------------------
# GET /api/public/videos — the PUBLIC gallery (no auth). Every ready video the
# fleet's users have made public. This is the anonymous "home feed".
# ---------------------------------------------------------------------------
@app.get("/api/public/videos")
async def public_feed():
    rows = await db.pool().fetch(
        "SELECT id, user_id, filename, ai_title, duration_sec, display_width,"
        " display_height, orientation, spoken_language, transcript_status,"
        " created_at, published_at FROM mikevideo.videos"
        " WHERE visibility='public' AND status='ready'"
        " ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 200")
    out = []
    for r in rows:
        vid = str(r["id"])
        out.append({
            "id": vid,
            "status": "ready",
            "ai_title": r["ai_title"],
            "filename": r["filename"],
            "duration_sec": r["duration_sec"],
            "display_width": r["display_width"],
            "display_height": r["display_height"],
            "orientation": r["orientation"],
            "spoken_language": r["spoken_language"],
            "transcript_status": r["transcript_status"],
            "visibility": "public",
            "thumb_url": _thumb_url(r["user_id"], vid),
            "created_at": _iso(r["created_at"]),
            "published_at": _iso(r["published_at"]),
        })
    return {"videos": out}


# ---------------------------------------------------------------------------
# POST /api/videos — auth + quota -> create row -> mint ticket
# ---------------------------------------------------------------------------
@app.post("/api/videos")
async def create_video(request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    filename = str(body.get("filename") or "video.mp4")
    content_type = str(body.get("content_type") or "video/mp4")
    try:
        total_size = int(body.get("total_size"))
        chunk_size = int(body.get("chunk_size"))
        count = int(body.get("count"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "total_size, chunk_size, count required (ints)"},
                            status_code=400)
    file_hash = str(body.get("file_hash") or "")
    if not file_hash:
        return JSONResponse({"error": "file_hash required (sha256)"}, status_code=400)
    if total_size <= 0 or total_size > config.MAX_UPLOAD_BYTES:
        return JSONResponse({"error": f"total_size out of range (max {config.MAX_UPLOAD_BYTES})"},
                            status_code=400)

    # Quota check BEFORE minting a ticket.
    used = await _user_usage(user_id)
    if used + total_size > config.USER_QUOTA_BYTES:
        return JSONResponse(
            {"error": "quota exceeded", "used": used, "quota": config.USER_QUOTA_BYTES,
             "requested": total_size}, status_code=507)

    device_id = str(body.get("device_id") or "") or None
    taken_at = _parse_ts(body.get("taken_at"))

    video_id = uuid.uuid4()
    upload_id = uuid.uuid4().hex  # filesystem-safe (hex) for ingest

    await db.pool().execute(
        "INSERT INTO mikevideo.videos"
        " (id, user_id, device_id, upload_id, filename, content_type, bytes,"
        "  sha256, status, taken_at)"
        " VALUES($1,$2,$3,$4,$5,$6,$7,$8,'uploading',$9)",
        video_id, user_id, device_id, upload_id, filename, content_type,
        total_size, file_hash, taken_at)

    ticket = mint_ticket(upload_id=upload_id, user_id=user_id,
                         max_size=total_size, file_hash=file_hash)

    return {
        "video_id": str(video_id),
        "upload_id": upload_id,
        "ingest_url": config.INGEST_URL,
        "ticket": ticket,
        "chunk_size": chunk_size,
    }


# ---------------------------------------------------------------------------
# POST /api/internal/complete — ingest callback (CALLBACK_SECRET) -> enqueue
# ---------------------------------------------------------------------------
@app.post("/api/internal/complete")
async def internal_complete(request: Request,
                            x_ingest_signature: str = Header(default="")):
    raw = await request.body()
    if not verify_callback_signature(raw, x_ingest_signature):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    upload_id = str(payload.get("upload_id") or "")
    if not upload_id:
        return JSONResponse({"error": "upload_id required"}, status_code=400)

    row = await db.pool().fetchrow(
        "SELECT id, status FROM mikevideo.videos WHERE upload_id=$1", upload_id)
    if row is None:
        return JSONResponse({"error": "unknown upload_id"}, status_code=404)

    video_id = str(row["id"])
    # never-trust-200: only enqueue if we actually flipped a row to encoding.
    updated = await db.pool().fetchval(
        "UPDATE mikevideo.videos SET status='encoding', sha256=COALESCE($2,sha256),"
        " updated_at=now() WHERE id=$1 AND status IN ('uploading','failed')"
        " RETURNING id",
        row["id"], str(payload.get("file_hash") or "") or None)
    if updated is None and row["status"] not in ("encoding", "ready"):
        return JSONResponse({"error": "could not transition"}, status_code=409)
    if row["status"] not in ("encoding", "ready"):
        encode.enqueue(video_id)
    return {"ok": True, "video_id": video_id, "enqueued": row["status"] not in ("encoding", "ready")}


# ---------------------------------------------------------------------------
# GET /api/videos — the user's library
# ---------------------------------------------------------------------------
@app.get("/api/videos")
async def list_videos(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = await db.pool().fetch(
        "SELECT id, filename, status, duration_sec, width, height,"
        " display_width, display_height, orientation, aspect_ratio, rotation,"
        " fps, has_audio, spoken_language, transcript_status, ai_title,"
        " enrich_status, visibility, bytes, taken_at, created_at FROM mikevideo.videos"
        " WHERE user_id=$1 ORDER BY created_at DESC LIMIT 500", user_id)
    out = []
    for r in rows:
        vid = str(r["id"])
        out.append({
            "id": vid,
            "filename": r["filename"],
            "status": r["status"],
            "duration_sec": r["duration_sec"],
            "width": r["width"],
            "height": r["height"],
            "display_width": r["display_width"],
            "display_height": r["display_height"],
            "orientation": r["orientation"],
            "aspect_ratio": r["aspect_ratio"],
            "rotation": r["rotation"],
            "fps": r["fps"],
            "has_audio": r["has_audio"],
            "spoken_language": r["spoken_language"],
            "transcript_status": r["transcript_status"],
            "ai_title": r["ai_title"],
            "enrich_status": r["enrich_status"],
            "visibility": r["visibility"],
            "bytes": r["bytes"],
            "thumb_url": _thumb_url(user_id, vid) if r["status"] == "ready" else None,
            "taken_at": _iso(r["taken_at"]),
            "created_at": _iso(r["created_at"]),
        })
    return {"videos": out}


# Column list shared by the authed + public detail queries.
_DETAIL_COLS = (
    "id, user_id, filename, content_type, status, error, duration_sec, width,"
    " height, display_width, display_height, orientation, aspect_ratio,"
    " rotation, video_codec, audio_codec, pix_fmt, fps, video_bitrate,"
    " audio_bitrate, overall_bitrate, has_audio, audio_channels,"
    " audio_sample_rate, color_primaries, color_transfer, color_space,"
    " is_hdr, spoken_language, language_confidence, transcript_status,"
    " has_speech, ai_title, ai_summary, ai_tags, ai_chapters, enrich_status,"
    " visibility, bytes, taken_at, created_at, published_at"
)


def _detail_payload(r, is_owner: bool) -> dict:
    """Build the /api/videos/{id} response from a row. `owner` gets the private
    controls (visibility, filename); a public viewer gets a lean, safe subset."""
    owner_user_id = r["user_id"]
    video_id = str(r["id"])
    base = f"/media/{owner_user_id}/{video_id}"
    ready = r["status"] == "ready"
    captions_ready = r["transcript_status"] == "ready"
    payload = {
        "id": video_id,
        "status": r["status"],
        "duration_sec": r["duration_sec"],
        "width": r["width"],
        "height": r["height"],
        "display_width": r["display_width"],
        "display_height": r["display_height"],
        "orientation": r["orientation"],
        "aspect_ratio": r["aspect_ratio"],
        "rotation": r["rotation"],
        "video_codec": r["video_codec"],
        "audio_codec": r["audio_codec"],
        "fps": r["fps"],
        "has_audio": r["has_audio"],
        "is_hdr": r["is_hdr"],
        "spoken_language": r["spoken_language"],
        "transcript_status": r["transcript_status"],
        "has_speech": r["has_speech"],
        "ai_title": r["ai_title"],
        "ai_summary": r["ai_summary"],
        "ai_tags": list(r["ai_tags"]) if r["ai_tags"] else None,
        "ai_chapters": _jsonb(r["ai_chapters"]),
        "visibility": r["visibility"],
        "bytes": r["bytes"],
        "duration": r["duration_sec"],
        "hls_url": f"{base}/hls/master.m3u8" if ready else None,
        "mp4_url": f"{base}/video.mp4" if ready else None,
        "thumb_url": f"{base}/thumb.jpg" if ready else None,
        "captions_url": f"{base}/captions/{r['spoken_language'] or 'und'}.vtt" if captions_ready else None,
        "taken_at": _iso(r["taken_at"]),
        "created_at": _iso(r["created_at"]),
        "published_at": _iso(r["published_at"]),
    }
    if is_owner:
        payload.update({
            "filename": r["filename"],
            "content_type": r["content_type"],
            "error": r["error"],
            "pix_fmt": r["pix_fmt"],
            "video_bitrate": r["video_bitrate"],
            "audio_bitrate": r["audio_bitrate"],
            "overall_bitrate": r["overall_bitrate"],
            "audio_channels": r["audio_channels"],
            "audio_sample_rate": r["audio_sample_rate"],
            "color_primaries": r["color_primaries"],
            "color_transfer": r["color_transfer"],
            "color_space": r["color_space"],
            "language_confidence": r["language_confidence"],
            "enrich_status": r["enrich_status"],
            "is_owner": True,
        })
    return payload


# ---------------------------------------------------------------------------
# GET /api/videos/{id} — owner detail (auth) + HLS master url + mp4 + thumb
# ---------------------------------------------------------------------------
@app.get("/api/videos/{video_id}")
async def get_video(video_id: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return JSONResponse({"error": "bad id"}, status_code=400)
    r = await db.pool().fetchrow(
        f"SELECT {_DETAIL_COLS} FROM mikevideo.videos WHERE id=$1 AND user_id=$2",
        vid, user_id)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _detail_payload(r, is_owner=True)


# ---------------------------------------------------------------------------
# GET /api/public/videos/{id} — NO AUTH. Only returns PUBLIC videos (shared links).
# Private videos 404 here regardless of who asks.
# ---------------------------------------------------------------------------
@app.get("/api/public/videos/{video_id}")
async def get_public_video(video_id: str):
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return JSONResponse({"error": "bad id"}, status_code=400)
    r = await db.pool().fetchrow(
        f"SELECT {_DETAIL_COLS} FROM mikevideo.videos"
        " WHERE id=$1 AND visibility='public' AND status='ready'", vid)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _detail_payload(r, is_owner=False)


# ---------------------------------------------------------------------------
# POST /api/videos/{id}/visibility {visibility: private|public} — owner only
# ---------------------------------------------------------------------------
@app.post("/api/videos/{video_id}/visibility")
async def set_visibility(video_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    vis = str(body.get("visibility") or "").lower()
    if vis not in ("private", "public"):
        return JSONResponse({"error": "visibility must be private|public"}, status_code=400)
    # never-trust-200: only flips a row the caller actually owns.
    row = await db.pool().fetchrow(
        "UPDATE mikevideo.videos SET visibility=$3,"
        " published_at=CASE WHEN $3='public' AND published_at IS NULL THEN now() ELSE published_at END,"
        " updated_at=now() WHERE id=$1 AND user_id=$2 RETURNING id, visibility, published_at",
        vid, user_id, vis)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": str(row["id"]), "visibility": row["visibility"],
            "published_at": _iso(row["published_at"])}


# ---------------------------------------------------------------------------
# GET /api/videos/{id}/transcript — the full transcript (segments + text)
# ---------------------------------------------------------------------------
@app.get("/api/videos/{video_id}/transcript")
async def get_transcript(video_id: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return JSONResponse({"error": "bad id"}, status_code=400)
    # Scope to the owner via the videos row, then read the transcript.
    r = await db.pool().fetchrow(
        "SELECT t.language, t.plain_text, t.segments, t.word_count, t.model, t.duration_sec"
        " FROM mikevideo.transcripts t JOIN mikevideo.videos v ON v.id=t.video_id"
        " WHERE t.video_id=$1 AND v.user_id=$2", vid, user_id)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    segments = r["segments"]
    if isinstance(segments, str):
        segments = json.loads(segments)
    return {
        "video_id": video_id,
        "language": r["language"],
        "text": r["plain_text"],
        "word_count": r["word_count"],
        "model": r["model"],
        "duration_sec": r["duration_sec"],
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# GET /api/search?q=... — full-text search over the user's transcripts.
# Returns matching videos + the best matching segment (with a timestamp) so the
# client can deep-link straight to the spoken moment.
# ---------------------------------------------------------------------------
def _best_segment(segments, terms: list[str]) -> dict | None:
    """First segment whose text contains any query term (case-insensitive)."""
    if not segments:
        return None
    for seg in segments:
        low = (seg.get("text") or "").lower()
        if any(t in low for t in terms):
            return {"start": float(seg.get("start") or 0), "text": (seg.get("text") or "").strip()}
    first = segments[0]
    return {"start": float(first.get("start") or 0), "text": (first.get("text") or "").strip()}


@app.get("/api/search")
async def search(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return {"query": "", "results": []}
    rows = await db.pool().fetch(
        "SELECT v.id, v.filename, v.spoken_language, v.ai_title, v.duration_sec,"
        "       t.plain_text, t.segments,"
        "       ts_rank(t.search_tsv, websearch_to_tsquery('simple', $2)) AS rank"
        " FROM mikevideo.transcripts t JOIN mikevideo.videos v ON v.id=t.video_id"
        " WHERE v.user_id=$1 AND t.search_tsv @@ websearch_to_tsquery('simple', $2)"
        " ORDER BY rank DESC LIMIT 50", user_id, q)
    terms = [w.lower() for w in re.findall(r"\w+", q, flags=re.UNICODE) if len(w) > 1]
    results = []
    for r in rows:
        vid = str(r["id"])
        segments = _jsonb(r["segments"]) or []
        results.append({
            "id": vid,
            "title": r["ai_title"] or r["filename"],
            "language": r["spoken_language"],
            "duration_sec": r["duration_sec"],
            "thumb_url": _thumb_url(user_id, vid),
            "match": _best_segment(segments, terms),
            "rank": float(r["rank"]) if r["rank"] is not None else 0.0,
        })
    return {"query": q, "results": results}


# ---------------------------------------------------------------------------
# Media serving with byte-range. v1 auth model: paths are gated on the
# unguessable video_id (a v4 uuid). We validate the {user_id}/{video_id}
# pair exists and the path stays inside its dir (no traversal). This keeps
# HLS/MP4 fetchable by <video>/hls.js without per-segment API keys.
# ---------------------------------------------------------------------------
_MEDIA_CACHE: dict[str, bool] = {}


async def _media_allowed(user_id: str, video_id: str) -> bool:
    key = f"{user_id}/{video_id}"
    if key in _MEDIA_CACHE:
        return _MEDIA_CACHE[key]
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return False
    row = await db.pool().fetchrow(
        "SELECT 1 FROM mikevideo.videos WHERE id=$1 AND user_id=$2", vid, user_id)
    ok = row is not None
    if ok:
        _MEDIA_CACHE[key] = True
    return ok


def _range_response(path: Path, request: Request) -> Response:
    file_size = path.stat().st_size
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix == ".m3u8":
        ctype = "application/vnd.apple.mpegurl"
    elif path.suffix == ".ts":
        ctype = "video/mp2t"
    elif path.suffix == ".vtt":
        ctype = "text/vtt"
    range_header = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"}
    if not range_header:
        return FileResponse(str(path), media_type=ctype, headers=headers)

    try:
        unit, rng = range_header.split("=", 1)
        start_s, end_s = rng.split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
    except Exception:
        return FileResponse(str(path), media_type=ctype, headers=headers)
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{file_size}"})
    length = end - start + 1

    def _iter():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                buf = f.read(min(1024 * 1024, remaining))
                if not buf:
                    break
                remaining -= len(buf)
                yield buf

    headers.update({
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    })
    return StreamingResponse(_iter(), status_code=206, media_type=ctype,
                             headers=headers)


@app.get("/media/{user_id}/{video_id}/{path:path}")
async def serve_media(user_id: str, video_id: str, path: str, request: Request):
    if not await _media_allowed(user_id, video_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    base = (config.DATA_ROOT / user_id / video_id).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base) + os.sep):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return _range_response(target, request)


# ---------------------------------------------------------------------------
# Browser web app
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    f = WEB_DIR / "index.html"
    return HTMLResponse(f.read_text())


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback():
    # OAuth redirect target — the SPA reads ?code&state and exchanges it.
    f = WEB_DIR / "index.html"
    return HTMLResponse(f.read_text())


@app.get("/hls.js")
async def hlsjs():
    f = WEB_DIR / "hls.min.js"
    return Response(f.read_text(), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"})
