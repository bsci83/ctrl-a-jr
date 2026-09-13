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
    c = slack_tools.SlackClient("xoxb-x", "#ar", http=http)
    assert c.post_message("hi")["ok"] is True
    assert http.calls[0][1].endswith("chat.postMessage")


def test_slack_api_error_raises():
    c = slack_tools.SlackClient("xoxb-x", "#ar", http=FakeHTTP({"ok": False, "error": "invalid_auth"}))
    with pytest.raises(RuntimeError, match="invalid_auth"):
        c.post_message("hi")


def test_bot_token_prefix_is_required():
    with pytest.raises(ValueError, match="xoxb-"):
        slack_tools.SlackClient("nope", "#ar", http=FakeHTTP({}))


def test_registers_one_read_and_one_mutating():
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", "#ar", http=FakeHTTP({})))
    assert reg.mutating_names() == ["slack_post_message"]
    assert "slack_lookup_user" in reg.names()


def test_post_renders_channel_and_text_for_approval():
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", "#ar", http=FakeHTTP({})))
    rendered = reg.get("slack_post_message").render_for_approval(
        {"channel": "#billing", "text": "Invoice in_1 is 30 days overdue."}
    )
    assert "#billing" in rendered and "30 days overdue" in rendered


def test_post_message_sends_the_expected_method_and_body():
    """A client posting the wrong payload used to pass — the URL was all we checked."""
    http = FakeHTTP({"ok": True, "ts": "1.2"})
    slack_tools.SlackClient("xoxb-x", "#ar", http=http).post_message("Invoice overdue")
    method, url, payload = http.calls[0]
    assert method == "POST"
    assert url.endswith("/chat.postMessage")
    assert payload == {"channel": "#ar", "text": "Invoice overdue"}


def test_lookup_user_sends_the_email_in_the_body():
    http = FakeHTTP({"ok": True, "user": {"id": "U1"}})
    slack_tools.SlackClient("xoxb-x", "#ar", http=http).lookup_user("a@b.c")
    method, url, payload = http.calls[0]
    assert method == "POST"
    assert url.endswith("/users.lookupByEmail")
    assert payload == {"email": "a@b.c"}


def test_bot_token_is_sent_as_a_bearer_header_and_never_in_the_body():
    """The token must not be able to reach a tool result or the model."""
    captured = {}

    class HeaderCapturingHTTP:
        def request(self, method, url, headers=None, json=None, timeout=None):
            captured["headers"] = headers
            captured["json"] = json
            return {"ok": True}

    slack_tools.SlackClient("xoxb-secret", "#ar", http=HeaderCapturingHTTP()).post_message("hi")
    assert captured["headers"]["Authorization"] == "Bearer xoxb-secret"
    assert "xoxb-secret" not in str(captured["json"])


def test_a_non_string_token_raises_value_error_not_attribute_error():
    """Parity with StripeClient. A None token must name the problem, not hand back
    an AttributeError traceback — found by a verification pass, not by review."""
    with pytest.raises(ValueError, match="xoxb-"):
        slack_tools.SlackClient(None, "#ar", http=FakeHTTP({}))


# ── spec invariant 4: scope is never taken from the model ────────────────────
# On the first live run the model was asked to escalate and invented a channel
# name (`ar-escalations`); Slack answered channel_not_found. The dangerous
# version of that guess is one that names a REAL channel — a private AR note
# delivered somewhere the operator never chose, under an approval granted for a
# different destination.

def test_the_model_is_not_offered_a_channel_parameter():
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", "#ar",
                                                                 http=FakeHTTP({})))
    schema = reg.get("slack_post_message").schema
    assert "channel" not in schema["properties"]
    assert schema["required"] == ["text"]


def test_a_channel_supplied_by_the_caller_is_rejected_not_honoured():
    """Belt and braces: even if something downstream passed one, the run signature
    has no place to put it."""
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", "#ar",
                                                                 http=FakeHTTP({"ok": True})))
    out = reg.get("slack_post_message").run(text="hi", channel="#somewhere-else")
    assert out.ok is False


def test_the_configured_channel_is_what_reaches_slack():
    http = FakeHTTP({"ok": True})
    slack_tools.SlackClient("xoxb-x", "C0123REAL", http=http).post_message("hi")
    assert http.calls[0][2]["channel"] == "C0123REAL"


def test_the_approver_is_shown_the_real_destination():
    """P2: the approver must see what will happen, and the destination is part of
    what happens. It is read from configuration, never echoed from model args."""
    reg = Registry()
    slack_tools.register_slack_tools(reg, slack_tools.SlackClient("xoxb-x", "#ar-real",
                                                                 http=FakeHTTP({})))
    rendered = reg.get("slack_post_message").render_for_approval({"text": "Ada owes $799"})
    assert "#ar-real" in rendered
    assert "Ada owes $799" in rendered


def test_an_empty_channel_is_refused_at_construction():
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="channel"):
            slack_tools.SlackClient("xoxb-x", bad, http=FakeHTTP({}))
