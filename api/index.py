"""The single WSGI entrypoint Vercel's Python runtime loads.

Vercel's current Python builder wants ONE app, not a handler class per file:
a deploy with only `api/<route>.py` shims fails the build with "No python
entrypoint found". The per-file shims are kept because the test suite drives
them directly as BaseHTTPRequestHandler subclasses, and because they are the
readable statement of what each route is; this module is the adapter that
lets the same route functions serve real traffic.

Routing is explicit rather than derived from the filesystem. A table that has
to be edited to add a route is a table someone reads when adding one — and an
unmatched path returning 404 here is the same answer an unauthenticated
request gets, so probing for undeployed endpoints tells an attacker nothing.
"""

from __future__ import annotations

import os
import sys

# Vercel does not guarantee the project root on sys.path, and `from api._lib`
# would then fail at cold start as an opaque 500.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api._lib.http import SECURITY_HEADERS, Response, json_response  # noqa: E402
from api._lib.routes import (  # noqa: E402
    approve_page,
    decide,
    poll,
    push,
    slack_interactive,
)

MAX_BODY = 1 << 20  # 1 MiB; a rendered artifact is kilobytes


def _route_for(method: str, path: str):
    """Longest-prefix match, most specific first."""
    if path.startswith("/api/slack/interactive"):
        return slack_interactive
    if path.startswith("/api/decide"):
        return decide
    if path.startswith("/api/approve/"):
        return approve_page
    if path.startswith("/api/approvals"):
        # POST /api/approvals pushes; GET /api/approvals/<id> polls.
        return push if method == "POST" else poll
    return None


class _Headers(dict):
    """The route functions call .get(name) case-insensitively, as email.message
    does. WSGI hands us uppercased HTTP_ keys, so normalise once here rather
    than teaching five routes about two header shapes."""

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)


def _headers_from_environ(environ) -> _Headers:
    out = _Headers()
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            out[key[5:].replace("_", "-").lower()] = value
    for key, name in (("CONTENT_TYPE", "content-type"), ("CONTENT_LENGTH", "content-length")):
        if environ.get(key):
            out[name] = environ[key]
    return out


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    if query:
        path = f"{path}?{query}"

    route = _route_for(method, path)
    if route is None:
        resp: Response = json_response(404, {"error": "not found"})
    else:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            resp = json_response(413, {"error": "body too large"})
        else:
            # Slack signs the RAW bytes. Read them once, unmodified — any
            # re-encoding here makes every signature verification fail.
            body = environ["wsgi.input"].read(length) if length else b""
            try:
                resp = route(method, path, _headers_from_environ(environ), body)
            except Exception:  # noqa: BLE001
                # No traceback, no exception text: a failed Turso call raises
                # carrying the request it attempted, and that is one refactor
                # away from returning the Authorization header to a caller.
                resp = json_response(500, {"error": "internal error"})

    headers = {**SECURITY_HEADERS, **resp.headers}
    headers.setdefault("Content-Length", str(len(resp.body)))
    start_response(f"{resp.status} ", list(headers.items()))
    return [resp.body]
