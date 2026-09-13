"""Slack via a bot token and one POST. No SDK.

Slack's Web API returns HTTP 200 with {"ok": false} on failure, so status
codes tell you nothing — the body must be checked or every error is silent.

The destination channel is NOT a tool argument. Spec invariant 4 says scope is
never taken from the model, and on the first live run the model demonstrated why:
asked to escalate, it invented a plausible channel name (`ar-escalations`) and
Slack answered `channel_not_found`. A wrong guess that happens to name a REAL
channel is the bad version of that — a private AR message posted somewhere the
operator did not choose, with an approval the operator granted for a different
destination. The channel is configuration; only the text is the model's.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx

from ..registry import Registry, ToolSpec
from ..types import ToolResult

API = "https://slack.com/api"

# Slack rejects the whole message when one section's text exceeds 3000
# characters (invalid_blocks), so an over-long customer email would silently
# cost the operator the approval card entirely. Truncate on our side, and SAY
# the text was cut — an approver who cannot tell they are reading a fragment is
# approving something they have not seen.
SECTION_TEXT_LIMIT = 3000
_TRUNCATION_NOTICE = "\n\n[truncated — open the approval page to read the full text]"

APPROVE_ACTION_ID = "ctrla_jr_approve"
DENY_ACTION_ID = "ctrla_jr_deny"

# An HTML tag specifically — a name, then either `>` or attributes — NOT the
# blanket `<[^>]+>`. That version also swallowed `<https://evil|Click here>`,
# deleting attacker text from the card instead of neutering it, which loses the
# operator the very thing they are being asked to judge.
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")
_BLANKS = re.compile(r"\n{3,}")


def to_block_text(raw: str) -> str:
    """Make arbitrary, attacker-influenced text safe to put in a Block Kit section.

    The body of an approval card is quoted from a customer's own email. Two
    separate hazards: the local approval surface renders HTML, so a rendering
    may arrive here carrying tags (stripped, then unescaped once — never twice,
    which would resurrect an escaped tag); and Slack's own parser treats
    `&`, `<` and `>` as markup, so `<https://evil|Click here>` in a customer
    body would otherwise become a real disguised link inside the card an
    operator is about to trust.
    """
    text = _TAG.sub(" ", str(raw))
    text = html.unescape(text)
    text = _BLANKS.sub("\n\n", text).strip()
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate_for_block(text: str, limit: int = SECTION_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_TRUNCATION_NOTICE))] + _TRUNCATION_NOTICE


class _Httpx:
    def request(self, method, url, headers=None, json=None, timeout=30):
        r = httpx.request(method, url, headers=headers, json=json, timeout=timeout)
        r.raise_for_status()
        return r.json()


class SlackClient:
    def __init__(self, bot_token: str, channel: str, http: Any | None = None) -> None:
        # isinstance first: a None token must fail with the message that names the
        # problem, not an AttributeError traceback. Stripe's guard was fixed this way
        # in review; this one was missed until a verification pass caught the drift.
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-"):
            raise ValueError("expected a Slack bot token beginning xoxb-")
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError(
                "a Slack channel is required: set CTRLA_JR_SLACK_CHANNEL to a channel id "
                "(C...) or #name that the bot has been invited to. It is configuration, "
                "not something the agent may choose."
            )
        self.token = bot_token
        self.channel = channel.strip()
        self.http = http or _Httpx()

    def _call(self, endpoint: str, payload: dict) -> dict:
        body = self.http.request(
            "POST", f"{API}/{endpoint}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json=payload,
        )
        if not body.get("ok"):
            raise RuntimeError(f"slack {endpoint} failed: {body.get('error', 'unknown')}")
        return body

    def lookup_user(self, email: str) -> dict:
        return self._call("users.lookupByEmail", {"email": email})

    def post_message(self, text: str) -> dict:
        """Destination comes from configuration. There is deliberately no channel
        parameter — a caller cannot pass one, so no model output can reach it."""
        return self._call("chat.postMessage", {"channel": self.channel, "text": text})

    def approval_blocks(self, approval_id: str, tool: str, rendered: str) -> list[dict]:
        """Block Kit for one pending approval. Pure, so it can be asserted on."""
        body = truncate_for_block(to_block_text(rendered))
        return [
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"*ctrl-a JR wants to run* `{tool}`"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"approval `{approval_id}`"}]},
            {"type": "actions", "elements": [
                # The button VALUE is the approval id and nothing else. The
                # decision lives in the action_id, which the model never sees and
                # cannot author — a value carrying "approve" would let anyone who
                # can forge a payload name their own outcome.
                {"type": "button", "action_id": APPROVE_ACTION_ID,
                 "style": "primary", "value": approval_id,
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": DENY_ACTION_ID,
                 "style": "danger", "value": approval_id,
                 "text": {"type": "plain_text", "text": "Deny"}},
            ]},
        ]

    def post_approval_request(self, approval_id: str, tool: str, rendered: str) -> dict:
        """Ask for an approval in Slack.

        NOT an agent tool, and deliberately not registered: a model able to post
        its own approval request is self-approval with one extra hop. This is
        called by RemoteApprover, from inside the gate. The destination is still
        configuration — there is no channel parameter here either.
        """
        return self._call("chat.postMessage", {
            "channel": self.channel,
            # Fallback for notifications and clients that cannot render blocks.
            "text": f"ctrl-a JR needs approval for {tool} ({approval_id})",
            "blocks": self.approval_blocks(approval_id, tool, rendered),
        })


def register_slack_tools(registry: Registry, client: SlackClient) -> None:
    def _wrap(fn):
        def inner(**kwargs):
            try:
                return ToolResult(True, json.dumps(fn(**kwargs), default=str))
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, "", str(exc))
        return inner

    registry.register(ToolSpec(
        name="slack_lookup_user",
        description="Find a Slack user by email address.",
        schema={"type": "object", "properties": {"email": {"type": "string"}},
                "required": ["email"]},
        mutating=False,
        run=_wrap(lambda email: client.lookup_user(email)),
    ))
    registry.register(ToolSpec(
        name="slack_post_message",
        description="Post a note to the team's escalation channel. Use this to escalate a "
                    "high-value overdue invoice after the recovery email is sent. You do "
                    "not choose the channel — it is configured by the operator. Write only "
                    "the message text.",
        schema={"type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]},
        mutating=True,
        run=_wrap(lambda text: client.post_message(text)),
        # The approver sees the REAL destination, read from configuration — not a
        # channel echoed back from the model's own arguments.
        render=lambda text: f"Post to {client.channel}:" + chr(10) + chr(10) + text,
    ))
