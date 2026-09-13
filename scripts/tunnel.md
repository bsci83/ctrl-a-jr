# Exposing the approval surface over a Cloudflare tunnel

The approval page is the authorization boundary of this product. Anyone who can
`POST /resolve` can authorize a real email and a real Stripe invoice. Read the
whole page before you run the command.

## 1. Start the agent

```
ctrl-a-jr run
```

It prints one line:

```
Approvals: http://127.0.0.1:8765/?t=<token>
  (generated approval token; set CTRLA_JR_APPROVAL_TOKEN to pin your own)
```

The token is printed **once**, at startup. To pin a stable one across restarts —
which you want if you are going to paste the link into a chat and restart the
agent mid-demo:

```
export CTRLA_JR_APPROVAL_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Keep it out of git. It is a bearer credential: holding it *is* the authority.

## 2. Open the tunnel

```
cloudflared tunnel --url http://localhost:8765 --http-host-header localhost:8765
```

`--http-host-header` is **not optional.** The server refuses any request whose
`Host` is not `localhost:<port>` or `127.0.0.1:<port>` — that check is the DNS
rebinding defence, and it predates the token. Without the flag cloudflared
forwards the public hostname as `Host` and every request comes back `403`.

cloudflared prints a URL like `https://<random-words>.trycloudflare.com`.

## 3. Build the link you actually hand out

Take the token from step 1 and the host from step 2:

```
https://<random-words>.trycloudflare.com/?t=<token>
```

The bare tunnel URL returns **404**, deliberately — an unauthenticated caller
should not learn that an approval surface lives there. The first authenticated
GET sets an `HttpOnly; SameSite=Strict` cookie, so the Approve/Deny buttons work
from then on without the query string.

Scripted callers can use `Authorization: Bearer <token>` instead:

```
curl -H "Authorization: Bearer $CTRLA_JR_APPROVAL_TOKEN" https://<host>.trycloudflare.com/
```

## What the tunnel is and is not

- **The tunnel URL is public.** Unlisted is not secret. It is guessable in
  principle, it is visible to anyone the link is forwarded to, and it does not
  expire when you look away. The token is the only thing standing between the
  internet and the Approve button.
- **The token is a bearer credential.** Pasting the full `?t=` link into a chat,
  a screenshot, or a screen share hands over approval authority. Share the
  tunnel URL and the token by separate channels if you can.
- **Anyone holding the link can approve a real send.** There is one operator
  and no per-user identity; the log records that *someone* authenticated
  approved, not who.
- **Rotate by restarting.** Ctrl-C the agent and start it again: a new token is
  generated (unless `CTRLA_JR_APPROVAL_TOKEN` is pinned), and every previously
  shared link is dead. Stop the tunnel when the demo ends.
- Failed authentication attempts are appended to the activity log as
  `approval_auth_failed`, with the path and whether a token was offered. The
  supplied value is never logged.
