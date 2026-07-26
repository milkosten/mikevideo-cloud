# Blockers — what MikeVideo needs from `account.osmike.com` + the daemon to be fully OAuth-native

> **UPDATE (resolved — Blocker A is DONE).** The provider shipped the web flow:
> `/.well-known/openid-configuration` now advertises `authorization_endpoint`, `authorization_code` +
> `code_challenge_methods:[S256]`, and the `mikevideo-web` client (redirect
> `https://video.osmike.com/auth/callback`) is live. **MikeVideo now implements real Authorization Code
> + PKCE** on the website (verified: "Sign in" → the MikeOS consent screen for `openid email video.read
> video.write` → `/auth/callback` → RS256 token), and the **email/password bridge was removed** — the
> cloud is OAuth Bearer (JWKS) + legacy X-API-KEY only. **Only Blocker B (the daemon token endpoint for
> the phone app) remains.** Original report kept below for the record.

---


**Audience:** whoever owns **`account.osmike.com`** (the IdP / OAuth AS) and the **on-device daemon**.
**TL;DR:** `video.osmike.com` (cloud + web) is **done and live** for "log in with email, no API keys,
share public videos." To finish the *real* OAuth vision (standard tokens everywhere, no email/password
bridge, phone app off the API key) MikeVideo is blocked on **two upstream pieces it does not own**:
a **web login flow on the provider** and a **token endpoint on the daemon**. Both are marked
*IN PROGRESS* in `mikeos-architecture/ecosystem/README.md §11`. This report is the precise contract for
each, plus what MikeVideo already ships and what it can finish without waiting.

---

## 1. Status — what `video.osmike.com` already does (shipped, verified this session)

- **Dual-auth resource server** (`server/identity.py::authenticate`, `docs/implement_oauth.md`):
  - **OAuth RS256 Bearer** validated **locally** against `account.osmike.com/oauth/jwks.json`
    (`iss`/`aud=mikevideo`/`exp`/`scope`), `sub` = `user_id`. **No per-request IdP round-trip.**
  - **Web session JWT** (email/password) as an interim browser path — validated via cached `/api/auth/me`.
  - **Legacy `X-API-KEY`** fallback (the phone app still uses it).
  - Write routes require `scope=video.write`; a present-but-invalid Bearer 401s (never falls through).
- **Website:** "Sign in with MikeOS" (email/password), a public gallery for anonymous visitors,
  private-by-default sharing with a per-video public/private toggle + share link.
- **Verified end-to-end:** minted a real device-grant token → `GET /api/videos` 200 as the correct
  user; every bad/expired/wrong-scope token → 401; legacy key still works.

**So the cloud is already a correct OAuth resource server.** The gaps below are about how *clients*
(a browser, the phone app) **obtain** a token — which is provider/daemon work.

---

## 2. BLOCKER A — provider has **no browser login flow** (no Authorization Code + PKCE)

### Evidence (live, verified)
`GET https://account.osmike.com/.well-known/openid-configuration` returns:
```
grant_types_supported:  ["urn:mikeos:params:oauth:grant-type:device", "refresh_token"]
response_types_supported: ["token"]
scopes_supported: ["openid","profile","email","video.read","video.write"]
token_endpoint_auth_methods_supported: ["none"]
# NO authorization_endpoint. NO "code" response type. NO authorization_code grant.
```
JWKS (`/oauth/jwks.json`) and the **device grant** work (a browser-less flow that takes an
`agent_key`). But a **browser cannot obtain an OAuth token** — there is no interactive
authorize/consent endpoint and no code grant.

### Consequence today
The website login is an **email/password bridge**: it posts to the IdP's `/api/auth/login`, gets a
30-day **HS256 session JWT**, and the cloud validates that via a cached `/api/auth/me`. This works and
is key-free for the user, but it is exactly the "long-lived session bearer + round-trip" pattern the
architecture wanted to retire. It cannot be removed until the provider offers a real web flow.

### What we need the provider to add
1. **`GET /oauth/authorize`** — Authorization Code flow with **PKCE (S256)**:
   `response_type=code`, `code_challenge`/`code_challenge_method`, `client_id`, `redirect_uri`,
   `scope`, `state`. Renders login/consent (reuse the existing account UI), redirects back with `?code`.
2. **`POST /oauth/token`** `grant_type=authorization_code` (+ `code_verifier`, public client / PKCE) →
   the same **RS256 access token** (`aud=mikevideo`, `scope`, `sub=user_id`, `exp`) + refresh token.
3. **A registered web client** for MikeVideo: a public `client_id` (e.g. `mikevideo-web`) with
   `redirect_uri = https://video.osmike.com/auth/callback` and allowed scopes `openid video.read
   video.write`. (`/oauth/register` and `/oauth/clients` respond 200 but the contract/`client_id`
   isn't documented — please provide the `client_id` + confirm the redirect URI is whitelisted.)
4. Discovery should then advertise `authorization_endpoint`, `code` in `response_types_supported`, and
   `authorization_code` in `grant_types_supported`, with `code_challenge_methods_supported: ["S256"]`.

### What MikeVideo does the moment this lands (≈½ day, no cloud change)
Swap the website from email/password to **Authorization Code + PKCE**, store tokens in a **secure
httpOnly cookie**, and **delete the `/api/auth/me` bridge**. The cloud already validates OAuth RS256
tokens via JWKS, so nothing server-side changes.

---

## 3. BLOCKER B — the daemon has **no token endpoint** (phone app can't drop the API key)

### Evidence
`ecosystem/README.md §6/§11` specifies (and lists as IN PROGRESS) the daemon "token agent":
```
GET https://127.0.0.1:7743/api/auth/token?aud=mikevideo&scope=video.read+video.write
    Authorization: Bearer <loopback daemon token>
 →  { access_token, token_type:"Bearer", expires_in }
```
It does not exist yet. The daemon holds the device refresh token (from pairing) and should mint
per-app access tokens via the provider's device grant (which **is** live — MikeVideo verified minting a
token from an `agent_key`).

### Consequence today
`mikevideo-app` still sends its `X-API-KEY` (the hive `agent_key`). The cloud accepts it via dual-auth,
so **nothing is broken** — but the app is not yet key-free-native.

### What we need the daemon to add
- Implement `GET /api/auth/token?aud=&scope=` on the loopback daemon: use the device refresh token to
  mint/refresh a per-`(aud,scope)` **RS256 access token** from `account.osmike.com`, cache it, return
  `{access_token, expires_in}`. (The provider's device grant + refresh already support this.)

### What MikeVideo does when this lands
Flip `mikevideo-app` to send the daemon's Bearer instead of `X-API-KEY`. This can be written **now**
behind a "use daemon token if the endpoint responds, else fall back to X-API-KEY" guard, so it
**auto-upgrades** with no coordinated deploy (see §5).

---

## 4. Not a code blocker, but a launch dependency
**10 investor accounts + 10 paired phones.** The shipped **Setup Wizard** (`com.mikeos.setup`) does this
per phone (create account with email → approve device → apps self-register), so it's 10× a
two-tap onboarding — but it must be run before handout. No `video.osmike.com` work involved.

---

## 5. What MikeVideo can finish now WITHOUT waiting (offered)
- **App OAuth STEP 2 with fallback:** `mikevideo-app` fetches the daemon Bearer if `/api/auth/token`
  answers, else keeps `X-API-KEY`. Auto-upgrades when BLOCKER B ships.
- **`DELETE /api/videos/{id}`** (`scope=video.write`): the spec lists delete; there's no route yet.
- **Web 401/expiry UX:** on an expired session, prompt re-login instead of silently showing an empty
  library. (Cookie/refresh hardening waits on BLOCKER A.)

---

## 6. Definition of done + unblock order
1. **Provider** ships `/oauth/authorize` + code+PKCE + a `mikevideo-web` client (BLOCKER A).
   → MikeVideo web switches to real SSO, drops the email/password bridge. *Fully OAuth on the web.*
2. **Daemon** ships `/api/auth/token` (BLOCKER B).
   → MikeVideo app drops `X-API-KEY` for a daemon Bearer. *Fully OAuth on the phone.*
3. Once both apps are on Bearer, **deprecate `X-API-KEY`** for MikeVideo (keep the resolve endpoint as a
   fleet-wide fallback until every service migrates, per `ACCOUNT-OSMIKE-OAUTH-PLAN.md §9`).

Until 1 & 2 land, MikeVideo is **live, key-free for users, and OAuth-ready** — the dual-auth cloud
accepts real OAuth tokens today and will need no change when the clients start sending them.
