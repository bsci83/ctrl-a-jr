"""Request plumbing shared by every function in api/.

A route is a pure function `(method, path, headers, body) -> Response`. The
BaseHTTPRequestHandler subclass Vercel's Python runtime expects is generated from
it by `make_handler`, so the whole surface is testable in-process without a
running Vercel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler

# A body larger than this is not a real approval push; reading an unbounded
# Content-Length into memory is a free way to burn the function's whole heap.
MAX_BODY = 256 * 1024

PUSH_TOKEN_ENV = "CTRLA_JR_PUSH_TOKEN"

# Same posture as the local server: no referrer, never framed. A framed approval
# page can be clickjacked without the attacker ever learning an approval id.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

# form-action 'self' is load-bearing: the Approve button is a real form POST to
# /api/decide. The repo-root vercel.json sends `form-action 'none'` for every
# path, and a browser enforces the INTERSECTION of the two policies — with that
# header still applying to /api/*, Approve silently does nothing. docs/deploy.md
# carries the required vercel.json change.
PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def json_response(status: int, payload: dict) -> Response:
    body = json.dumps(payload).encode("utf-8")
    return Response(status, body, {"Content-Type": "application/json; charset=utf-8"})


def html_response(status: int, markup: str) -> Response:
    return Response(
        status,
        markup.encode("utf-8"),
        {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": PAGE_CSP},
    )


def not_found() -> Response:
    """404, not 401/403.

    Copied deliberately from the local server: on a public URL a 401 confirms
    that an approval surface lives here and invites a guessing campaign. A caller
    without the right secret learns only that the path is nothing.
    """
    return Response(404, b"Not Found", {"Content-Type": "text/plain; charset=utf-8"})


def _header(headers, name: str) -> str:
    """Case-insensitive lookup that works for both a real HTTPMessage and a dict."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if value is None:
        for key in headers:
            if key.lower() == name.lower():
                value = headers[key]
                break
    return (value or "").strip()


def _expected(env_name: str) -> str:
    return (os.environ.get(env_name) or "").strip()


def bearer_ok(headers, env_name: str = PUSH_TOKEN_ENV) -> bool:
    """Constant-time bearer check that fails closed when the secret is unset.

    An unset secret must never mean "allow": a deploy that forgot the env var
    would otherwise publish an unauthenticated write endpoint onto the internet.
    """
    expected = _expected(env_name)
    if not expected:
        return False
    auth = _header(headers, "Authorization")
    if not auth.lower().startswith("bearer "):
        return False
    supplied = auth[7:].strip()
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def approve_nonce(approval_id: str) -> str:
    """Per-approval secret embedded in the approval link.

    /api/approve/<id> and /api/decide have no session and no login — the link IS
    the capability. Without this, the whole surface is protected by a 48-bit
    approval id: anyone who guesses or is shown one could approve a real payment
    email, and any page on the internet could blind-POST /api/decide for an id it
    guessed. Derived from the push token so the local agent, which already holds
    that token, can build the link itself without a second shared secret.
    """
    secret = _expected(PUSH_TOKEN_ENV)
    if not secret:
        raise LookupError(f"{PUSH_TOKEN_ENV} is not set")
    mac = hmac.new(secret.encode("utf-8"), f"approve:{approval_id}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def nonce_ok(approval_id: str, supplied: str | None) -> bool:
    if not supplied:
        return False
    try:
        expected = approve_nonce(approval_id)
    except LookupError:
        return False
    return hmac.compare_digest(supplied, expected)


def make_handler(route):
    """Wrap a route function in the handler class Vercel's Python runtime loads."""

    class handler(BaseHTTPRequestHandler):  # noqa: N801 — the name Vercel looks for
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # keep function logs free of request lines
            pass

        def _dispatch(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                resp = json_response(413, {"error": "body too large"})
            else:
                body = self.rfile.read(length) if length else b""
                try:
                    resp = route(self.command, self.path, self.headers, body)
                except Exception:
                    # Deliberately no traceback and no exception text in the
                    # response: a failed Turso call raises with the request it
                    # tried to make, and that is one refactor away from putting
                    # the Authorization header into a body an attacker reads.
                    resp = json_response(500, {"error": "internal error"})
            self._write(resp)

        def _write(self, resp: Response) -> None:
            self.send_response(resp.status)
            for key, value in {**SECURITY_HEADERS, **resp.headers}.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            self.wfile.write(resp.body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch

    return handler
