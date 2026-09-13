"""The approval surface: one local page, stdlib only.

The approver must see the ARTIFACT, not the arguments (spec §5 P2). Approving
a JSON blob is not approval, so the page renders the finished email and
escapes it — the body is attacker-influenced content from a customer thread.
"""

from __future__ import annotations

import html
import os
import secrets
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .activity import log_action, read_log
from .approval import ApprovalStore
from .artifacts import STYLE as ARTIFACT_STYLE
from .artifacts import render_artifact
from .types import ApprovalRecord, Decision

_STYLE = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#faf9f7;color:#1a1a1a}
main{max-width:720px;margin:0 auto;padding:32px 20px}
h1{font-size:18px;margin:0 0 24px}
.card{background:#fff;border:1px solid #e3e0db;border-radius:10px;padding:20px;margin-bottom:16px}
.tool{font:12px ui-monospace,monospace;color:#8a6d3b;background:#fdf6e3;
      padding:2px 8px;border-radius:20px;display:inline-block;margin-bottom:12px}
pre{white-space:pre-wrap;word-wrap:break-word;background:#f6f5f3;padding:14px;
    border-radius:6px;margin:0 0 16px;font:13px/1.55 ui-monospace,monospace}
button{font:14px system-ui;padding:9px 20px;border-radius:6px;border:0;
       cursor:pointer;margin-right:8px}
.ok{background:#1a7f37;color:#fff}.no{background:#cf222e;color:#fff}
.empty{color:#6b6b6b;text-align:center;padding:48px}
.strip{display:flex;flex-direction:column;gap:6px;margin-bottom:24px}
.act{display:flex;align-items:center;gap:10px;font:13px ui-monospace,monospace;
     color:#4a4a4a;background:#fff;border:1px solid #eeebe6;border-radius:6px;
     padding:7px 12px}
.act .ico{width:16px;text-align:center;flex:0 0 16px}
.act .det{color:#8a8a8a;margin-left:auto;font-size:12px}
.act.err{color:#cf222e;border-color:#f5c2c0;background:#fff6f5}
.act.gate{color:#8a6d3b;border-color:#f0e0b8;background:#fdf6e3;font-weight:600}
.runid{font:11px ui-monospace,monospace;color:#9a9a9a;margin:0 0 8px}
"""


# The approval surface is the authorization boundary of the whole product. Bound
# to localhost it needed no secret; put behind a public tunnel so a judge can
# drive it, an unauthenticated POST /resolve lets anyone holding the URL approve
# a real email and a real Stripe invoice. The token is what makes the public URL
# an address rather than a capability.
TOKEN_ENV = "CTRLA_JR_APPROVAL_TOKEN"
TOKEN_PARAM = "t"
TOKEN_COOKIE = "ctrla_jr_token"


def _token_from_cookie(header: str | None) -> str | None:
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
    except CookieError:
        # A malformed Cookie header is an unauthenticated request, not a crash.
        return None
    morsel = jar.get(TOKEN_COOKIE)
    return morsel.value if morsel is not None else None


def _supplied_tokens(path: str, headers) -> list[str]:
    """Every place a caller may present the token, in the order they are accepted.

    Three channels because one pasted link has to work end to end: `?t=` carries
    the first GET, the cookie it sets carries the form POST that follows, and the
    bearer header serves anything scripted.
    """
    found: list[str] = []
    query = parse_qs(urlsplit(path).query)
    found.extend(v for v in query.get(TOKEN_PARAM, []) if v)

    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        found.append(auth[7:].strip())

    cookie = _token_from_cookie(headers.get("Cookie"))
    if cookie:
        found.append(cookie)
    return found


def token_matches(supplied: str, expected: str) -> bool:
    """Constant-time compare. `==` on a secret leaks its prefix one byte at a
    time to anyone who can measure the response, and over a public tunnel that
    measurement is available to the whole internet. Compared as bytes because
    compare_digest rejects non-ASCII str."""
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


ICONS = {
    "tool_call": "*", "tool_refused": "x", "approval_requested": "?",
    "approval_resolved": "!", "payload_mismatch": "!", "model_turn": ">",
    "provider_transport_failure": "x", "round_limit_reached": "-",
    "content_block_dropped": "-",
}

# Newest last, so the eye lands where the agent currently is.
STRIP_LIMIT = 14


def render_strip(events: list[dict]) -> str:
    """Live feed of what the agent is doing.

    Shape borrowed from ctrl-a's activity-strip: compact rows rather than a log
    dump, an icon per kind, and a distinct style for the state that matters. It
    renders nothing when there is nothing — an empty strip is better than a strip
    announcing its own emptiness.
    """
    if not events:
        return ""
    rows = []
    for e in events[-STRIP_LIMIT:]:
        kind = str(e.get("event", "?"))
        tool = str(e.get("tool") or "")
        cls = "act"
        if kind in ("tool_refused", "payload_mismatch", "provider_transport_failure"):
            cls += " err"
        elif kind == "approval_requested":
            cls += " gate"

        if kind == "model_turn":
            label, detail = f"thinking ({e.get('model', '?')})", f"round {e.get('round', '?')}"
        elif kind == "tool_call":
            label = tool
            detail = "ok" if e.get("ok") else f"failed: {str(e.get('error', ''))[:40]}"
            if not e.get("ok"):
                cls += " err"
        elif kind == "approval_requested":
            label, detail = f"{tool} — waiting for you", "gate"
        elif kind == "approval_resolved":
            label, detail = tool, str(e.get("decision", ""))
        elif kind == "tool_refused":
            label, detail = tool, str(e.get("reason", "refused"))
        else:
            label, detail = kind, tool

        rows.append(
            f'<div class="{cls}"><span class="ico">{html.escape(ICONS.get(kind, "-"))}</span>'
            f"<span>{html.escape(label)}</span>"
            f'<span class="det">{html.escape(detail)}</span></div>'
        )
    run = events[-1].get("run_id")
    head = f'<p class="runid">run {html.escape(str(run))}</p>' if run else ""
    return head + '<div class="strip">' + "".join(rows) + "</div>"


def render_page(records: list[ApprovalRecord], events: list[dict] | None = None) -> str:
    strip = render_strip(events or [])
    if not records:
        # Only claim nothing is happening when nothing is. With a live strip the
        # page is the agent working, not a waiting room.
        body = "" if strip else '<p class="empty">Nothing waiting for you.</p>'
    else:
        cards = []
        for r in records:
            cards.append(
                f'<div class="card"><div class="tool">{html.escape(r.tool)}</div>'
                # render_artifact escapes every value it interpolates; it is the
                # only place trusted markup enters this page.
                f"{render_artifact(r.tool, r.args)}"
                f'<form method="post" action="/resolve" style="display:inline">'
                f'<input type="hidden" name="id" value="{html.escape(r.id)}">'
                f'<button class="ok" name="decision" value="approved">Approve</button>'
                f'<button class="no" name="decision" value="denied">Deny</button>'
                f"</form></div>"
            )
        body = "".join(cards)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ctrl-a JR — approvals</title>"
        "<meta http-equiv='refresh' content='2'>"
        f"<style>{_STYLE}{ARTIFACT_STYLE}</style></head><body><main>"
        f"<h1>{'Waiting for your authorization' if records else 'ctrl-a JR'}</h1>"
        f"{strip}{body}</main></body></html>"
    )


class WebApprover:
    """Blocks the agent until a human clicks. One operator, one decision at a time."""

    def __init__(self, store: ApprovalStore, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None) -> None:
        self.store = store
        self.host, self.port = host, port
        env_token = (os.environ.get(TOKEN_ENV) or "").strip()
        # Generated rather than defaulted-off: a secret you have to opt into is a
        # secret that is absent the one time the surface is exposed.
        self.token = token or env_token or secrets.token_urlsafe(32)
        self.token_is_generated = not (token or env_token)
        self._decisions: dict[str, Decision] = {}
        self._event = threading.Event()
        self._httpd: HTTPServer | None = None
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def authed_url(self) -> str:
        """The link the operator actually opens. Without `?t=` the page 404s."""
        return f"{self.url}?{TOKEN_PARAM}={self.token}"

    def start(self) -> None:
        approver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console clean
                pass

            def _authorised(self) -> bool:
                """True only on an exact, constant-time token match.

                Returns the verdict; the caller decides the response, because GET
                and POST have to drain and answer differently.
                """
                supplied = _supplied_tokens(self.path, self.headers)
                return any(token_matches(s, approver.token) for s in supplied)

            def _reject_unauthorised(self) -> None:
                # 404, not 401/403: over a public tunnel a 401 confirms that an
                # approval surface lives here and invites a guessing campaign. An
                # unauthenticated caller learns only that the path is nothing.
                #
                # The path and whether a token was offered are logged. The token
                # VALUE never is — a near-miss written to the evidence file would
                # put attacker-controlled guesses, and one day the real secret via
                # a copy-paste, into the artifact we hand to reviewers.
                log_action(
                    "approval_auth_failed",
                    method=self.command,
                    path=urlsplit(self.path).path,
                    token_supplied=bool(_supplied_tokens(self.path, self.headers)),
                )
                body = b"Not Found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                host = (self.headers.get("Host") or "").strip()
                if host not in (f"127.0.0.1:{approver.port}", f"localhost:{approver.port}"):
                    self.send_response(403)
                    self.end_headers()
                    return
                if not self._authorised():
                    self._reject_unauthorised()
                    return
                page = render_page(approver.store.pending(), read_log()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # The pasted link carries the token once; the cookie carries it to
                # the POST that follows. HttpOnly keeps it out of reach of any
                # script that lands on the page; SameSite=Strict means a hostile
                # page cannot make the browser spend it on a cross-site POST.
                self.send_header(
                    "Set-Cookie",
                    f"{TOKEN_COOKIE}={approver.token}; Path=/; HttpOnly; SameSite=Strict",
                )
                # A framed approval page can be clickjacked without the attacker
                # ever reading an approval id — the Host check passes from inside
                # an iframe. One operator click on a decoy approves a real send.
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "frame-ancestors 'none'; default-src 'none'; "
                                 "style-src 'unsafe-inline'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self):
                host = (self.headers.get("Host") or "").strip()
                if host not in (f"127.0.0.1:{approver.port}", f"localhost:{approver.port}"):
                    # Drain the body before responding: closing the connection with
                    # unread bytes still in flight races the client's own write and
                    # intermittently resets the socket instead of delivering the 403.
                    length = int(self.headers.get("Content-Length", 0))
                    if length:
                        self.rfile.read(length)
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                if self.path.split("?")[0] != "/resolve":
                    self.send_response(404)
                    self.end_headers()
                    return
                # /resolve is the ONLY mutating route on this server, and this is
                # the authorization check for it. It sits above the point where a
                # decision is recorded, so an unauthenticated POST cannot reach
                # _decisions at all — a 404 that still approved the send would be
                # the exact hole the token exists to close.
                if not self._authorised():
                    self._reject_unauthorised()
                    return
                approval_id = form.get("id", [""])[0]
                decision = form.get("decision", ["denied"])[0]
                approver._decisions[approval_id] = (
                    Decision.APPROVED if decision == "approved" else Decision.DENIED
                )
                approver._event.set()
                self.send_response(303)
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                # The token travels back in the redirect as well as the cookie.
                # A browser that refuses the cookie (third-party-cookie blocking
                # applies to a tunnelled origin) would otherwise bounce the
                # operator from a successful approval straight into a 404.
                self.send_header("Location", f"/?{TOKEN_PARAM}={approver.token}")
                self.end_headers()

        self._httpd = HTTPServer((self.host, self.port), Handler)
        # When port=0 the OS picks one; record it so .url is correct.
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        # Release anyone blocked in decide() BEFORE tearing the server down, so a
        # waiting agent thread cannot be stranded on a decision that can no longer
        # arrive.
        self._stopped = True
        self._event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def decide(self, record: ApprovalRecord) -> Decision:
        while record.id not in self._decisions:
            if self._stopped:
                # The approval surface is gone. Nobody can say yes, so the answer
                # is no — an unanswerable request must never become an approval.
                #
                # But record that this was a MACHINE denial. A human denying a
                # send and the server shutting down are the same Decision and
                # very different evidence: without this marker, Ctrl-C with an
                # approval pending flips denial_handling from inconclusive to
                # pass, and the agent gets credit for behaviour nobody observed.
                log_action("approval_auto_denied", approval_id=record.id,
                           tool=record.tool, reason="approver_stopped")
                return Decision.DENIED
            self._event.wait(timeout=0.25)
            self._event.clear()
        return self._decisions.pop(record.id)
