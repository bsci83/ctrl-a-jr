"""doctor's Slack check must verify the grant, not the key.

Regression test for a live false green on 2026-09-13: an app configuration
token (xoxe., scopes identify + app_configurations:*) passed `auth.test` and
doctor reported "ok". It could not post a message. Same shape as the Composio
pre-flight four days earlier — the key authenticated, the grant was useless.
"""

import httpx
import pytest

from ctrl_a_jr import doctor


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token-under-test")

    def install(body, headers=None):
        monkeypatch.setattr(httpx, "post",
                            lambda *a, **k: FakeResponse(body, headers))
    return install


def test_a_real_bot_token_with_chat_write_passes(slack):
    slack({"ok": True, "team": "ctrl-a.jr", "user": "ctrla.jr", "bot_id": "B123"},
          {"x-oauth-scopes": "chat:write,users:read"})
    c = doctor.check_slack()
    assert c.ok is True
    assert "chat:write granted" in c.detail


def test_an_app_configuration_token_is_rejected(slack):
    """The exact token that produced the false green."""
    slack({"ok": True, "team": "ctrl-a.jr", "user": "ctrla.jr"},
          {"x-oauth-scopes": "identify,app_configurations:read,app_configurations:write"})
    c = doctor.check_slack()
    assert c.ok is False
    assert "not a BOT token" in c.detail


def test_a_bot_token_without_chat_write_is_rejected(slack):
    """Authenticates, is a bot, still cannot do the one thing it is for."""
    slack({"ok": True, "team": "t", "user": "u", "bot_id": "B123"},
          {"x-oauth-scopes": "users:read,channels:read"})
    c = doctor.check_slack()
    assert c.ok is False
    assert "lacks chat:write" in c.detail


def test_a_missing_scope_header_is_not_a_pass(slack):
    """Unconfirmed is not confirmed. Absence of evidence, again."""
    slack({"ok": True, "team": "t", "user": "u", "bot_id": "B123"}, {})
    c = doctor.check_slack()
    assert c.ok is False
    assert "unconfirmed" in c.detail


def test_an_outright_auth_failure_still_reports_the_error(slack):
    slack({"ok": False, "error": "invalid_auth"})
    c = doctor.check_slack()
    assert c.ok is False
    assert "invalid_auth" in c.detail
