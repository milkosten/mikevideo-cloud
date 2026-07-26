# Proposal — real logins for 10 investor phones in 5 days (no API keys)

**The ask:** in ~5 days, hand 10 phones to 10 investors. Each investor uses MikeVideo (and MikeOS)
**without ever touching an API key** — they log in like a normal product (email), and their videos are
their own. This doc grounds the plan in what *actually exists today* (verified in the code, not the
roadmap docs) and lays out the two provisioning options so we can pick.

---

## 1. The reality today (verified in code)

**Good news: a real account system already exists and is live.** We do **not** need to build the full
OAuth 2.0 / OIDC Authorization Server (`account.osmike.com`) to hit this deadline — that is the
long-term ideal, not the requirement.

**IdP = `mikeoscomputers`** (live on Railway, Node/Express/TS + Postgres; the service behind
account.osmike.com). It already has:

| Capability | Endpoint (live today) | Returns |
|---|---|---|
| Create account (email/pw, **no phone**) | `POST /api/auth/register {email,password}` | `{token, user}` — token = **JWT (HS256), 30-day** |
| Log in | `POST /api/auth/login {email,password}` | `{token, user}` |
| Who am I | `GET /api/auth/me` (Bearer JWT) | `{id, email}` |
| Pair a device | `POST /api/devices/pair/request` → `POST /api/devices/pair/activate` (Bearer JWT) | links `device_id` ↔ `user_id` in `linked_devices` |
| App mints its own key | `POST /api/mikeos/agents {deviceId, app}` (device-authed) | `{agent_key, user_id, …}` |
| Resolve key → user | `GET /api/mikeos/agents/resolve/{key}` | `{valid, user_id, …}` |

There is even a live web UI (Login / Register / Dashboard / Activate) on the IdP.

**The phone app is ALREADY key-free.** MikeVideo self-registers on launch: it POSTs to the on-device
daemon (`/api/agents/register {app}`, loopback), the daemon uses the phone's `device_id` to mint an
`agent_key` at the IdP, and caches it in `hive_credentials.json`. **No human ever types a key; there is
no login screen in the app.** It works the moment the phone is *paired to a user account*.

Crucial detail: the JWT's user is the **same `user_id`** the app's agent-key resolves to. So a web
session (JWT) and the phone app (agent key) see the **same library** — our `WHERE user_id=$1` scoping
is untouched either way.

---

## 2. The two real gaps (all that stands between us and 10 investors)

1. **The website (`video.osmike.com`) still asks the user to "paste your X-API-KEY."** This is the
   ugliness. Everything to replace it with a normal email login already exists on the IdP.
2. **Fresh phones don't self-provision.** There is **no on-phone Setup Wizard**; today, linking a phone
   to a user is a manual laptop-browser dance, and the system has effectively been used with a single
   hardcoded user. For 10 phones + 10 investors that manual path doesn't scale to the deadline.

Everything else (per-app registration, hive presence, sync, encode, transcripts, AI) is already
automatic once a phone is paired.

---

## 3. Proposed plan — two tracks

### Track 1 — "Sign in with MikeOS" on the website  (kills the API key)
*Self-contained: `mikevideo-cloud` + `web/index.html`. No dependency on the phones.*

- **Cloud (`server/identity.py`, `app.py`):** extend the single `_auth(request)` chokepoint to accept a
  user **Bearer JWT** in addition to the legacy `X-API-KEY`. Validate the JWT by calling the IdP's
  `GET /api/auth/me` (round-trip, same pattern as today's `resolve_agent_key`; no secret sharing) →
  `user_id`. Keep `X-API-KEY` working as an internal fallback (the phone app still uses it).
- **Cloud proxy:** add thin `POST /api/auth/login` + `POST /api/auth/register` that forward to the IdP
  (so the browser talks only to `video.osmike.com` — no cross-origin/CORS problem).
- **Web:** replace the "paste key" modal with a proper **Sign in / Create account** screen (email +
  password) and a logout. Store the JWT in localStorage, send it as `Authorization: Bearer`.

**Result:** the website is a normal email login. Zero keys. ~1.5 days.

### Track 2 — Provision the 10 phones  (each investor pre-logged-in)
*See §4 for the two ways to do this — that's the open decision.*

Either way, the end state per phone is: the phone's `device_id` is linked to that investor's account,
so MikeVideo (and the other MikeOS apps) self-register and show **that investor's own private
library** — no key, no login screen on the phone.

---

## 4. The decision: how to provision the phones

### Option A — Pre-provision via a script  *(recommended for the deadline)*
You run a small script over USB (all 10 phones on your desk), which, per phone, automates exactly the
steps that already exist:

```
register(investorN@…, password)         # POST /api/auth/register  → JWT
code = pair/request(deviceName)          # POST /api/devices/pair/request
pair/activate(code, Bearer JWT)          # POST /api/devices/pair/activate → device_id
write device_id → phone:~/.mikeos/device-id   (adb, into the daemon)
launch MikeVideo → it self-registers → ✓ shows that investor's library
```

- **Investor experience:** picks up a phone that is **already set up and logged into their account**.
  Opens MikeVideo → their videos. If they open the website, they sign in with the email/password we set.
- **Effort/risk:** ~1–1.5 days for the script + provisioning run. **Low risk** — it only orchestrates
  live, proven endpoints; nothing new to design. Fits 5 days comfortably.
- **Cons:** not self-service; you (admin) set it up. Fine for a controlled investor handout.

### Option B — Build the on-phone Setup Wizard  *(the "real" long-term solution)*
A first-boot Android app (`com.mikeos.setup`, spec'd but **not built**): "Welcome to MikeOS → create
account → approve this phone," done by the investor themselves on the device.

- **Investor experience:** true self-service; powers on, creates account, approves phone.
- **Effort/risk:** a **new Android app** to design, build, and test against the daemon + IdP in 5 days,
  on top of Track 1. **Higher risk** for the deadline; if it slips, the handout slips.
- **Best treated as the post-demo follow-up** once the investor handout has de-risked the launch.

**Recommendation:** **Option A for the 5-day handout, Option B as the immediate follow-up.** Option A
uses only endpoints that are already live and hands investors a phone that "just works."

### Accounts: separate vs shared
**Recommendation: a separate real account per investor** (`investorN@…`). Their uploads stay private to
them and they can log into the website with their own email — a realistic product demo. A single shared
demo account mixes everyone's uploads and has no privacy story; only worth it if we explicitly want a
communal demo library.

---

## 5. Five-day schedule (assuming Option A + separate accounts)

| Day | Work |
|---|---|
| **1** | Track 1 cloud: `authenticate()` (Bearer JWT via `/api/auth/me` + X-API-KEY fallback); `/api/auth/login`+`/register` proxy. Deploy to the box, verify. |
| **2** | Track 1 web: Sign-in / Create-account / logout UI; remove the key paste. Verify register → library end-to-end in a browser. |
| **3** | Track 2 script: provision **one** test phone end-to-end (new account → paired → MikeVideo scoped to it, upload works). |
| **4** | Provision **all 10** phones + 10 accounts; per-phone smoke test (own library, upload, web login). |
| **5** | Buffer/polish: MikeVideo branding, a printed card per investor (email + set-password link), full dry-run. |

---

## 6. Explicitly NOT doing now (and why it's safe)
- **The full OAuth 2.0 AS on `account.osmike.com`** (JWKS, device-grant RFC 8628, auth-code+PKCE,
  refresh rotation). It's the right long-term design (`mikeos-architecture/docs/ACCOUNT-OSMIKE-OAUTH-PLAN.md`),
  but it is **not required** for "investors log in with email, no keys." The live email/password + JWT
  system already delivers that. When the AS is built later, the cloud swap is ~30 lines in the same
  `_auth` chokepoint we're touching now — so this plan is a stepping stone, not throwaway.

## 7. Open decisions (for you)
1. **Provisioning:** Option A (script, recommended) or Option B (on-phone wizard)?
2. **Accounts:** separate per investor (recommended) or one shared demo account?
3. **Passwords:** we set them and hand each investor a card, or send a "set your password" link?

*Nothing has been built or changed yet — this is the proposal. Say go and I start Day 1 (Track 1 cloud),
which is useful under either provisioning option.*
