# Deploying the approval surface

The local agent keeps every credential — the Stripe key, the Gmail app password,
the Slack bot token. This Vercel app holds none of them. It stores pending
approvals and the decisions humans make about them, and the agent reaches it
outbound only: it pushes an approval, then polls for the answer. Nothing ever
connects inbound to the operator's machine, and this app cannot send anything.

## 1. What gets deployed

| Route | File | Auth |
|---|---|---|
| `POST /api/approvals` | `api/approvals/index.py` | `Authorization: Bearer $CTRLA_JR_PUSH_TOKEN` |
| `GET /api/approvals/<id>` | `api/approvals/[id].py` | `Authorization: Bearer $CTRLA_JR_PUSH_TOKEN` |
| `GET /api/approve/<id>?k=…` | `api/approve/[id].py` | HMAC nonce `k`, derived from the push token |
| `POST /api/decide` | `api/decide.py` | the same nonce, in the form body |
| `POST /api/slack/interactive` | `api/slack/interactive.py` | Slack `v0` request signature |

`api/_lib/` is shared code. Vercel does not route files or directories whose
name starts with `_`, so it ships in the bundle without becoming an endpoint.

## 2. Create the database

Turso, reached over plain HTTP (`/v2/pipeline`) with `httpx`. No driver is
installed — the project's dependency budget is four packages.

```bash
turso db create ctrl-a-jr-approvals
turso db show ctrl-a-jr-approvals --url      # libsql://ctrl-a-jr-approvals-<org>.turso.io
turso db tokens create ctrl-a-jr-approvals   # write-capable — treat as a secret
```

The table is created on demand; the DDL lives in `api/_lib/store.py` and every
call sends it as a `CREATE TABLE IF NOT EXISTS` ahead of its own statements,
because a serverless function has no deploy hook to run migrations from.

```sql
CREATE TABLE IF NOT EXISTS approvals (
  approval_id       TEXT PRIMARY KEY,
  tool              TEXT NOT NULL,
  payload_hash      TEXT NOT NULL,
  rendered_artifact TEXT NOT NULL,
  args_json         TEXT,
  created_at        TEXT NOT NULL,
  decision          TEXT NOT NULL DEFAULT 'pending',
  decided_by        TEXT,
  decided_at        TEXT
);
```

## 3. Environment variables

Set all four in Vercel → Project → Settings → Environment Variables, for
Production **and** Preview (a preview deployment with no signing secret rejects
every Slack click, which is the right behaviour but looks like an outage).

| Variable | Where it comes from |
|---|---|
| `CTRLA_JR_PUSH_TOKEN` | You generate it: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. The same value goes in the local agent's `.env.local`. |
| `SLACK_SIGNING_SECRET` | api.slack.com/apps → your app → **Basic Information** → App Credentials → Signing Secret. |
| `TURSO_DATABASE_URL` | `turso db show … --url` above. `libsql://` or `https://` both work. |
| `TURSO_AUTH_TOKEN` | `turso db tokens create …` above. |

Optional: `CTRLA_JR_PUBLIC_BASE_URL` (e.g. `https://ctrl-a-jr.vercel.app`). It
only sets the base of the approval link returned to the agent; without it the
`Host` header of the push request is used.

Never commit any of these. `.env.local` is already gitignored.

## 4. Fix the Content-Security-Policy before the first deploy

`vercel.json` currently sends `form-action 'none'` for **every** path. The
Approve button is a real form POST to `/api/decide`, and a browser enforces the
intersection of that header and the page's own policy — with the repo-root rule
still applying to `/api/*`, clicking Approve silently does nothing and no error
appears anywhere.

Scope the strict policy to the static page and let the functions set their own:

```json
"headers": [
  {
    "source": "/((?!api/).*)",
    "headers": [
      { "key": "Content-Security-Policy", "value": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" },
      { "key": "X-Frame-Options", "value": "DENY" },
      { "key": "Referrer-Policy", "value": "no-referrer" }
    ]
  }
]
```

The functions already send `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and a
page CSP of `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`.

> Not yet verified against a real deployment. If `api/approvals/index.py` does
> not answer `/api/approvals`, add a rewrite rather than renaming the file:
> `{"source": "/api/approvals", "destination": "/api/approvals/index"}`.

## 5. Deploy

```bash
vercel link            # once, in the repo root
vercel deploy          # preview
vercel deploy --prod   # production
```

Add an `alias` array to `vercel.json` if this project will be redeployed often —
`vercel --prod` only auto-claims the team-suffixed URL, and the bare
`<project>.vercel.app` alias otherwise stays pinned to the first production
deploy.

## 6. Point Slack at it

1. api.slack.com/apps → your app → **Interactivity & Shortcuts**.
2. Turn **Interactivity** on.
3. **Request URL**: `https://<app>.vercel.app/api/slack/interactive`
4. Save. Slack sends a signed test request; a 404 back means
   `SLACK_SIGNING_SECRET` is wrong or unset — the endpoint fails closed and
   answers 404 rather than 401, so an unauthenticated caller learns nothing
   about what lives there.

The approval card itself is built and posted by the local agent
(`src/ctrl_a_jr/tools/slack_tools.py`) with its own bot token. Its buttons carry
`action_id` `ctrla_jr_approve` / `ctrla_jr_deny` and the bare `approval_id` as
the button `value`; `/api/slack/interactive` reads exactly that shape.

## 7. Smoke test

```bash
BASE=https://<app>.vercel.app
TOKEN=<CTRLA_JR_PUSH_TOKEN>

# push — prints the approval link, with its nonce
curl -sS -X POST "$BASE/api/approvals" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"approval_id":"ap_smoketest01","tool":"gmail_send","payload_hash":"'"$(python -c 'print("a"*64)')"'","rendered":"<div class=\"art\">smoke test</div>","created_at":"2026-09-13T12:00:00Z"}'

# poll — {"status":"pending", ...}
curl -sS "$BASE/api/approvals/ap_smoketest01" -H "Authorization: Bearer $TOKEN"

# these two must both be 404, and must not create or change anything
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/approvals" -d '{}'
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/slack/interactive" -d 'payload={}'
```

Open the `approve_url` from the push response, click Approve, then poll again:
`status` becomes `approved` and `decided_by` is `link`. Click the Slack button
instead and `decided_by` names the person — `slack:U01ABC (brandon)`. That
difference is the point of wiring Slack up at all.

## 8. What the deployment does not protect against

- **The push token is the master key.** Anyone holding it can push approvals and
  can derive the nonce for any approval id, so they can open and approve any
  approval whose id they know. Rotate it by changing the Vercel env var and the
  agent's `.env.local` together; in-flight approval links stop working.
- **The decision this API returns is unsigned.** A hostile or typo-squatted
  `CTRLA_JR_APPROVAL_API` can answer `approved`. The agent re-checks the payload
  hash, but an attacker who can impersonate the API already saw that hash in the
  push. Pin the URL.
- **Deleting a row deletes the evidence.** Anyone with the Turso token can edit
  the `approvals` table directly. The authoritative record of what happened stays
  the local activity log, which this app never writes to.
