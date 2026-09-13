# Runbook — from credentials to a scored run

Everything below is ~25 minutes, most of it waiting on browser tabs.

## 1. Two browser tabs (~15 min)

**Gmail app password** — `myaccount.google.com/apppasswords`
- Requires 2-Step Verification to be ON (app passwords do not exist without it)
- Generate, copy the 16 characters into `GMAIL_APP_PASSWORD` in `.env.local`
- Put the address in `GMAIL_ADDRESS`
- **Then enable IMAP**: Gmail → Settings → Forwarding and POP/IMAP → *Enable IMAP*.
  Off by default on some accounts. `doctor` checks SMTP and IMAP separately
  because they fail independently.

**Slack bot token** — `api.slack.com/apps`
- Create New App → From scratch
- OAuth & Permissions → **Bot Token Scopes**: `chat:write`, `users:read.email`
- Install to Workspace → copy `xoxb-...` into `SLACK_BOT_TOKEN`
- `/invite @yourbot` in the channel you will post to — it cannot post to a
  channel it is not in

## 2. Verify before building anything

```
ctrl-a-jr doctor
```

Five independent checks; one failure never hides another. Do not move on until
it says **all green**. Exit code is 0 only when everything passes, so it is
scriptable.

## 3. Seed, run, score

```
ctrl-a-jr fixtures seed     # 3 customers, invoices at 31/12/3 days overdue
ctrl-a-jr run               # approve this one
ctrl-a-jr run               # DENY this one
ctrl-a-jr eval
```

**Expect the first surprise at `fixtures seed`.** Its tests all use a fake
transport, and the one genuinely unverified thing is whether Stripe accepts a
past `due_date` with `collection_method=send_invoice`. If it rejects, switch to
`days_until_due` — the invoices are then *open* rather than *overdue*, which
weakens the framing slightly and breaks nothing.

Do at least two runs, one approved and one denied, so `denial_handling` has
something to score and `runs` is greater than 1.

`ctrl-a-jr fixtures teardown` removes only objects tagged `ctrl_a_jr_fixture`.
Untagged customers are never touched.

## 4. What to record

The approval page at `127.0.0.1:8765` shows the whole run — the live strip, then
the rendered email, then Approve/Deny. That single window is the demo.

The moment worth capturing is the email arriving in a real inbox, and the run
where you **deny** and the agent reports it and stops. Showing it *not* act is
rarer than showing it act.

Then `ctrl-a-jr eval` for the numbers, and `verdict.json` as the artifact.
