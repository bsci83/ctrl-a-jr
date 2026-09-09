import pytest
from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import slack_tools


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self.response


def test_post_message_returns_ok():
    http = FakeHTTP({"ok": True, "ts": "1.2"})
    c = slack_tools.SlackClient("xoxb-x", http=http)
    assert c.post_message("#alerts", "hi")["ok"] is True
    assert http.calls[0][1].endswith("chat.postMessage")


def test_slack_api_error_raises():
    c = slack_tools.SlackClient("xoxb-x", http=FakeHTTP({"ok": False, "error": "invalid_auth"}))
    with pytest.raises(RuntimeError, match="invalid_auth"):
        c.post_message("#alerts", "hi")


def test_bot_token_prefix_is_required():
    with pytest.raises(ValueError, match="xoxb-"):
        slack_tools.SlackClient("nope", http=FakeHTTP({}))


def test_registers_one_read_and_one_mutating():
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", http=FakeHTTP({})))
    assert reg.mutating_names() == ["slack_post_message"]
    assert "slack_lookup_user" in reg.names()


def test_post_renders_channel_and_text_for_approval():
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", http=FakeHTTP({})))
    rendered = reg.get("slack_post_message").render_for_approval(
        {"channel": "#billing", "text": "Invoice in_1 is 30 days overdue."}
    )
    assert "#billing" in rendered and "30 days overdue" in rendered
