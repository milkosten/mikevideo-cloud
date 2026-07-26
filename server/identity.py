"""Agent-key -> user_id resolution against mikeoscomputers (the IdP).

Apps authenticate with their hive agent key, sent as the X-API-KEY header. We
resolve it via
  GET {MIKEOSCOMPUTERS_URL}/api/mikeos/agents/resolve/{agent_key}
    -> {valid: bool, user_id: str, ...}
and scope ALL kitchen/shopping data per user_id. This is the same trust pattern
the hive and mikeos-oauth use.

Successful resolutions are cached briefly to avoid a network hop per request.
"""
import os
import time
import logging
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

MIKEOSCOMPUTERS_URL = os.environ.get(
    "MIKEOSCOMPUTERS_URL",
    "https://mikeoscomputers-production.up.railway.app",
)

# --- OAuth 2.0 resource server (account.osmike.com) -------------------------
# Standard RS256 Bearer JWTs, validated LOCALLY against the published JWKS — no
# per-request round-trip to the IdP. sub == the same user_id resolve returns, so
# all our WHERE user_id=$1 scoping is unchanged. See docs/implement_oauth.md.
ACCOUNT_ISSUER = os.environ.get("ACCOUNT_OSMIKE_ISSUER", "https://account.osmike.com")
ACCOUNT_JWKS = os.environ.get("ACCOUNT_OSMIKE_JWKS_URL", f"{ACCOUNT_ISSUER}/oauth/jwks.json")
SERVICE_AUD = os.environ.get("OAUTH_AUDIENCE", "mikevideo")
# Lazy: constructing the client does NOT fetch keys, so this is safe even before
# the OAuth provider is live (fetch happens on the first Bearer request).
_jwks = PyJWKClient(ACCOUNT_JWKS, cache_keys=True, lifespan=3600)


def _verify_bearer(token: str) -> Optional[dict]:
    """Validate an account.osmike.com access token locally. Returns claims or None."""
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key, algorithms=["RS256"],
            issuer=ACCOUNT_ISSUER, audience=SERVICE_AUD,
            options={"require": ["exp", "iss", "sub"]})
    except Exception as e:
        logger.warning("Bearer rejected: %s", e)
        return None


def _has_scope(claims: dict, needed: str) -> bool:
    return needed in (claims.get("scope") or "").split()


# --- Web session bridge -----------------------------------------------------
# The provider is device-grant only today (no /oauth/authorize, no code grant),
# so a browser cannot obtain an OAuth token. Until the provider ships the web
# flow, "Sign in with MikeOS" on the website is email/password -> a session JWT
# from the IdP, which we validate here via /api/auth/me. CACHED (not per-request)
# so it isn't a round-trip anti-pattern; a logged-in user = full access (owner).
_session_cache: dict[str, tuple[str, float]] = {}  # token -> (user_id, expires)


async def _verify_session(token: str) -> Optional[str]:
    cached = _session_cache.get(token)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    base = MIKEOSCOMPUTERS_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/api/auth/me",
                                    headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as e:
        logger.warning("session /me failed: %s", e)
        return None
    uid = data.get("id") or data.get("user_id")
    if not uid:
        return None
    _session_cache[token] = (str(uid), time.monotonic() + _RESOLVE_CACHE_TTL)
    return str(uid)


async def authenticate(request, scope: Optional[str] = None) -> Optional[str]:
    """Return the caller's user_id, or None (caller -> 401).

    Order: OAuth RS256 Bearer (JWKS, scoped) -> MikeOS web session JWT (email/pw
    login, cached /me) -> legacy X-API-KEY. A present-but-invalid Bearer must 401 —
    it never falls through to the key path.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        claims = _verify_bearer(token)                     # OAuth RS256 (preferred)
        if claims:
            if scope is None or _has_scope(claims, scope):
                return str(claims["sub"])                  # sub == user_id
            return None                                    # valid token, missing scope
        uid = await _verify_session(token)                 # web email/password session
        if uid:
            return uid                                     # owner login = full access
        return None
    key = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
    return await resolve_agent_key(key)                    # unchanged legacy path

_RESOLVE_CACHE_TTL = 300  # seconds
_resolve_cache: dict[str, tuple[str, float]] = {}  # agent_key -> (user_id, expires)


async def resolve_agent_key(agent_key: Optional[str]) -> Optional[str]:
    """Resolve an agent (hive) key to its user_id. Returns None if invalid.

    Network / IdP errors fail closed (return None -> caller responds 401).
    """
    if not agent_key:
        return None

    cached = _resolve_cache.get(agent_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    base = MIKEOSCOMPUTERS_URL.rstrip("/")
    url = f"{base}/api/mikeos/agents/resolve/{agent_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning("IdP resolve returned HTTP %s", resp.status_code)
                return None
            data = resp.json()
    except Exception as e:
        logger.error("Error resolving agent key against IdP: %s", e)
        return None

    if not data or not data.get("valid"):
        return None
    user_id = data.get("user_id")
    if not user_id:
        logger.warning("IdP resolve returned valid=true without user_id")
        return None

    _resolve_cache[agent_key] = (str(user_id), time.monotonic() + _RESOLVE_CACHE_TTL)
    return str(user_id)
