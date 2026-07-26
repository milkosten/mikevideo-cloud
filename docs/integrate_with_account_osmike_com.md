# Integrate MikeVideo auth with account.osmike.com (OAuth 2.0)

**For the MikeVideo Claude session.** This replaces MikeVideo's raw `X-API-KEY` auth with
**standard OAuth 2.0 Bearer tokens issued by `account.osmike.com`** — while keeping the ecosystem
working the whole way (dual-auth, zero downtime). The full ecosystem design is
`mikeos-architecture/docs/ACCOUNT-OSMIKE-OAUTH-PLAN.md`; read it first. MikeVideo is the **pilot**.

> **Non-technical is the requirement.** The user never sees a token or key. They create their MikeOS
> account once (email, no phone) and approve their phone once — done. Everything below is invisible to
> them; it's how the *code* stops passing a naked UUID around and starts passing a signed, expiring,
> audience-scoped token.

---

## 1. What MikeVideo does today (the thing to replace)

- **`mikevideo-cloud`** reads `X-API-KEY: <uuid>` and calls
  `resolve_agent_key()` → `GET account.osmike.com/api/mikeos/agents/resolve/{key}` → `user_id`
  (see `server/identity.py`, `server/app.py:87`). Long-lived opaque UUID, no scopes, no expiry, an
  IdP round-trip per request.
- **`mikevideo-app`** sends its per-app **hive key** (`HiveIdentity.agentKey`) as that `X-API-KEY`.

## 2. Target state

- **`mikevideo-cloud`** accepts **`Authorization: Bearer <JWT>`** issued by account.osmike.com,
  validates it **locally** against the published JWKS (no per-request IdP call), and takes
  `user_id = sub`. It **also** still accepts `X-API-KEY` during migration (dual-auth).
- **`mikevideo-app`** stops sending its hive key and instead asks the **on-device daemon** for a
  fresh, MikeVideo-scoped access token and sends it as a `Bearer` header.

Nothing about MikeVideo's data model changes — `user_id` is the same value as before, so all your
per-user scoping (`WHERE user_id = $1`) is untouched.

## 3. The account.osmike.com surface MikeVideo depends on

| Endpoint | Use |
|---|---|
| `GET https://account.osmike.com/.well-known/openid-configuration` | discovery (issuer, jwks_uri) |
| `GET https://account.osmike.com/oauth/jwks.json` | RSA public keys to verify token signatures (cache these) |

**Access-token claims MikeVideo checks:** `iss = https://account.osmike.com`, `aud` includes
`mikevideo`, `exp` not passed, `sub` = the user_id, `scope` contains what the request needs.

**MikeVideo scopes:** `video.read` (list/stream), `video.write` (upload/ingest/delete). Ask for
`video.read video.write` by default.

> **Dependency / status:** the account.osmike.com OAuth AS is **not built yet** (tracked in the plan
> doc §10). Implement the code below now — it is **safe to deploy immediately** because the Bearer path
> is simply inert until real tokens arrive, and X-API-KEY keeps working. When the AS goes live, MikeVideo
> is already ready.

## 4. `mikevideo-cloud` — become an OAuth resource server (dual-auth)

Add `pyjwt[crypto]` to `requirements.txt`. Extend `server/identity.py` with local JWT validation and
a single `authenticate(request)` entrypoint that prefers Bearer and falls back to X-API-KEY:

```python
# server/identity.py  (additions)
import os, time, logging
from typing import Optional
import jwt  # PyJWT ; add "pyjwt[crypto]" to requirements.txt
from jwt import PyJWKClient

log = logging.getLogger(__name__)

ACCOUNT_ISSUER = os.environ.get("ACCOUNT_OSMIKE_ISSUER", "https://account.osmike.com")
ACCOUNT_JWKS_URL = os.environ.get("ACCOUNT_OSMIKE_JWKS_URL", f"{ACCOUNT_ISSUER}/oauth/jwks.json")
SERVICE_AUDIENCE = os.environ.get("OAUTH_AUDIENCE", "mikevideo")

# PyJWKClient caches keys and handles kid rotation.
_jwk_client = PyJWKClient(ACCOUNT_JWKS_URL, cache_keys=True, lifespan=3600)

def _verify_bearer(token: str) -> Optional[dict]:
    """Validate an account.osmike.com access token locally. Returns claims or None."""
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, signing_key, algorithms=["RS256"],
            issuer=ACCOUNT_ISSUER, audience=SERVICE_AUDIENCE,
            options={"require": ["exp", "iss", "sub"]},
        )
        return claims
    except Exception as e:
        log.warning("Bearer JWT rejected: %s", e)
        return None

def _has_scope(claims: dict, needed: str) -> bool:
    return needed in (claims.get("scope") or "").split()

async def authenticate(request, scope: Optional[str] = None) -> Optional[str]:
    """Return the user_id for a request, or None (caller -> 401).

    Order: OAuth Bearer (preferred) -> legacy X-API-KEY (during migration).
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        claims = _verify_bearer(auth[7:].strip())
        if claims and (scope is None or _has_scope(claims, scope)):
            return str(claims["sub"])          # sub == user_id
        return None                            # a present-but-bad Bearer must NOT silently fall through

    key = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
    return await resolve_agent_key(key)        # unchanged legacy path
```

Then point the existing dependency at it. In `server/app.py` (currently ~line 87):

```python
# before
key = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
return await resolve_agent_key(key)

# after
from .identity import authenticate
return await authenticate(request)                       # reads only
# for write routes (upload/ingest/delete), require the scope:
# return await authenticate(request, scope="video.write")
```

Rules that still apply (house rules): **never trust HTTP 200** — a request with no valid user_id must
401; keep timestamps ISO-8601; parameterized SQL; the public HLS/MP4 stream routes stay unauthenticated
by segment as they are today (that's a signed-URL/CDN concern, not this change).

**Env to set on the mikevideo-cloud Railway service:** `ACCOUNT_OSMIKE_ISSUER=https://account.osmike.com`
(defaults are correct; `OAUTH_AUDIENCE=mikevideo`). No secrets — validation uses only the **public** JWKS.

## 5. `mikevideo-app` — get the token from the daemon (seamless, no key stored)

Stop sending `HiveIdentity.agentKey`. Instead fetch a short-lived MikeVideo token from the on-device
daemon over loopback (same trust pattern as `GET /api/location`), and send it as `Bearer`:

```kotlin
// Ask the daemon (the on-device OAuth token agent) for a fresh MikeVideo-scoped token.
// GET https://127.0.0.1:7743/api/auth/token?aud=mikevideo&scope=video.read+video.write
//   Authorization: Bearer <loopback daemon token>   -> { access_token, expires_in }
suspend fun mikeVideoAccessToken(): String? =
    daemonGet("/api/auth/token?aud=mikevideo&scope=video.read+video.write")
        ?.optString("access_token")

// then on every mikevideo-cloud call:
//   requestBuilder.header("Authorization", "Bearer $token")   // instead of header("X-API-KEY", agentKey)
```

Cache the token until shortly before `expires_in`; re-fetch on 401. The daemon holds the device
refresh token and mints/refreshes silently (plan §5) — the app stores nothing sensitive.

> **Interim (until the daemon's `/api/auth/token` and the AS exist):** the app keeps sending
> `X-API-KEY = agentKey` and the cloud's dual-auth accepts it. Flip the app to Bearer only after the
> daemon endpoint is live; no coordinated deploy needed because the cloud accepts both.

## 6. How the device got authorized (context — you don't build this in MikeVideo)

The user's one-time consent happens in the **MikeOS Setup Wizard**: the daemon runs the OAuth
**Device Authorization Grant** (RFC 8628), the user approves the phone at `account.osmike.com/activate`
(the pairing code == the OAuth `user_code`), and the daemon receives the device **refresh token**.
From then on the daemon issues per-app access tokens. MikeVideo just consumes them.

## 7. Testing

- **Legacy still works:** existing `X-API-KEY` calls return the same data (regression guard).
- **Bearer accepted:** once the AS is live, `curl -H "Authorization: Bearer <jwt>" .../api/videos`
  returns the user's videos; a token with wrong `aud`/`iss`/expired → **401**; a `video.write` route
  with only `video.read` scope → **401/403**.
- **Never-trust-200:** a request with neither header, or a malformed Bearer, must 401 — not fall
  through to unscoped data.
- **No IdP round-trip on the Bearer path:** confirm validation is local (JWKS cached).

## 8. Checklist for the MikeVideo session
- [ ] `pyjwt[crypto]` in `mikevideo-cloud/requirements.txt`.
- [ ] `_verify_bearer` + `authenticate()` added to `server/identity.py` (keep `resolve_agent_key`).
- [ ] `server/app.py` auth dependency calls `authenticate(request)`; write routes pass `scope="video.write"`.
- [ ] Railway env `ACCOUNT_OSMIKE_ISSUER` / `OAUTH_AUDIENCE=mikevideo` set (public JWKS only, no secrets).
- [ ] `mikevideo-app` ready to read `GET /api/auth/token?aud=mikevideo` and send `Bearer` (flip on when the daemon endpoint ships).
- [ ] Deploy cloud now (safe — dual-auth); coordinate the app flip with the daemon/AS rollout.

*This is a target-state spec with a safe interim. It does not require the AS to exist to deploy the
cloud change; it makes MikeVideo the first fully OAuth-native MikeOS service the moment the AS is live.*
