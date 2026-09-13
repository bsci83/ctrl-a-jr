"""Slack request signing and Block Kit rendering.

Signature verification is the whole security of /api/slack/interactive. That
endpoint is a public URL that records approvals and has no other authentication:
if the check is wrong, anyone on the internet can approve a real payment email by
POSTing a JSON blob. Everything below fails closed — an unset secret, a missing
header, a malformed timestamp and a bad digest are all the same "no".
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

SIGNING_SECRET_ENV = "SLACK_SIGNING_SECRET"
SIG_HEADER = "X-Slack-Signature"
TS_HEADER = "X-Slack-Request-Timestamp"
VERSION = "v0"

# Slack's own recommendation. A captured-and-replayed request is otherwise valid
# forever, and one captured "approve" replayed against a later approval id is a
# forged authorization that the signature check alone would wave through.
MAX_SKEW_SECONDS = 60 * 5

APPROVE_ACTION = "ctrla_jr_approve"
DENY_ACTION = "ctrla_jr_deny"


def verify_signature(headers, raw_body: bytes, now: float | None = None) -> bool:
    """Slack's documented v0 scheme, over the RAW body.

    The body must be the exact bytes received — re-serialising the parsed form
    reorders and re-encodes it, and the digest stops matching for reasons that
    look like a Slack outage.
    """
    secret = (os.environ.get(SIGNING_SECRET_ENV) or "").strip()
    if not secret:
        return False

    get = getattr(headers, "get", None)
    if get is None:
        return False
    signature = (get(SIG_HEADER) or get(SIG_HEADER.lower()) or "").strip()
    timestamp = (get(TS_HEADER) or get(TS_HEADER.lower()) or "").strip()
    if not signature or not timestamp:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    # Future-dated as well as stale: a clock-skewed forgery is still a forgery,
    # and abs() is what makes the window a window rather than a floor.
    if abs((time.time() if now is None else now) - sent_at) > MAX_SKEW_SECONDS:
        return False

    basestring = b"%s:%s:%s" % (VERSION.encode(), timestamp.encode(), raw_body)
    expected = VERSION + "=" + hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    """The signature a correctly configured Slack would send. Used by tests."""
    basestring = b"%s:%s:%s" % (VERSION.encode(), timestamp.encode(), raw_body)
    return VERSION + "=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()


def esc(text: object) -> str:
    """Slack's three required escapes.

    The email body quoted into these blocks came from a customer. Unescaped, a
    `<!channel>` in it pages the whole workspace and a `<https://x|Approved>`
    renders as friendly link text over a hostile URL — inside the very message
    someone is about to make a money decision from.
    """
    return (
        str("" if text is None else text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clip(text: str, limit: int = 2800) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fields(pairs: list[tuple[str, object]]) -> list[dict]:
    return [
        {"type": "mrkdwn", "text": _clip(f"*{esc(key)}*\n{esc(value)}", 1900)}
        for key, value in pairs
        if value not in (None, "")
    ]


def _email_blocks(args: dict) -> list[dict]:
    return [
        {"type": "section", "fields": _fields(
            [("To", args.get("to")), ("Subject", args.get("subject"))]
        )},
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": _clip(esc(args.get("body")))}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "This leaves your account and reaches a real customer."}
        ]},
    ]


def _slack_blocks(args: dict) -> list[dict]:
    return [
        {"type": "section", "fields": _fields([("Channel", args.get("channel"))])},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(esc(args.get("text")))}},
    ]


def _invoice_blocks(args: dict) -> list[dict]:
    return [
        {"type": "section", "fields": _fields([("Invoice", args.get("invoice_id"))])},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Stripe sends this from your account, with a payment link."}
        ]},
    ]


def _report_blocks(args: dict) -> list[dict]:
    return [
        {"type": "section", "fields": _fields([("File", args.get("filename"))])},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(esc(args.get("content")))}},
    ]


def _provider_blocks(args: dict) -> list[dict]:
    return [{"type": "section", "fields": _fields([
        ("Provider", args.get("provider")),
        ("Model", args.get("model")),
        ("Reason", args.get("reason")),
    ])}]


# Same shape as ctrl_a_jr.artifacts.RENDERERS and for the same reason: the model
# never authors what the approver reads. The display is derived in code from the
# exact arguments that execute, so it cannot drift from them.
BLOCK_RENDERERS = {
    "gmail_send": _email_blocks,
    "slack_post_message": _slack_blocks,
    "stripe_send_invoice": _invoice_blocks,
    "write_report": _report_blocks,
    "provider_switch": _provider_blocks,
}


def render_body_blocks(tool: str, args: dict | None, fallback_text: str) -> list[dict]:
    render = BLOCK_RENDERERS.get(tool) if args else None
    if render is not None:
        return render(args)
    # No args pushed, so nothing typed can be derived. The flattened artifact is
    # weaker than a typed rendering and is labelled as the fallback it is.
    return [{"type": "section", "text": {"type": "mrkdwn", "text": _clip(esc(fallback_text))}}]


def approval_blocks(
    *, approval_id: str, tool: str, args: dict | None, fallback_text: str,
    payload_hash: str, approve_url: str,
) -> list[dict]:
    """The Slack message the local agent posts with its own bot token."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": "Approval needed", "emoji": False}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*{esc(tool)}* · `{esc(approval_id)}`"}
        ]},
    ]
    blocks += render_body_blocks(tool, args, fallback_text)
    blocks.append({
        "type": "actions",
        "block_id": f"ctrla_jr:{approval_id}"[:255],
        "elements": [
            {
                "type": "button",
                "action_id": APPROVE_ACTION,
                "text": {"type": "plain_text", "text": "Approve", "emoji": False},
                "style": "primary",
                "value": approval_id,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve this?"},
                    "text": {"type": "mrkdwn", "text": "This executes for real. It cannot "
                                                       "be taken back once sent."},
                    "confirm": {"type": "plain_text", "text": "Approve"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "action_id": DENY_ACTION,
                "text": {"type": "plain_text", "text": "Deny", "emoji": False},
                "style": "danger",
                "value": approval_id,
            },
            {
                "type": "button",
                "action_id": "ctrla_jr_open",
                "text": {"type": "plain_text", "text": "Open full artifact", "emoji": False},
                "url": approve_url,
            },
        ],
    })
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"payload `{esc(payload_hash[:16])}…`"}
    ]})
    return blocks


def actor(payload: dict) -> str:
    """Who decided, from Slack's own view of the click.

    This is the reason Slack approval is worth building: the approval link proves
    only that somebody held a URL. `slack:U01ABC (brandon)` is a claim about a
    person that the workspace can corroborate.
    """
    user = payload.get("user") or {}
    user_id = str(user.get("id") or "unknown")
    name = str(user.get("username") or user.get("name") or "")
    return f"slack:{user_id} ({name})" if name else f"slack:{user_id}"
