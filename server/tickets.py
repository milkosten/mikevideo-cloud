"""HMAC upload tickets — minted by cloud, verified by ingest.

Ticket wire format (matches ingest's _verify_ticket):
    base64url(json_payload) + '.' + base64url(hmac_sha256(payload))
payload = {upload_id, user_id, max_size, file_hash, exp}
"""
import base64
import hashlib
import hmac
import json
import time

from . import config


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint_ticket(upload_id: str, user_id: str, max_size: int, file_hash: str,
                ttl: int | None = None) -> str:
    """Return a signed ticket string ingest will accept.

    file_hash is the whole-file sha256 ("sha256:..." or bare hex).
    """
    if not config.INGEST_HMAC_SECRET:
        raise RuntimeError("INGEST_HMAC_SECRET not configured")
    ttl = ttl if ttl is not None else config.TICKET_TTL_SECONDS
    payload = {
        "upload_id": upload_id,
        "user_id": user_id,
        "max_size": int(max_size),
        "file_hash": file_hash,
        "exp": int(time.time()) + ttl,
    }
    payload_raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(config.INGEST_HMAC_SECRET.encode(), payload_raw,
                   hashlib.sha256).digest()
    return f"{_b64u(payload_raw)}.{_b64u(sig)}"


def verify_callback_signature(raw_body: bytes, signature: str) -> bool:
    """Verify the ingest completion callback's X-Ingest-Signature (hex hmac)."""
    if not config.CALLBACK_SECRET or not signature:
        return False
    want = hmac.new(config.CALLBACK_SECRET.encode(), raw_body,
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip())
