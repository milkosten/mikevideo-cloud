"""mikevideo-cloud — the MikeVideo control plane + browser experience.

FastAPI + asyncpg + Postgres + ffmpeg. Auth: X-API-KEY -> user_id (mikeoscomputers).
Mints HMAC upload tickets ingest trusts; ingest calls back on completion; an
ffmpeg worker produces MP4 + HLS + thumbnail.

Guard rails: ISO-8601 timestamps, parameterized SQL, never-trust-200,
never load a whole file into RAM (ffmpeg streams; media served with byte-range).
"""
import asyncio
import hashlib
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
# Channels (P3) — one public creator profile per user, keyed on user_id.
# ---------------------------------------------------------------------------
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")


def _default_handle(user_id: str) -> str:
    """Deterministic, unique-per-user default @handle (stable, editable later)."""
    return "u" + hashlib.sha1(user_id.encode()).hexdigest()[:10]


def _channel_brief(handle, display_name, avatar_url, user_id: str) -> dict:
    """A compact channel descriptor for cards, tolerant of a missing row."""
    h = handle or _default_handle(user_id)
    return {
        "handle": h,
        "display_name": display_name or ("@" + h),
        "avatar_url": avatar_url,
    }


async def _ensure_channel(user_id: str):
    """Guarantee a channel row exists for a user; return it. Idempotent."""
    row = await db.pool().fetchrow(
        "SELECT user_id, handle, display_name, bio, avatar_url"
        " FROM mikevideo.channels WHERE user_id=$1", user_id)
    if row:
        return row
    await db.pool().execute(
        "INSERT INTO mikevideo.channels(user_id, handle) VALUES($1,$2)"
        " ON CONFLICT (user_id) DO NOTHING", user_id, _default_handle(user_id))
    return await db.pool().fetchrow(
        "SELECT user_id, handle, display_name, bio, avatar_url"
        " FROM mikevideo.channels WHERE user_id=$1", user_id)


async def _channel_for(user_id: str) -> dict:
    """Channel brief for a user_id (for the watch page). Tolerant of no row."""
    row = await db.pool().fetchrow(
        "SELECT handle, display_name, avatar_url FROM mikevideo.channels WHERE user_id=$1",
        user_id)
    if row:
        return _channel_brief(row["handle"], row["display_name"],
                              row["avatar_url"], user_id)
    return _channel_brief(None, None, None, user_id)


async def backfill_channels():
    """On startup, ensure every user who owns a video has a channel row so their
    default @handle resolves. Idempotent; small (one row per user)."""
    try:
        users = await db.pool().fetch("SELECT DISTINCT user_id FROM mikevideo.videos")
        for u in users:
            await _ensure_channel(u["user_id"])
    except Exception as e:
        logger.warning("backfill_channels skipped: %s", e)


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
    # P3: ensure existing creators have a resolvable @handle (idempotent).
    asyncio.create_task(backfill_channels())
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
        _CARD_SELECT +
        " WHERE v.visibility='public' AND v.status='ready'"
        " ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC LIMIT 200")
    return {"videos": [_public_card(r) for r in rows]}


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

    await _ensure_channel(user_id)   # P3: creator profile exists from first upload

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
        " enrich_status, visibility, view_count, bytes, taken_at, created_at FROM mikevideo.videos"
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
            "view_count": r["view_count"],
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
    " visibility, view_count, bytes, taken_at, created_at, published_at"
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
        "view_count": r["view_count"],
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
    payload = _detail_payload(r, is_owner=True)
    payload["channel"] = await _channel_for(r["user_id"])
    payload["channel"].update({
        "subscriber_count": await _sub_count(r["user_id"]),
        "subscribed": await _is_subscribed(user_id, r["user_id"]),
        "is_me": user_id == r["user_id"],
    })
    payload.update(await _engagement(vid, user_id))
    return payload


async def _engagement(video_id, viewer_user_id) -> dict:
    """Likes count + whether the viewer liked it + their saved playback position."""
    likes = await db.pool().fetchval(
        "SELECT count(*) FROM mikevideo.likes WHERE video_id=$1", video_id) or 0
    liked, position = False, None
    if viewer_user_id:
        liked = bool(await db.pool().fetchval(
            "SELECT 1 FROM mikevideo.likes WHERE video_id=$1 AND user_id=$2",
            video_id, viewer_user_id))
        position = await db.pool().fetchval(
            "SELECT position_sec FROM mikevideo.watch_progress"
            " WHERE video_id=$1 AND user_id=$2", video_id, viewer_user_id)
    return {"likes": int(likes), "liked": liked, "progress_sec": position}


# ---------------------------------------------------------------------------
# GET /api/public/videos/{id} — NO AUTH. Only returns PUBLIC videos (shared links).
# Private videos 404 here regardless of who asks.
# ---------------------------------------------------------------------------
@app.get("/api/public/videos/{video_id}")
async def get_public_video(video_id: str, request: Request):
    try:
        vid = uuid.UUID(video_id)
    except ValueError:
        return JSONResponse({"error": "bad id"}, status_code=400)
    r = await db.pool().fetchrow(
        f"SELECT {_DETAIL_COLS} FROM mikevideo.videos"
        " WHERE id=$1 AND visibility='public' AND status='ready'", vid)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = _detail_payload(r, is_owner=False)
    payload["channel"] = await _channel_for(r["user_id"])
    viewer = await authenticate(request)          # optional — personalises like/progress
    payload["channel"].update({
        "subscriber_count": await _sub_count(r["user_id"]),
        "subscribed": await _is_subscribed(viewer, r["user_id"]),
        "is_me": viewer == r["user_id"],
    })
    payload.update(await _engagement(vid, viewer))
    return payload


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
# Channels (P3) — public creator profiles at @handle
# ---------------------------------------------------------------------------
async def _channel_stats(user_id: str) -> dict:
    """Public-facing counts for a channel: public ready videos + views + subs."""
    row = await db.pool().fetchrow(
        "SELECT count(*) AS n, COALESCE(SUM(view_count),0) AS views"
        " FROM mikevideo.videos WHERE user_id=$1 AND visibility='public'"
        " AND status='ready'", user_id)
    return {"video_count": int(row["n"] or 0), "total_views": int(row["views"] or 0),
            "subscriber_count": await _sub_count(user_id)}


# ---------------------------------------------------------------------------
# Subscriptions (P4) — a viewer follows a creator (both are user_ids).
# ---------------------------------------------------------------------------
async def _sub_count(channel_user_id: str) -> int:
    return int(await db.pool().fetchval(
        "SELECT count(*) FROM mikevideo.subscriptions WHERE channel_user_id=$1",
        channel_user_id) or 0)


async def _is_subscribed(viewer_user_id, channel_user_id: str) -> bool:
    if not viewer_user_id:
        return False
    return bool(await db.pool().fetchval(
        "SELECT 1 FROM mikevideo.subscriptions"
        " WHERE subscriber_user_id=$1 AND channel_user_id=$2",
        viewer_user_id, channel_user_id))


def _public_card(r) -> dict:
    """A public video card from a row that joined videos + channels (columns:
    id, user_id, filename, ai_title, duration_sec, display_width, display_height,
    orientation, spoken_language, transcript_status, view_count, created_at,
    published_at, handle, channel_name, avatar_url)."""
    vid = str(r["id"])
    return {
        "id": vid, "status": "ready", "ai_title": r["ai_title"],
        "filename": r["filename"], "duration_sec": r["duration_sec"],
        "display_width": r["display_width"], "display_height": r["display_height"],
        "orientation": r["orientation"], "spoken_language": r["spoken_language"],
        "transcript_status": r["transcript_status"], "visibility": "public",
        "view_count": r["view_count"], "thumb_url": _thumb_url(r["user_id"], vid),
        "channel": _channel_brief(r["handle"], r["channel_name"], r["avatar_url"], r["user_id"]),
        "created_at": _iso(r["created_at"]), "published_at": _iso(r["published_at"]),
    }


# Shared column list + join for the public-video card queries (feed / subs feed).
_CARD_SELECT = (
    "SELECT v.id, v.user_id, v.filename, v.ai_title, v.duration_sec,"
    " v.display_width, v.display_height, v.orientation, v.spoken_language,"
    " v.transcript_status, v.view_count, v.created_at, v.published_at,"
    " c.handle, c.display_name AS channel_name, c.avatar_url"
    " FROM mikevideo.videos v"
    " LEFT JOIN mikevideo.channels c ON c.user_id=v.user_id"
)


def _channel_full(row) -> dict:
    """The full editable/public channel object from a channels row."""
    uid = row["user_id"]
    return {
        "user_id": uid,
        "handle": row["handle"] or _default_handle(uid),
        "display_name": row["display_name"] or ("@" + (row["handle"] or _default_handle(uid))),
        "bio": row["bio"],
        "avatar_url": row["avatar_url"],
    }


@app.get("/api/channels/me")
async def my_channel(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ch = await _ensure_channel(user_id)
    out = _channel_full(ch)
    out["is_me"] = True
    out["stats"] = await _channel_stats(user_id)
    return out


@app.put("/api/channels/me")
async def update_my_channel(request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await _ensure_channel(user_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    sets, params = [], []
    if "handle" in body:
        handle = str(body.get("handle") or "").lstrip("@").strip()
        if not _HANDLE_RE.match(handle):
            return JSONResponse(
                {"error": "handle must be 3-30 chars: letters, digits, underscore"},
                status_code=400)
        clash = await db.pool().fetchval(
            "SELECT 1 FROM mikevideo.channels WHERE lower(handle)=lower($1)"
            " AND user_id<>$2", handle, user_id)
        if clash:
            return JSONResponse({"error": "handle taken"}, status_code=409)
        params.append(handle); sets.append(f"handle=${len(params)}")
    if "display_name" in body:
        params.append(str(body.get("display_name") or "").strip()[:80])
        sets.append(f"display_name=${len(params)}")
    if "bio" in body:
        params.append((str(body.get("bio")).strip()[:500]) if body.get("bio") else None)
        sets.append(f"bio=${len(params)}")
    if "avatar_url" in body:
        params.append((str(body.get("avatar_url")).strip()[:500]) if body.get("avatar_url") else None)
        sets.append(f"avatar_url=${len(params)}")
    if not sets:
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    params.append(user_id)
    row = await db.pool().fetchrow(
        f"UPDATE mikevideo.channels SET {', '.join(sets)}, updated_at=now()"
        f" WHERE user_id=${len(params)}"
        " RETURNING user_id, handle, display_name, bio, avatar_url", *params)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    out = _channel_full(row)
    out["is_me"] = True
    out["stats"] = await _channel_stats(user_id)
    return out


@app.get("/api/channels/{handle}")
async def public_channel(handle: str, request: Request):
    handle = handle.lstrip("@")
    ch = await db.pool().fetchrow(
        "SELECT user_id, handle, display_name, bio, avatar_url"
        " FROM mikevideo.channels WHERE lower(handle)=lower($1)", handle)
    if ch is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    owner = ch["user_id"]
    rows = await db.pool().fetch(
        "SELECT id, ai_title, filename, duration_sec, display_width, display_height,"
        " orientation, spoken_language, transcript_status, view_count, created_at,"
        " published_at FROM mikevideo.videos"
        " WHERE user_id=$1 AND visibility='public' AND status='ready'"
        " ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 200", owner)
    videos = []
    for r in rows:
        vid = str(r["id"])
        videos.append({
            "id": vid, "status": "ready", "ai_title": r["ai_title"],
            "filename": r["filename"], "duration_sec": r["duration_sec"],
            "display_width": r["display_width"], "display_height": r["display_height"],
            "orientation": r["orientation"], "spoken_language": r["spoken_language"],
            "transcript_status": r["transcript_status"], "view_count": r["view_count"],
            "visibility": "public", "thumb_url": _thumb_url(owner, vid),
            "created_at": _iso(r["created_at"]),
        })
    out = _channel_full(ch)
    out.pop("user_id", None)                       # don't leak the raw user_id
    viewer = await authenticate(request)           # optional — flag "this is me"
    return {"channel": out, "is_me": viewer == owner,
            "subscribed": await _is_subscribed(viewer, owner),
            "stats": await _channel_stats(owner), "videos": videos}


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe to a channel (P4). Both keyed on user_id.
# ---------------------------------------------------------------------------
async def _channel_owner(handle: str):
    return await db.pool().fetchval(
        "SELECT user_id FROM mikevideo.channels WHERE lower(handle)=lower($1)",
        handle.lstrip("@"))


@app.post("/api/channels/{handle}/subscribe")
async def subscribe(handle: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner = await _channel_owner(handle)
    if not owner:
        return JSONResponse({"error": "not found"}, status_code=404)
    if owner == user_id:
        return JSONResponse({"error": "cannot subscribe to yourself"}, status_code=400)
    await db.pool().execute(
        "INSERT INTO mikevideo.subscriptions(subscriber_user_id, channel_user_id)"
        " VALUES($1,$2) ON CONFLICT DO NOTHING", user_id, owner)
    return {"subscribed": True, "subscriber_count": await _sub_count(owner)}


@app.delete("/api/channels/{handle}/subscribe")
async def unsubscribe(handle: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner = await _channel_owner(handle)
    if not owner:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.pool().execute(
        "DELETE FROM mikevideo.subscriptions"
        " WHERE subscriber_user_id=$1 AND channel_user_id=$2", user_id, owner)
    return {"subscribed": False, "subscriber_count": await _sub_count(owner)}


# GET /api/feed/subscriptions — recent public videos from channels you follow.
@app.get("/api/feed/subscriptions")
async def subscriptions_feed(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = await db.pool().fetch(
        _CARD_SELECT +
        " JOIN mikevideo.subscriptions s ON s.channel_user_id=v.user_id"
        "   AND s.subscriber_user_id=$1"
        " WHERE v.visibility='public' AND v.status='ready'"
        " ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC LIMIT 200", user_id)
    return {"videos": [_public_card(r) for r in rows]}


# ---------------------------------------------------------------------------
# Comments (P5) — threaded (one reply level), per-viewer likes + owner heart.
# ---------------------------------------------------------------------------
async def _video_access(video_id):
    """(owner_user_id, visibility) for a video, or (None, None) if missing."""
    r = await db.pool().fetchrow(
        "SELECT user_id, visibility FROM mikevideo.videos WHERE id=$1", video_id)
    return (r["user_id"], r["visibility"]) if r else (None, None)


def _comment_node(r, viewer, owner) -> dict:
    return {
        "id": str(r["id"]),
        "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
        "body": r["body"],
        "hearted": bool(r["hearted"]),
        "like_count": int(r["like_count"] or 0),
        "liked": bool(r["liked"]),
        "author": _channel_brief(r["handle"], r["channel_name"], r["avatar_url"], r["user_id"]),
        "created_at": _iso(r["created_at"]),
        "can_delete": bool(viewer and (viewer == r["user_id"] or viewer == owner)),
        "is_author": viewer == r["user_id"],
        "replies": [],
    }


_COMMENT_SELECT = (
    "SELECT c.id, c.user_id, c.parent_id, c.body, c.hearted, c.created_at,"
    " ch.handle, ch.display_name AS channel_name, ch.avatar_url,"
    " (SELECT count(*) FROM mikevideo.comment_likes cl WHERE cl.comment_id=c.id) AS like_count,"
    " ($2::text IS NOT NULL AND EXISTS(SELECT 1 FROM mikevideo.comment_likes cl"
    "   WHERE cl.comment_id=c.id AND cl.user_id=$2)) AS liked"
    " FROM mikevideo.comments c"
    " LEFT JOIN mikevideo.channels ch ON ch.user_id=c.user_id"
)


@app.get("/api/videos/{video_id}/comments")
async def list_comments(video_id: str, request: Request):
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    owner, vis = await _video_access(vid)
    if owner is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    viewer = await authenticate(request)
    if vis != "public" and viewer != owner:      # private → owner only
        return JSONResponse({"error": "not found"}, status_code=404)
    rows = await db.pool().fetch(
        _COMMENT_SELECT + " WHERE c.video_id=$1 ORDER BY c.created_at ASC", vid, viewer)
    nodes = {str(r["id"]): _comment_node(r, viewer, owner) for r in rows}
    top = []
    for r in rows:
        n = nodes[str(r["id"])]
        pid = n["parent_id"]
        if pid and pid in nodes:
            nodes[pid]["replies"].append(n)      # replies stay chronological
        else:
            top.append(n)
    top.sort(key=lambda n: n["created_at"] or "", reverse=True)   # newest thread first
    return {"video_id": str(vid), "count": len(rows), "comments": top,
            "can_comment": bool(viewer and (vis == "public" or viewer == owner)),
            "is_owner": viewer == owner}


@app.post("/api/videos/{video_id}/comments")
async def post_comment(video_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    owner, vis = await _video_access(vid)
    if owner is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if vis != "public" and user_id != owner:
        return JSONResponse({"error": "cannot comment on this video"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    text = str(body.get("body") or "").strip()[:2000]
    if not text:
        return JSONResponse({"error": "body required"}, status_code=400)
    pid = None
    if body.get("parent_id"):
        pid = _vid_or_400(str(body.get("parent_id")))
        prow = await db.pool().fetchrow(
            "SELECT parent_id FROM mikevideo.comments WHERE id=$1 AND video_id=$2", pid, vid)
        if prow is None:
            return JSONResponse({"error": "parent not found"}, status_code=400)
        if prow["parent_id"]:                    # collapse to single-depth threads
            pid = prow["parent_id"]
    await _ensure_channel(user_id)               # so the author's @handle resolves
    cid = uuid.uuid4()
    await db.pool().execute(
        "INSERT INTO mikevideo.comments(id, video_id, user_id, parent_id, body)"
        " VALUES($1,$2,$3,$4,$5)", cid, vid, user_id, pid, text)
    r = await db.pool().fetchrow(_COMMENT_SELECT + " WHERE c.id=$1", cid, user_id)
    return _comment_node(r, user_id, owner)


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = _vid_or_400(comment_id)
    if cid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    row = await db.pool().fetchrow(
        "SELECT c.user_id, v.user_id AS owner FROM mikevideo.comments c"
        " JOIN mikevideo.videos v ON v.id=c.video_id WHERE c.id=$1", cid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if user_id != row["user_id"] and user_id != row["owner"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    await db.pool().execute("DELETE FROM mikevideo.comments WHERE id=$1", cid)  # cascade
    return {"ok": True}


async def _comment_like_count(cid) -> int:
    return int(await db.pool().fetchval(
        "SELECT count(*) FROM mikevideo.comment_likes WHERE comment_id=$1", cid) or 0)


@app.post("/api/comments/{comment_id}/like")
async def like_comment(comment_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = _vid_or_400(comment_id)
    if cid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    exists = await db.pool().fetchval("SELECT 1 FROM mikevideo.comments WHERE id=$1", cid)
    if not exists:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.pool().execute(
        "INSERT INTO mikevideo.comment_likes(comment_id, user_id) VALUES($1,$2)"
        " ON CONFLICT DO NOTHING", cid, user_id)
    return {"liked": True, "like_count": await _comment_like_count(cid)}


@app.delete("/api/comments/{comment_id}/like")
async def unlike_comment(comment_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = _vid_or_400(comment_id)
    if cid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    await db.pool().execute(
        "DELETE FROM mikevideo.comment_likes WHERE comment_id=$1 AND user_id=$2", cid, user_id)
    return {"liked": False, "like_count": await _comment_like_count(cid)}


async def _set_heart(comment_id: str, request: Request, on: bool):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = _vid_or_400(comment_id)
    if cid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    row = await db.pool().fetchrow(
        "SELECT v.user_id AS owner FROM mikevideo.comments c"
        " JOIN mikevideo.videos v ON v.id=c.video_id WHERE c.id=$1", cid)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if user_id != row["owner"]:
        return JSONResponse({"error": "only the video owner can heart"}, status_code=403)
    await db.pool().execute(
        "UPDATE mikevideo.comments SET hearted=$2, updated_at=now() WHERE id=$1", cid, on)
    return {"hearted": on}


@app.post("/api/comments/{comment_id}/heart")
async def heart_comment(comment_id: str, request: Request):
    return await _set_heart(comment_id, request, True)


@app.delete("/api/comments/{comment_id}/heart")
async def unheart_comment(comment_id: str, request: Request):
    return await _set_heart(comment_id, request, False)


# ---------------------------------------------------------------------------
# Engagement (P1): views · likes · watch progress
# ---------------------------------------------------------------------------
def _vid_or_400(video_id: str):
    try:
        return uuid.UUID(video_id)
    except ValueError:
        return None


@app.post("/api/videos/{video_id}/view")
async def add_view(video_id: str):
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    n = await db.pool().fetchval(
        "UPDATE mikevideo.videos SET view_count=view_count+1"
        " WHERE id=$1 AND status='ready' RETURNING view_count", vid)
    if n is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"view_count": int(n)}


@app.post("/api/videos/{video_id}/like")
async def add_like(video_id: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    await db.pool().execute(
        "INSERT INTO mikevideo.likes(video_id, user_id) VALUES($1,$2)"
        " ON CONFLICT DO NOTHING", vid, user_id)
    likes = await db.pool().fetchval(
        "SELECT count(*) FROM mikevideo.likes WHERE video_id=$1", vid)
    return {"liked": True, "likes": int(likes or 0)}


@app.delete("/api/videos/{video_id}/like")
async def remove_like(video_id: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    await db.pool().execute(
        "DELETE FROM mikevideo.likes WHERE video_id=$1 AND user_id=$2", vid, user_id)
    likes = await db.pool().fetchval(
        "SELECT count(*) FROM mikevideo.likes WHERE video_id=$1", vid)
    return {"liked": False, "likes": int(likes or 0)}


@app.put("/api/videos/{video_id}/progress")
async def put_progress(video_id: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(video_id)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        body = await request.json()
        pos = float(body.get("position_sec") or 0)
    except Exception:
        return JSONResponse({"error": "position_sec required"}, status_code=400)
    dur = body.get("duration_sec")
    dur = float(dur) if dur is not None else None
    await db.pool().execute(
        "INSERT INTO mikevideo.watch_progress(video_id, user_id, position_sec, duration_sec, updated_at)"
        " VALUES($1,$2,$3,$4,now()) ON CONFLICT (video_id, user_id) DO UPDATE"
        " SET position_sec=EXCLUDED.position_sec, duration_sec=COALESCE(EXCLUDED.duration_sec, mikevideo.watch_progress.duration_sec),"
        " updated_at=now()", vid, user_id, pos, dur)
    return {"ok": True}


@app.get("/api/me/continue")
async def continue_watching(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = await db.pool().fetch(
        "SELECT v.id, v.user_id AS owner, v.filename, v.ai_title, v.duration_sec,"
        " v.display_width, v.display_height, v.orientation, v.spoken_language,"
        " v.transcript_status, v.view_count, v.created_at, w.position_sec, w.updated_at"
        " FROM mikevideo.watch_progress w JOIN mikevideo.videos v ON v.id=w.video_id"
        " WHERE w.user_id=$1 AND v.status='ready' AND w.position_sec > 5"
        "   AND (v.duration_sec IS NULL OR w.position_sec < v.duration_sec*0.95)"
        " ORDER BY w.updated_at DESC LIMIT 30", user_id)
    out = []
    for r in rows:
        vid = str(r["id"])
        out.append({
            "id": vid, "status": "ready", "ai_title": r["ai_title"], "filename": r["filename"],
            "duration_sec": r["duration_sec"], "display_width": r["display_width"],
            "display_height": r["display_height"], "orientation": r["orientation"],
            "spoken_language": r["spoken_language"], "transcript_status": r["transcript_status"],
            "view_count": r["view_count"], "progress_sec": r["position_sec"],
            "thumb_url": _thumb_url(r["owner"], vid), "created_at": _iso(r["created_at"]),
        })
    return {"videos": out}


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


# ---------------------------------------------------------------------------
# Playlists + Watch Later (P2)
# ---------------------------------------------------------------------------
def _pl_card(r) -> dict:
    vid = str(r["id"])
    return {
        "id": vid, "status": "ready", "ai_title": r["ai_title"], "filename": r["filename"],
        "duration_sec": r["duration_sec"], "display_width": r["display_width"],
        "display_height": r["display_height"], "orientation": r["orientation"],
        "spoken_language": r["spoken_language"], "transcript_status": r["transcript_status"],
        "view_count": r["view_count"], "visibility": r["visibility"],
        "thumb_url": _thumb_url(r["owner"], vid), "created_at": _iso(r["created_at"]),
    }


async def _ensure_watch_later(user_id: str):
    row = await db.pool().fetchrow(
        "SELECT id FROM mikevideo.playlists WHERE user_id=$1 AND is_system='watch_later'", user_id)
    if row:
        return row["id"]
    try:
        pid = uuid.uuid4()
        await db.pool().execute(
            "INSERT INTO mikevideo.playlists(id,user_id,title,is_system)"
            " VALUES($1,$2,'Watch Later','watch_later')", pid, user_id)
        return pid
    except Exception:
        row = await db.pool().fetchrow(
            "SELECT id FROM mikevideo.playlists WHERE user_id=$1 AND is_system='watch_later'", user_id)
        return row["id"] if row else None


@app.get("/api/playlists")
async def list_playlists(request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await _ensure_watch_later(user_id)
    video_id = request.query_params.get("video_id")  # optional -> per-playlist `has`
    has_vid = None
    if video_id:
        try:
            has_vid = uuid.UUID(video_id)
        except ValueError:
            has_vid = None
    rows = await db.pool().fetch(
        "SELECT p.id, p.title, p.is_system, p.updated_at,"
        " (SELECT count(*) FROM mikevideo.playlist_items i WHERE i.playlist_id=p.id) AS n,"
        " (SELECT v.user_id||'/'||v.id FROM mikevideo.playlist_items pi"
        "   JOIN mikevideo.videos v ON v.id=pi.video_id WHERE pi.playlist_id=p.id"
        "   ORDER BY pi.sort_order, pi.added_at LIMIT 1) AS first_path,"
        " ($2::uuid IS NOT NULL AND EXISTS(SELECT 1 FROM mikevideo.playlist_items x"
        "   WHERE x.playlist_id=p.id AND x.video_id=$2)) AS has"
        " FROM mikevideo.playlists p WHERE p.user_id=$1"
        " ORDER BY (p.is_system IS NOT NULL) DESC, p.updated_at DESC", user_id, has_vid)
    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]), "title": r["title"], "is_system": r["is_system"],
            "count": int(r["n"] or 0), "has": bool(r["has"]),
            "thumb_url": (f"/media/{r['first_path']}/thumb.jpg" if r["first_path"] else None),
            "updated_at": _iso(r["updated_at"]),
        })
    return {"playlists": out}


@app.post("/api/playlists")
async def create_playlist(request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        title = str((await request.json()).get("title") or "").strip()
    except Exception:
        title = ""
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    pid = uuid.uuid4()
    await db.pool().execute(
        "INSERT INTO mikevideo.playlists(id,user_id,title) VALUES($1,$2,$3)",
        pid, user_id, title[:120])
    return {"id": str(pid), "title": title[:120], "is_system": None, "count": 0, "has": False}


@app.delete("/api/playlists/{pid}")
async def delete_playlist(pid: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(pid)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    row = await db.pool().fetchrow(
        "DELETE FROM mikevideo.playlists WHERE id=$1 AND user_id=$2 AND is_system IS NULL"
        " RETURNING id", vid, user_id)
    if row is None:
        return JSONResponse({"error": "not found or not deletable"}, status_code=404)
    return {"ok": True}


@app.get("/api/playlists/{pid}")
async def get_playlist(pid: str, request: Request):
    user_id = await _auth(request)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    vid = _vid_or_400(pid)
    if vid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    p = await db.pool().fetchrow(
        "SELECT id, title, is_system FROM mikevideo.playlists WHERE id=$1 AND user_id=$2",
        vid, user_id)
    if p is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    rows = await db.pool().fetch(
        "SELECT v.id, v.user_id AS owner, v.filename, v.ai_title, v.duration_sec,"
        " v.display_width, v.display_height, v.orientation, v.spoken_language,"
        " v.transcript_status, v.view_count, v.visibility, v.created_at"
        " FROM mikevideo.playlist_items pi JOIN mikevideo.videos v ON v.id=pi.video_id"
        " WHERE pi.playlist_id=$1 AND v.status='ready' ORDER BY pi.sort_order, pi.added_at", vid)
    return {"id": str(p["id"]), "title": p["title"], "is_system": p["is_system"],
            "videos": [_pl_card(r) for r in rows]}


@app.post("/api/playlists/{pid}/items")
async def add_playlist_item(pid: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    plid = _vid_or_400(pid)
    if plid is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        video_id = uuid.UUID(str((await request.json()).get("video_id")))
    except Exception:
        return JSONResponse({"error": "video_id required"}, status_code=400)
    owned = await db.pool().fetchval(
        "SELECT 1 FROM mikevideo.playlists WHERE id=$1 AND user_id=$2", plid, user_id)
    if not owned:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.pool().execute(
        "INSERT INTO mikevideo.playlist_items(playlist_id, video_id, sort_order)"
        " VALUES($1,$2, COALESCE((SELECT max(sort_order)+1 FROM mikevideo.playlist_items"
        "   WHERE playlist_id=$1),0)) ON CONFLICT DO NOTHING", plid, video_id)
    await db.pool().execute(
        "UPDATE mikevideo.playlists SET updated_at=now() WHERE id=$1", plid)
    return {"ok": True}


@app.delete("/api/playlists/{pid}/items/{video_id}")
async def remove_playlist_item(pid: str, video_id: str, request: Request):
    user_id = await _auth(request, scope="video.write")
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    plid, vidv = _vid_or_400(pid), _vid_or_400(video_id)
    if plid is None or vidv is None:
        return JSONResponse({"error": "bad id"}, status_code=400)
    owned = await db.pool().fetchval(
        "SELECT 1 FROM mikevideo.playlists WHERE id=$1 AND user_id=$2", plid, user_id)
    if not owned:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.pool().execute(
        "DELETE FROM mikevideo.playlist_items WHERE playlist_id=$1 AND video_id=$2", plid, vidv)
    return {"ok": True}


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
# The SPA is self-contained and ships fixes inline, so never let a browser serve a
# stale copy — always revalidate (a cached old index.html can break playback/features).
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/", response_class=HTMLResponse)
async def index():
    f = WEB_DIR / "index.html"
    return HTMLResponse(f.read_text(), headers=_NO_CACHE)


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback():
    # OAuth redirect target — the SPA reads ?code&state and exchanges it.
    f = WEB_DIR / "index.html"
    return HTMLResponse(f.read_text(), headers=_NO_CACHE)


@app.get("/hls.js")
async def hlsjs():
    f = WEB_DIR / "hls.min.js"
    return Response(f.read_text(), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"})
