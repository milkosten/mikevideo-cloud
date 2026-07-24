"""Environment configuration for mikevideo-cloud."""
import os
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Where all media lives (shared /data volume with ingest).
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data/mikevideo"))
# Ingest writes completed blobs here: {INGEST_DATA_ROOT}/{upload_id}/data.bin
INGEST_DATA_ROOT = Path(os.environ.get("INGEST_DATA_ROOT", "/data/mikevideo/ingest"))

# Shared HMAC secret with ingest — cloud mints tickets ingest trusts.
INGEST_HMAC_SECRET = os.environ.get("INGEST_HMAC_SECRET", "")
# Shared secret ingest signs its completion callback with.
CALLBACK_SECRET = os.environ.get("CALLBACK_SECRET", "")

# Public ingest URL the browser/app uploads chunks to.
INGEST_URL = os.environ.get("INGEST_URL", "https://up.osmike.com")

# Per-user storage quota (bytes). Default 100 GB.
USER_QUOTA_BYTES = int(os.environ.get("USER_QUOTA_BYTES", str(100 * 1024 * 1024 * 1024)))

# Ticket TTL (seconds) — how long a minted upload ticket stays valid.
TICKET_TTL_SECONDS = int(os.environ.get("TICKET_TTL_SECONDS", str(6 * 3600)))

# Cap accepted upload size at ticket mint (bytes). Default 20 GB (matches ingest MAX_TOTAL_SIZE).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024 * 1024)))

PORT = int(os.environ.get("PORT", "8090"))

# --- Speech → text (see docs/SPEECH-PIPELINE.md) ---------------------------
# Quality-first, CPU-only, background. large-v3 via faster-whisper (int8).
ASR_ENABLED = os.environ.get("ASR_ENABLED", "true").lower() not in ("0", "false", "no")
ASR_MODEL = os.environ.get("ASR_MODEL", "large-v3")
ASR_COMPUTE_TYPE = os.environ.get("ASR_COMPUTE_TYPE", "int8")
ASR_BEAM_SIZE = int(os.environ.get("ASR_BEAM_SIZE", "5"))
# Cap CPU threads so transcription stays a gentle neighbour to the OSM stack on the box.
ASR_CPU_THREADS = int(os.environ.get("ASR_CPU_THREADS", "4"))
# Model weights cache — on the /data volume so container rebuilds don't re-download.
ASR_MODEL_CACHE = Path(os.environ.get("ASR_MODEL_CACHE", str(DATA_ROOT / "models")))
# Force a language code (e.g. "en"), or empty = auto-detect (LID folded into ASR).
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "")
