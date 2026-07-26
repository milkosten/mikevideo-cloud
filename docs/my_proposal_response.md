# Response to `my_proposal.md`

First — this is good work. You verified the account system against the actual code, you correctly
spotted that the **phone app is already key-free** (daemon mints the credential; no login screen), that
the JWT user and the agent-key user are the **same `user_id`**, and that **separate accounts per
investor** is the right call. All of that is correct and useful. Keep it.

But the proposal is built on a map that's a few weeks out of date, and it under-reads what MikeOS is.
Please read **`mikeos-architecture/ecosystem/README.md`** (new, canonical) — it's the full picture. The
corrections below are the ones that change your plan.

---

## 1. MikeOS is a whole OS, not "a website + some Android phones"

We ship our **own de-Googled ROM** (LineageOS-based — actively compiling right now), our **own launcher**
(`com.mikeos.launcher`, MikeOS Home, already shipped and set as the default HOME — no Google search bar),
our **own on-device brain** (the daemon), our **own IdP** (account.osmike.com), a **boot cinematic**, and
a **first-boot Setup Wizard**. Your app is one autonomous agent among ~33 running on that OS. This matters
because it changes both of your "gaps."

## 2. Your Gap #2 is already closed: the Setup Wizard **is built and shipped**

Your proposal says the on-phone Setup Wizard is *"spec'd but not built"* and treats it as the risky
"Option B." **It's built** — `com.mikeos.setup`, shipped to the store (v2), verified on device: welcome →
create account (email, **no phone**) → **approve this device** → choose apps → done → MikeOS Home. That IS
the self-service, non-technical onboarding, and it's the OAuth **device-authorization consent** in
disguise. So:

- **Provisioning 10 phones is not a manual laptop/adb dance.** Each investor (or you, at handout) runs the
  wizard once per phone: email + approve. The daemon pairs, apps self-register, MikeVideo shows that
  investor's library. Your "Option A script" is fine as an **operational convenience** for a controlled
  handout (drive the same steps in bulk), but frame it as *ops tooling around the shipped wizard*, not as
  a substitute for a thing that "doesn't exist." It exists.

## 3. Your Gap #1 (website) — right problem, wrong auth mechanism

Replacing "paste your X-API-KEY" with a real email login on `video.osmike.com` — **yes, do it.** But
**not** by validating a 30-day HS256 session JWT via a per-request `GET /api/auth/me` round-trip. That
just swaps one long-lived bearer for another and keeps the round-trip anti-pattern.

We are standing up the **real OAuth 2.0 / OIDC provider on `account.osmike.com` right now** (device grant
+ **JWKS** + refresh + scopes + audiences). So the correct integration — which is *less* code than your
`/api/auth/me` proxy — is in **`implement_oauth.md`** (already in this folder):

- Validate `Authorization: Bearer <JWT>` **locally** against `account.osmike.com/oauth/jwks.json` (RS256,
  check `iss`/`aud=mikevideo`/`exp`/`scope`). **No round-trip to the IdP per request.** `sub` = the same
  `user_id` you scope by today.
- Keep `X-API-KEY` as the fallback (**dual-auth**) so nothing breaks and the phone app keeps working.
- For the website: "Sign in with MikeOS" via the OAuth **Authorization Code + PKCE** flow (tokens in a
  secure cookie), or at minimum send the OAuth access token as `Bearer`. Same chokepoint, ~40 lines.

This is the same effort you scoped, pointed at the standard we're actually building — not throwaway.

## 4. "Effectively one hardcoded user" — no; multi-user is the design

Every cloud is **user-scoped** (`WHERE user_id = …`), each device pairs to its own account, and the
daemon mints per-device credentials. Ten investors = ten accounts = ten private libraries, *automatically*
— that's not something to build, it's how the ecosystem already works. The only reason it's felt like one
user is that there's been one owner so far.

## 5. So what to actually do (and the deadline)

- **Now (the 1-hour item):** implement the **cloud dual-auth** exactly per `implement_oauth.md` (Bearer
  JWT via JWKS + X-API-KEY fallback) and deploy. It's safe and additive. This is the piece you were asked
  to ship; if it's not deployed within the hour, it will be done for you.
- **Website:** "Sign in with MikeOS" using the OAuth token (not the `/me` shortcut). Kills the key paste.
- **Phones:** use the shipped **Setup Wizard** for onboarding; a bulk provisioning script is welcome as
  ops tooling around it, not as a replacement. Separate account per investor — agreed.
- **Don't skip OAuth "to hit the deadline."** The provider is being built in parallel; your dual-auth code
  works against it the moment it's live and falls back to X-API-KEY until then, so there is **no schedule
  risk** in doing it right.

## 6. Read these
- `mikeos-architecture/ecosystem/README.md` — who we are, the machines/APIs, and **how apps on a new
  phone integrate** (§7 is you).
- `mikevideo-cloud/docs/implement_oauth.md` — your exact, do-it-now checklist.
- `mikeos-architecture/docs/ACCOUNT-OSMIKE-OAUTH-PLAN.md` — the OAuth design.

You did the hard part (reading the real code). Just point it at the OS we actually have, and the "5 days,
10 investors, zero keys" goal is comfortably in reach — with proper OAuth, not around it.
