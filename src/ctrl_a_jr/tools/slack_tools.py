"""Slack via a bot token and one POST. No SDK.

Slack's Web API returns HTTP 200 with {"ok": false} on failure, so status
codes tell you nothing — the body must be checked or every error is silent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..registry import Registry, ToolSpec
from ..types import ToolResult

API = "https://slack.com/api"


class _Httpx:
    def request(self, method, url, headers=None, json=None, timeout=30):
        r = httpx.request(method, url, headers=headers, json=json, timeout=timeout)
        r.raise_for_status()
        return r.json()


class SlackClient:
    def __init__(self, bot_token: str, http: Any | None = None) -> None:
        # isinstance first: a None token must fail with the message that names the
        # problem, not an AttributeError traceback. Stripe's guard was fixed this way
        # in review; this one was missed until a verification pass caught the drift.
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-"):
            raise ValueError("expected a Slack bot token beginning xoxb-")
        self.token = bot_token
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

    def post_message(self, channel: str, text: str) -> dict:
        return self._call("chat.postMessage", {"channel": channel, "text": text})


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
        description="Post to a Slack channel. Use this to escalate a high-value overdue "
                    "invoice to a human teammate after the recovery email is sent.",
        schema={"type": "object",
                "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                "required": ["channel", "text"]},
        mutating=True,
        run=_wrap(lambda channel, text: client.post_message(channel, text)),
        render=lambda channel, text: f"Post to {channel}:\n\n{text}",
    ))
