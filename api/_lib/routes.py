"""Every endpoint, as a pure function of the request.

Auth per endpoint, and each one fails closed:

  POST /api/approvals        bearer CTRLA_JR_PUSH_TOKEN   (the local agent pushes)
  GET  /api/approvals/<id>   bearer CTRLA_JR_PUSH_TOKEN   (the local agent polls)
  GET  /api/approve/<id>     HMAC nonce in ?k=            (the human opens)
  POST /api/decide           HMAC nonce in the form       (the human clicks)
  POST /api/slack/interactive Slack v0 signature          (Slack clicks)

Decisions are recorded, never executed. The local agent re-derives the payload
hash from what is about to run and compares it before anything leaves the
building (spec §5 P3); this app never sees a credential and cannot send anything.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from . import slack
from .http import (
    Response,
    bearer_ok,
    html_response,
    json_response,
    not_found,
    nonce_ok,
)
from .http import approve_nonce as _nonce
from .page import render_approval
from .payload import payload_hash
from .sanitize import to_text
from .store import Record, StoreUnavailable, TursoStore

# Tests set this to a MemoryStore. Production never does — see store.MemoryStore.
STORE = None

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DECISIONS = ("approved", "denied")
PUBLIC_BASE_ENV = "CTRLA_JR_PUBLIC_BASE_URL"


def _store():
    return STORE if STORE is not None else TursoStore.from_env()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tail_id(path: str) -> str | None:
    """Last path segment, for Vercel's `[id].py` dynamic routes."""
    tail = urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]
    return tail if ID_RE.match(tail) else None


def _approve_url(approval_id: str, headers=None) -> str:
    """Absolute link to the approval page, nonce included.

    Falls back to the Host of the push request so a deploy that forgot
    CTRLA_JR_PUBLIC_BASE_URL still produces a usable link. Slack rejects a
    relative URL on a button, so an empty base would break the card rather than
    degrade it.
    """
    base = (os.environ.get(PUBLIC_BASE_ENV) or "").strip().rstrip("/")
    if not base and headers is not None:
        host = _host(headers)
        if host:
            base = f"https://{host}"
    return f"{base}/api/approve/{approval_id}?k={_nonce(approval_id)}"


def approve_url(approval_id: str, headers=None) -> str:
    """Public name for `_approve_url`, for the run endpoints in `runs.py`.

    Same function, not a second one: an approval link built two ways is an
    approval link that can be built WRONG one way, and a nonce mismatch reads as
    "unknown approval" rather than as a bug.
    """
    return _approve_url(approval_id, headers)


def _host(headers) -> str:
    getter = getattr(headers, "get", None)
    value = getter("Host") if getter is not None else None
    return (value or "").strip()


def _unavailable(exc: StoreUnavailable) -> Response:
    return json_response(503, {"error": str(exc)})


def push(method: str, path: str, headers, body: bytes) -> Response:
    """POST /api/approvals — the local agent registers a pending approval."""
    if method != "POST":
        return json_response(405, {"error": "method not allowed"})
    if not bearer_ok(headers):
        # 404 rather than 401, and the store is never touched: an unauthenticated
        # push must not be able to create a row, or an attacker could plant an
        # artifact for a human to approve.
        return not_found()
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return json_response(400, {"error": "body must be JSON"})
    if not isinstance(data, dict):
        return json_response(400, {"error": "body must be a JSON object"})

    approval_id = str(data.get("approval_id") or "")
    tool = str(data.get("tool") or "")
    given_hash = str(data.get("payload_hash") or "")
    # The local RemoteApprover sends `rendered` and `artifact`; `rendered_artifact`
    # is the name in the design brief. `rendered` wins when they disagree, because
    # it is the string the local approval page showed. Accepting one name only
    # would fail closed on every real push — the feature would be dead, quietly.
    artifact = str(
        data.get("rendered")
        or data.get("artifact")
        or data.get("rendered_artifact")
        or ""
    )
    created_at = str(data.get("created_at") or _now())
    args = data.get("args")

    if not ID_RE.match(approval_id) or not tool or not given_hash or not artifact:
        return json_response(400, {"error": "approval_id, tool, payload_hash and "
                                            "rendered (or artifact) are required"})
    if args is not None:
        if not isinstance(args, dict):
            return json_response(400, {"error": "args must be an object"})
        # With args present the Slack rendering is derived from them, so they have
        # to be the same bytes the hash covers. Otherwise a caller could push
        # honest arguments alongside a hash for something else and Slack would
        # show one thing while the agent executes another.
        if payload_hash(tool, args) != given_hash:
            return json_response(400, {"error": "payload_hash does not match args"})

    rec = Record(
        approval_id=approval_id, tool=tool, payload_hash=given_hash,
        rendered_artifact=artifact, created_at=created_at,
        args_json=json.dumps(args) if args is not None else None,
    )
    try:
        stored = _store().put(rec)
    except StoreUnavailable as exc:
        return _unavailable(exc)

    try:
        approve_url = _approve_url(stored.approval_id, headers)
    except LookupError as exc:
        return json_response(503, {"error": str(exc)})

    blocks = slack.approval_blocks(
        approval_id=stored.approval_id,
        tool=stored.tool,
        args=args,
        fallback_text=to_text(stored.rendered_artifact),
        payload_hash=stored.payload_hash,
        approve_url=approve_url,
    )
    return json_response(200, {
        "ok": True,
        "approval_id": stored.approval_id,
        "approve_url": approve_url,
        # The agent posts this itself, with its own bot token. This app is not
        # given one — a credential that never arrives cannot leak from here.
        "slack_blocks": blocks,
        **stored.status_payload(),
    })


def poll(method: str, path: str, headers, body: bytes) -> Response:
    """GET /api/approvals/<id> — the local agent asks what the human said."""
    if method != "GET":
        return json_response(405, {"error": "method not allowed"})
    if not bearer_ok(headers):
        return not_found()
    approval_id = _tail_id(path)
    if approval_id is None:
        return not_found()
    try:
        rec = _store().get(approval_id)
    except StoreUnavailable as exc:
        return _unavailable(exc)
    if rec is None:
        # Never invent `pending` for an id nobody pushed. A polling agent would
        # wait on it forever, which is the safe direction, but the log would show
        # a request that does not exist.
        return json_response(404, {"error": "unknown approval"})
    # payload_hash is echoed straight from the row, never recomputed here: the
    # client refuses an approval whose echoed hash differs from the one it
    # pushed, which is what stops one approval authorising a different payload.
    return json_response(200, {
        "approval_id": rec.approval_id,
        "tool": rec.tool,
        "payload_hash": rec.payload_hash,
        **rec.status_payload(),
    })


def approve_page(method: str, path: str, headers, body: bytes) -> Response:
    """GET /api/approve/<id>?k=… — what the human reads before deciding."""
    if method != "GET":
        return json_response(405, {"error": "method not allowed"})
    approval_id = _tail_id(path)
    if approval_id is None:
        return not_found()
    supplied = parse_qs(urlsplit(path).query).get("k", [""])[0]
    if not nonce_ok(approval_id, supplied):
        return not_found()
    try:
        rec = _store().get(approval_id)
    except StoreUnavailable as exc:
        return _unavailable(exc)
    if rec is None:
        return not_found()
    return html_response(200, render_approval(rec, supplied))


def decide(method: str, path: str, headers, body: bytes) -> Response:
    """POST /api/decide — the web page records a decision."""
    if method != "POST":
        return json_response(405, {"error": "method not allowed"})
    form = parse_qs(body.decode("utf-8", "replace"))
    approval_id = form.get("id", [""])[0]
    decision = form.get("decision", [""])[0]
    supplied = form.get("k", [""])[0]
    if not ID_RE.match(approval_id or "") or not nonce_ok(approval_id, supplied):
        # The nonce is also the CSRF defence. Without it any page on the internet
        # could POST this form for an id it guessed, and the operator's browser
        # would approve a real send on its behalf.
        return not_found()
    if decision not in DECISIONS:
        return json_response(400, {"error": "decision must be approved or denied"})
    try:
        rec, _changed = _store().decide(approval_id, decision, "link", _now())
    except StoreUnavailable as exc:
        return _unavailable(exc)
    if rec is None:
        return not_found()
    # Re-render rather than redirect: _changed is False when someone else already
    # decided, and the page must show the decision that actually stands, not the
    # one just clicked.
    return html_response(200, render_approval(rec, supplied))


def slack_interactive(method: str, path: str, headers, body: bytes) -> Response:
    """POST /api/slack/interactive — a Block Kit button click."""
    if method != "POST":
        return json_response(405, {"error": "method not allowed"})
    # Verified BEFORE the body is parsed, and before the store is touched at all.
    # An unverified request must not be able to reach a write, so the check sits
    # above everything rather than beside it.
    if not slack.verify_signature(headers, body):
        return not_found()

    form = parse_qs(body.decode("utf-8", "replace"))
    raw = form.get("payload", [""])[0]
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return json_response(400, {"error": "payload is not JSON"})
    if not isinstance(data, dict):
        return json_response(400, {"error": "payload must be an object"})

    actions = data.get("actions") or []
    action = actions[0] if actions and isinstance(actions[0], dict) else {}
    action_id = str(action.get("action_id") or "")
    approval_id = str(action.get("value") or "")
    if action_id == slack.APPROVE_ACTION:
        decision = "approved"
    elif action_id == slack.DENY_ACTION:
        decision = "denied"
    else:
        # Includes the "Open full artifact" link button, which Slack also posts.
        # Not a decision, so it must not become one.
        return json_response(200, {"ok": True, "ignored": action_id})
    if not ID_RE.match(approval_id):
        return json_response(400, {"error": "action value is not an approval id"})

    decided_by = slack.actor(data)
    try:
        rec, changed = _store().decide(approval_id, decision, decided_by, _now())
    except StoreUnavailable as exc:
        return _unavailable(exc)
    if rec is None:
        return json_response(200, {
            "replace_original": False,
            "text": f"ctrl-a JR: no approval {approval_id} — nothing recorded.",
        })
    if changed:
        text = f"*{slack.esc(rec.decision)}* by {slack.esc(rec.decided_by)} — {slack.esc(rec.tool)}"
    else:
        text = (
            f"Already *{slack.esc(rec.decision)}* by {slack.esc(rec.decided_by)}. "
            "A decision is final; this click changed nothing."
        )
    return json_response(200, {"replace_original": True, "text": text})
