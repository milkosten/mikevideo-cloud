# MikeVideo — IMPLEMENT OAuth now (drop X-API-KEY handout). Deadline: today.

**Why this is not optional.** In 5 days, **10 phones go to 10 different real users (investors)**. There
is no "there's only one user" — there will be ten, and more. The point of OAuth here is NOT the user
count; it's that the current credential is a **long-lived, unscoped, unstandard API key**. We are moving
MikeVideo to **standard OAuth 2.0 Bearer tokens issued by `account.osmike.com`**, validated locally.
This must be **flawless and non-technical** — the user never sees a token or a key.

> You have **1 hour** to implement the cloud side below. If it's not deployed, it will be implemented
> for you. It's ~40 lines + one dep. Do it now.

Design context (read once): `mikeos-architecture/docs/ACCOUNT-OSMIKE-OAUTH-PLAN.md` and the sibling
`integrate_with_account_osmike_com.md`. This file is the **actionable checklist**.

---

## What `account.osmike.com` now exposes (the OAuth provider — LIVE)

Standards-compliant OAuth 2.0 / OIDC. You only need two things: the **JWKS** (to verify tokens) and the
token **claims** (to authorize).

- `GET https://account.osmike.com/.well-known/openid-configuration` — discovery (issuer, jwks_uri).
- `GET https://account.osmike.com/oauth/jwks.json` — RS256 **public** keys. Cache them.
- Access token = **RS256 JWT**. Claims MikeVideo checks:
  - `iss` = `https://account.osmike.com`
  - `sub` = the **user_id** (identical value to what `resolve_agent_key` returns today — your data scoping is unchanged)
  - `aud` includes `mikevideo`
  - `scope` includes what the request needs: **`video.read`** (list/stream), **`video.write`** (upload/ingest/delete)
  - `exp` (short-lived, ~1h), `device_id`, `azp`

Tokens are minted on-device by the daemon (via a device grant that reuses the phone's existing pairing —
so the *user* just signs in once and approves the phone; no keys, ever). **You don't build that** — you
only **validate** the resulting Bearer JWT.

## STEP 1 — `mikevideo-cloud`: become a dual-auth resource server (~40 lines)

`requirements.txt`: add `pyjwt[crypto]`.

`server/identity.py` — add local JWT validation + a single `authenticate(request)` that prefers Bearer
and falls back to the legacy `X-API-KEY` (so nothing breaks during rollout):

```python
import os, logging
from typing import Optional
import jwt
from jwt import PyJWKClient

log = logging.getLogger(__name__)
ACCOUNT_ISSUER  = os.environ.get("ACCOUNT_OSMIKE_ISSUER", "https://account.osmike.com")
ACCOUNT_JWKS    = os.environ.get("ACCOUNT_OSMIKE_JWKS_URL", f"{ACCOUNT_ISSUER}/oauth/jwks.json")
SERVICE_AUD     = os.environ.get("OAUTH_AUDIENCE", "mikevideo")
_jwks = PyJWKClient(ACCOUNT_JWKS, cache_keys=True, lifespan=3600)

def _verify_bearer(token: str) -> Optional[dict]:
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"],
                          issuer=ACCOUNT_ISSUER, audience=SERVICE_AUD,
                          options={"require": ["exp", "iss", "sub"]})
    except Exception as e:
        log.warning("Bearer rejected: %s", e); return None

def _has_scope(claims: dict, needed: str) -> bool:
    return needed in (claims.get("scope") or "").split()

async def authenticate(request, scope: Optional[str] = None) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        claims = _verify_bearer(auth[7:].strip())
        if claims and (scope is None or _has_scope(claims, scope)):
            return str(claims["sub"])          # sub == user_id
        return None                            # present-but-bad Bearer must 401, not fall through
    key = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
    return await resolve_agent_key(key)        # unchanged legacy path (keep resolve_agent_key as-is)
```

`server/app.py` — point the auth dependency at `authenticate` (the current code is ~line 87–88):
```python
from .identity import authenticate
# read routes:
return await authenticate(request)
# write routes (upload / ingest / delete) — require the write scope:
return await authenticate(request, scope="video.write")
```
Keep every house rule: never-trust-200 (no user_id → 401), parameterized SQL, the public HLS/MP4 segment
routes stay as they are.

Railway env on the `mikevideo-cloud` service (public JWKS only — **no secrets**):
`ACCOUNT_OSMIKE_ISSUER=https://account.osmike.com`, `OAUTH_AUDIENCE=mikevideo`.

**This is safe to deploy immediately** — Bearer is honored when present, X-API-KEY still works, so there
is zero coordinated-deploy risk.

## STEP 2 — `mikevideo-app`: send Bearer from the daemon (flip when the daemon token endpoint ships)

Replace the `X-API-KEY = HiveIdentity.agentKey` header with a short-lived Bearer token fetched from the
on-device daemon:
```kotlin
// GET https://127.0.0.1:7743/api/auth/token?aud=mikevideo&scope=video.read+video.write
//   Authorization: Bearer <loopback daemon token>  -> { access_token, expires_in }
suspend fun mikeVideoToken(): String? =
    daemonGet("/api/auth/token?aud=mikevideo&scope=video.read+video.write")?.optString("access_token")
// then: .header("Authorization", "Bearer $token")   // instead of X-API-KEY
// cache until ~expires_in; on 401, refetch once.
```
The daemon's `/api/auth/token` is being built. **Until it ships, keep sending `X-API-KEY`** — the cloud's
dual-auth accepts it, so you can deploy STEP 1 now and flip the app later with no coordination.

## STEP 3 — Test (prove it works)
1. Mint a real token from the provider (device grant):
   ```
   curl -s -X POST https://account.osmike.com/oauth/token \
     -H 'Content-Type: application/json' \
     -d '{"grant_type":"urn:mikeos:params:oauth:grant-type:device","agent_key":"<a valid hive key>","audience":"mikevideo","scope":"video.read video.write"}'
   ```
   → `{ access_token, token_type:"Bearer", expires_in:3600, refresh_token, scope }`.
2. Call MikeVideo with it: `curl -H "Authorization: Bearer <access_token>" https://video.osmike.com/api/videos` → the user's videos.
3. Negative tests (must 401): no header; malformed Bearer; wrong `aud`; expired token; a `video.write` route with only `video.read` scope.
4. Regression: existing `X-API-KEY` calls still return the same data.

## Checklist (deploy STEP 1 within the hour)
- [ ] `pyjwt[crypto]` in `requirements.txt`
- [ ] `_verify_bearer` + `authenticate()` in `server/identity.py` (keep `resolve_agent_key`)
- [ ] `server/app.py` auth dep → `authenticate(request)`; write routes pass `scope="video.write"`
- [ ] Railway env `ACCOUNT_OSMIKE_ISSUER` + `OAUTH_AUDIENCE=mikevideo`
- [ ] Deploy + run the STEP 3 tests; paste results
- [ ] (app) ready to switch to Bearer once the daemon `/api/auth/token` ships

**The user never touches a key. One email signup + one device approval, and MikeVideo works for all 10
users via standard, expiring, scoped Bearer tokens.** Ship STEP 1 now.
