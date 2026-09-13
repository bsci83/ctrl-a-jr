"""The conversational run endpoints, driven through the real WSGI app.

Every claim that a tool did NOT execute is asserted with a spy on the tool
implementation, not with a status code. A status code is what the code under
test says happened; the spy is what happened.

No network: the model client is scripted, Turso is stdlib sqlite3 behind the
pipeline wire format, and Slack is a recording fake.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from tests.test_api_harness import PUSH_TOKEN, SIGNING_SECRET, install

from api import index  # noqa: E402
from api._lib import runs as runs_mod  # noqa: E402
from api._lib import slack as slack_lib  # noqa: E402
from api._lib.agent import AgentBuild, MissingCredential, build_agent  # noqa: E402
from ctrl_a_jr.approval import ApprovalStore  # noqa: E402
from ctrl_a_jr.guard import Guard  # noqa: E402
from ctrl_a_jr.registry import Registry, ToolSpec  # noqa: E402
from ctrl_a_jr.resume import SuspendingApprover  # noqa: E402
from ctrl_a_jr.runstate import RunStore  # noqa: E402
from ctrl_a_jr.tools.slack_tools import SlackClient  # noqa: E402
from ctrl_a_jr.types import ToolResult  # noqa: E402
from tests.turso_fake import FakeTurso  # noqa: E402

SYSTEM = "you are the shop agent"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class Block:
    def __init__(self, type_, text=None, name=None, input_=None, id_="tu_1"):
        self.type, self.text, self.name = type_, text, name
        self.input, self.id = input_ or {}, id_


class Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class Scripted:
    """A model whose responses are a queue shared across HTTP requests."""

    provider = "test"
    model = "fake"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.messages_seen = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def create(self, system, messages, tools):
        self.messages_seen.append(json.loads(json.dumps(messages)))
        if not self.responses:
            return Msg([Block("text", text="nothing further")])
        return self.responses.pop(0)


class Spy:
    """Records every actual execution of a tool implementation."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(True, "sent")

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeSlackHttp:
    def __init__(self):
        self.posts = []

    def request(self, method, url, headers=None, json=None, timeout=30):
        self.posts.append({"url": url, "payload": json})
        return {"ok": True, "ts": "1700000000.000200"}


SEND_ARGS = {"to": "a@b.c", "subject": "Your quote", "body": "Full detail: $420.00"}


def _build(send_spy, read_spy=None, client=None, slack_http=None):
    registry = Registry()
    registry.register(ToolSpec(
        "quote_price", "read the menu", {"type": "object", "properties": {}}, False,
        read_spy or Spy(ToolResult(True, '{"total":"$420.00"}')),
    ))
    registry.register(ToolSpec(
        "gmail_send", "send mail", {"type": "object", "properties": {}}, True,
        send_spy, render=lambda **kw: f"To: {kw.get('to')}\n\n{kw.get('body')}",
    ))
    slack_client = SlackClient("xoxb-test", "C0SHOP", http=slack_http or FakeSlackHttp())
    return AgentBuild(
        registry=registry,
        guard=Guard(registry, ApprovalStore(), SuspendingApprover()),
        client=client or Scripted(),
        slack=slack_client,
        system=SYSTEM,
        max_rounds=8,
    )


@pytest.fixture
def wired(monkeypatch):
    """Routes, run store, approval store and agent, all in-process."""
    approvals = install(monkeypatch)
    run_store = RunStore(FakeTurso())
    send_spy = Spy()
    client = Scripted()
    slack_http = FakeSlackHttp()
    build = _build(send_spy, client=client, slack_http=slack_http)
    monkeypatch.setattr(runs_mod, "RUN_STORE", run_store)
    monkeypatch.setattr(runs_mod, "AGENT", build)
    return {
        "approvals": approvals, "store": run_store, "spy": send_spy,
        "client": client, "slack": slack_http, "build": build,
    }


# --------------------------------------------------------------------------
# the real WSGI app, not a shim
# --------------------------------------------------------------------------

def wsgi(method, path, body=b"", token=PUSH_TOKEN, extra_headers=None):
    query = path.split("?", 1)[1] if "?" in path else ""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_HOST": "ctrl-a-jr.vercel.app",
    }
    if token:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    for key, value in (extra_headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split(" ")[0])

    chunks = index.app(environ, start_response)
    return captured["status"], b"".join(chunks)


def post_json(path, payload, token=PUSH_TOKEN):
    return wsgi("POST", path, json.dumps(payload).encode("utf-8"), token=token)


def body_json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def send_turn(id_="tu_send"):
    return Msg([Block("tool_use", name="gmail_send", input_=SEND_ARGS, id_=id_)],
               stop_reason="tool_use")


def all_runs(store: RunStore) -> list[tuple]:
    return store._send.sql("SELECT run_id FROM jr_runs") if _has_table(store) else []


def _has_table(store: RunStore) -> bool:
    rows = store._send.sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='jr_runs'"
    )
    return bool(rows)


# --------------------------------------------------------------------------
# the gate holds through the conversational entrypoint
# --------------------------------------------------------------------------

def test_a_message_needing_a_mutating_tool_suspends_and_the_tool_did_not_run(wired):
    """The whole point: a chat entrypoint is not a way around the approval gate."""
    wired["client"].queue(send_turn())

    status, raw = post_json("/api/runs", {"message": "Quote the Tahoe job and email it"})
    payload = body_json(raw)

    assert status == 201
    assert wired["spy"].calls == []                 # the implementation was never reached
    assert payload["status"] == "awaiting_approval"
    assert payload["pending_approval"]["tool"] == "gmail_send"
    # The human is shown the ARTIFACT, not the arguments.
    assert "Full detail: $420.00" in payload["pending_approval"]["rendered"]
    assert wired["store"].load(payload["run_id"]).status == "awaiting_approval"


def test_the_first_user_turn_is_the_humans_message_not_a_constant(wired):
    wired["client"].queue(Msg([Block("text", text="Two requests are waiting.")]))

    status, raw = post_json("/api/runs", {"message": "what's waiting in the inbox?"})

    assert status == 201
    first_history = wired["client"].messages_seen[0]
    assert first_history[0] == {"role": "user", "content": "what's waiting in the inbox?"}
    assert body_json(raw)["conversation"][0] == {
        "kind": "human", "text": "what's waiting in the inbox?",
    }


def test_the_suspended_call_is_published_to_the_approval_surface(wired):
    """Otherwise the run suspends on a ticket no human surface can answer."""
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    approval_id = body_json(raw)["pending_approval"]["approval_id"]

    record = wired["approvals"].get(approval_id)
    assert record is not None and record.decision == "pending"
    assert "/api/approve/" in body_json(raw)["pending_approval"]["approve_url"]


def test_advance_twice_on_one_decided_approval_executes_the_tool_once(wired):
    """At-most-once, via claim_pending. A double-clicked button must not resend."""
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    run_id = body_json(raw)["run_id"]
    approval_id = body_json(raw)["pending_approval"]["approval_id"]

    wired["approvals"].decide(approval_id, "approved", "link", "2026-09-13T12:00:00+00:00")

    wired["client"].queue(Msg([Block("text", text="Sent.")]))
    first, _ = wsgi("POST", f"/api/runs/{run_id}/advance")
    wired["client"].queue(Msg([Block("text", text="Sent again.")]))
    second, raw2 = wsgi("POST", f"/api/runs/{run_id}/advance")

    assert (first, second) == (200, 200)
    assert wired["spy"].calls == [SEND_ARGS]        # exactly one real send
    assert body_json(raw2)["status"] == "done"


def test_advance_without_a_decision_stays_awaiting_and_executes_nothing(wired):
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    run_id = body_json(raw)["run_id"]

    status, raw2 = wsgi("POST", f"/api/runs/{run_id}/advance")

    assert status == 200
    assert body_json(raw2)["status"] == "awaiting_approval"
    assert wired["spy"].calls == []


def test_a_denied_approval_does_not_execute_the_tool(wired):
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    run_id, approval_id = body_json(raw)["run_id"], body_json(raw)["pending_approval"][
        "approval_id"]
    wired["approvals"].decide(approval_id, "denied", "link", "2026-09-13T12:00:00+00:00")

    wired["client"].queue(Msg([Block("text", text="The send was denied; stopping.")]))
    status, raw2 = wsgi("POST", f"/api/runs/{run_id}/advance")

    assert status == 200
    assert wired["spy"].calls == []
    assert body_json(raw2)["status"] == "done"


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def test_a_new_message_is_refused_while_a_run_awaits_a_decision(wired):
    """The human owes the run a decision, not a new instruction."""
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    run_id = body_json(raw)["run_id"]
    approval_id = body_json(raw)["pending_approval"]["approval_id"]

    status, raw2 = post_json(f"/api/runs/{run_id}/messages", {"message": "actually, stop"})
    error = body_json(raw2)["error"]

    assert status == 409
    assert approval_id in error and "decide" in error
    assert wired["spy"].calls == []
    assert wired["store"].load(run_id).status == "awaiting_approval"


def test_a_message_continues_a_finished_run_with_its_own_reply_in_history(wired):
    wired["client"].queue(Msg([Block("text", text="Two requests are waiting.")]))
    _, raw = post_json("/api/runs", {"message": "what's waiting?"})
    run_id = body_json(raw)["run_id"]

    wired["client"].queue(Msg([Block("text", text="The Tahoe one is biggest.")]))
    status, raw2 = post_json(f"/api/runs/{run_id}/messages", {"message": "which is biggest?"})
    payload = body_json(raw2)

    assert status == 200
    assert payload["status"] == "done"
    assert [t["text"] for t in payload["conversation"]] == [
        "what's waiting?", "Two requests are waiting.",
        "which is biggest?", "The Tahoe one is biggest.",
    ]
    # The model saw its own previous answer, not the question twice.
    assert wired["client"].messages_seen[-1][1]["role"] == "assistant"


def test_an_empty_message_is_refused_and_creates_no_run(wired):
    status, _ = post_json("/api/runs", {"message": "   "})
    assert status == 400
    assert all_runs(wired["store"]) == []


# --------------------------------------------------------------------------
# conversation shape
# --------------------------------------------------------------------------

def test_the_conversation_renders_two_model_turns_and_a_tool_call_in_order(wired):
    """A UI gets ordered turns, not raw model JSON."""
    wired["client"].queue(
        Msg([Block("text", text="Let me price it."),
             Block("tool_use", name="quote_price", input_={}, id_="tu_q")],
            stop_reason="tool_use"),
        Msg([Block("text", text="That job prices at $420.00.")]),
    )

    _, raw = post_json("/api/runs", {"message": "price the Tahoe job"})
    turns = body_json(raw)["conversation"]

    assert [t["kind"] for t in turns] == ["human", "assistant", "tool_call", "assistant"]
    assert turns[2]["tool"] == "quote_price"
    assert turns[2]["status"] == "ok"
    assert "420" in turns[2]["output"]
    assert turns[3]["text"] == "That job prices at $420.00."


def test_a_failing_tool_renders_as_failed_not_ok(wired):
    registry_spy = Spy(ToolResult(False, "", "menu unavailable"))
    build = _build(Spy(), read_spy=registry_spy, client=wired["client"],
                   slack_http=wired["slack"])
    runs_mod.AGENT = build
    wired["client"].queue(
        Msg([Block("tool_use", name="quote_price", input_={}, id_="tu_q")],
            stop_reason="tool_use"),
        Msg([Block("text", text="I could not read the menu.")]),
    )

    _, raw = post_json("/api/runs", {"message": "price it"})
    tool_turn = [t for t in body_json(raw)["conversation"] if t["kind"] == "tool_call"][0]

    assert tool_turn["status"] == "failed"


def test_get_run_returns_the_conversation_and_the_pending_card(wired):
    wired["client"].queue(send_turn())
    _, raw = post_json("/api/runs", {"message": "email the quote"})
    run_id = body_json(raw)["run_id"]

    status, raw2 = wsgi("GET", f"/api/runs/{run_id}")
    payload = body_json(raw2)

    assert status == 200
    assert payload["pending_approval"]["tool"] == "gmail_send"
    assert payload["conversation"][-1]["kind"] == "approval"


def test_get_unknown_run_is_404(wired):
    status, _ = wsgi("GET", "/api/runs/deadbeef0000")
    assert status == 404


def test_listing_returns_recent_runs_newest_first(wired):
    wired["client"].queue(Msg([Block("text", text="one")]),
                          Msg([Block("text", text="two")]))
    post_json("/api/runs", {"message": "first question"})
    post_json("/api/runs", {"message": "second question"})

    status, raw = wsgi("GET", "/api/runs")
    listed = body_json(raw)["runs"]

    assert status == 200
    assert len(listed) == 2
    assert {r["first_message"] for r in listed} == {"first question", "second question"}
    assert all(r["status"] == "done" for r in listed)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

ROUTES = [
    ("POST", "/api/runs", b'{"message":"send an email to a@b.c"}'),
    ("GET", "/api/runs", b""),
    ("GET", "/api/runs/abc123", b""),
    ("POST", "/api/runs/abc123/messages", b'{"message":"go"}'),
    ("POST", "/api/runs/abc123/advance", b""),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_is_404_without_a_bearer_token(wired, method, path, body):
    status, _ = wsgi(method, path, body, token=None)
    assert status == 404


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_an_unauthenticated_request_changes_nothing(wired, method, path, body):
    """404 is not enough: the store must not have been touched either."""
    wsgi(method, path, body, token=None)
    assert all_runs(wired["store"]) == []
    assert wired["spy"].calls == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_a_wrong_bearer_token_is_404(wired, method, path, body):
    status, _ = wsgi(method, path, body, token="not-the-token")
    assert status == 404
    assert all_runs(wired["store"]) == []


def test_an_unset_push_token_fails_closed(wired, monkeypatch):
    """A deploy that forgot the variable must not publish an open endpoint."""
    monkeypatch.setenv("CTRLA_JR_PUSH_TOKEN", "")
    status, _ = post_json("/api/runs", {"message": "email everyone"})
    assert status == 404
    assert all_runs(wired["store"]) == []


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

CREDENTIAL_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_x",
    "GMAIL_ADDRESS": "shop@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "CTRLA_JR_SLACK_CHANNEL": "C0SHOP",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}


def test_build_agent_names_every_missing_variable_and_builds_nothing(monkeypatch):
    for name in CREDENTIAL_ENV:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CTRLA_JR_PROVIDER", "minimax")

    with pytest.raises(MissingCredential) as caught:
        build_agent()

    message = str(caught.value)
    for name in CREDENTIAL_ENV:
        assert name in message


def test_build_agent_names_the_one_variable_that_is_missing(monkeypatch):
    for name, value in CREDENTIAL_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")

    with pytest.raises(MissingCredential) as caught:
        build_agent()

    assert "GMAIL_APP_PASSWORD" in str(caught.value)
    assert "STRIPE_SECRET_KEY" not in str(caught.value)


def test_the_route_reports_the_missing_variable_and_creates_no_run(wired, monkeypatch):
    monkeypatch.setattr(runs_mod, "AGENT", None)
    for name in CREDENTIAL_ENV:
        monkeypatch.setenv(name, "")

    status, raw = post_json("/api/runs", {"message": "email the quote"})

    assert status == 503
    assert "STRIPE_SECRET_KEY" in body_json(raw)["error"]
    assert all_runs(wired["store"]) == []     # no half-started run row


def test_an_openrouter_deploy_is_asked_for_the_openrouter_key(monkeypatch):
    for name, value in CREDENTIAL_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CTRLA_JR_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")

    with pytest.raises(MissingCredential) as caught:
        build_agent()

    assert "OPENROUTER_API_KEY" in str(caught.value)


# --------------------------------------------------------------------------
# Slack events
# --------------------------------------------------------------------------

def slack_post(payload: dict, *, secret=SIGNING_SECRET, retry: str | None = None,
               timestamp: str | None = None):
    body = json.dumps(payload).encode("utf-8")
    stamp = timestamp or str(int(time.time()))
    headers = {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": slack_lib.sign(secret, stamp, body),
    }
    if retry is not None:
        headers["X-Slack-Retry-Num"] = retry
    # No bearer token: this endpoint's only authentication is the signature.
    return wsgi("POST", "/api/slack/events", body, token=None, extra_headers=headers)


def mention_event(text="<@U0BOT> quote the Tahoe job", event_id="Ev1", type_="app_mention",
                  **event_overrides):
    event = {
        "type": type_,
        "user": "U0HUMAN",
        "text": text,
        "channel": "C0SHOP",
        "ts": "1700000000.000100",
    }
    event.update(event_overrides)
    return {
        "type": "event_callback",
        "event_id": event_id,
        "authorizations": [{"user_id": "U0BOT"}],
        "event": event,
    }


def test_a_forged_slack_signature_is_rejected_and_creates_no_run(wired):
    status, _ = slack_post(mention_event(), secret="0" * 32)

    assert status == 404
    assert all_runs(wired["store"]) == []
    assert wired["spy"].calls == []
    assert wired["slack"].posts == []


def test_an_unsigned_slack_request_is_rejected(wired):
    status, _ = wsgi("POST", "/api/slack/events", b'{"type":"event_callback"}', token=None)
    assert status == 404
    assert all_runs(wired["store"]) == []


def test_a_stale_slack_timestamp_is_rejected(wired):
    """Replay window: a captured request must not stay valid forever."""
    old = str(int(time.time()) - slack_lib.MAX_SKEW_SECONDS - 60)
    status, _ = slack_post(mention_event(), timestamp=old)
    assert status == 404
    assert all_runs(wired["store"]) == []


def test_url_verification_echoes_the_challenge_and_creates_nothing(wired):
    status, raw = slack_post({"type": "url_verification", "challenge": "abc123"})

    assert status == 200
    assert body_json(raw)["challenge"] == "abc123"
    assert all_runs(wired["store"]) == []


def test_an_app_mention_starts_a_run_with_the_mention_stripped(wired):
    wired["client"].queue(Msg([Block("text", text="Two are waiting.")]))

    status, raw = slack_post(mention_event(text="<@U0BOT> what's waiting in the inbox?"))

    assert status == 200
    run_id = body_json(raw)["run_id"]
    state = wired["store"].load(run_id)
    assert state.messages[0] == {"role": "user", "content": "what's waiting in the inbox?"}


def test_the_answer_is_posted_back_into_the_same_thread(wired):
    wired["client"].queue(Msg([Block("text", text="Two are waiting.")]))
    slack_post(mention_event())

    posted = wired["slack"].posts[-1]["payload"]
    assert posted["channel"] == "C0SHOP"
    assert posted["thread_ts"] == "1700000000.000100"
    assert "Two are waiting." in posted["text"]


def test_a_retried_event_id_does_not_start_a_second_run(wired):
    """The duplicate-invoice case. Slack retries every event we are slow to ack."""
    wired["client"].queue(Msg([Block("text", text="Two are waiting.")]))
    first, _ = slack_post(mention_event(event_id="Ev-retry"))
    # A retry carries the SAME event_id. Slack sends X-Slack-Retry-Num too, so
    # drop that header to prove the claim itself is what stops the second run.
    wired["client"].queue(Msg([Block("text", text="Two are waiting, again.")]))
    second, raw = slack_post(mention_event(event_id="Ev-retry"))

    assert (first, second) == (200, 200)
    assert body_json(raw).get("duplicate") == "Ev-retry"
    assert len(all_runs(wired["store"])) == 1


def test_a_retry_header_short_circuits_before_any_work(wired):
    wired["client"].queue(Msg([Block("text", text="hello")]))
    status, raw = slack_post(mention_event(event_id="Ev-header"), retry="1")

    assert status == 200
    assert body_json(raw)["retry"] is True
    assert all_runs(wired["store"]) == []


def test_a_retried_event_cannot_trigger_a_second_mutating_tool(wired):
    """Three deliveries of one @mention must not become three approval cards."""
    wired["client"].queue(send_turn())
    slack_post(mention_event(event_id="Ev-send"))
    wired["client"].queue(send_turn(id_="tu_send2"))
    _, raw = slack_post(mention_event(event_id="Ev-send"))

    assert body_json(raw).get("duplicate") == "Ev-send"
    assert len(all_runs(wired["store"])) == 1
    assert wired["spy"].calls == []
    assert len(wired["approvals"]._rows) == 1


def test_a_bot_message_is_ignored(wired):
    """Otherwise the agent's own reply re-triggers it, forever, in a real workspace."""
    payload = mention_event(event_id="Ev-bot", type_="message", channel_type="im",
                            bot_id="B0BOT")
    status, raw = slack_post(payload)

    assert status == 200
    assert body_json(raw)["ignored"] == "bot"
    assert all_runs(wired["store"]) == []


def test_the_apps_own_user_is_ignored(wired):
    payload = mention_event(event_id="Ev-self", type_="message", channel_type="im",
                            user="U0BOT")
    status, raw = slack_post(payload)

    assert status == 200
    assert body_json(raw)["ignored"] == "bot"
    assert all_runs(wired["store"]) == []


def test_channel_chatter_that_is_not_a_dm_or_mention_is_ignored(wired):
    payload = mention_event(event_id="Ev-chan", type_="message", channel_type="channel")
    status, raw = slack_post(payload)

    assert status == 200
    assert body_json(raw)["ignored"] == "not a DM"
    assert all_runs(wired["store"]) == []


def test_a_dm_starts_a_run(wired):
    wired["client"].queue(Msg([Block("text", text="On it.")]))
    status, raw = slack_post(mention_event(event_id="Ev-dm", type_="message",
                                          channel_type="im", channel="D0HUMAN",
                                          text="quote the Tahoe job"))

    assert status == 200
    run_id = body_json(raw)["run_id"]
    assert wired["store"].load(run_id).messages[0]["content"] == "quote the Tahoe job"


def test_a_slack_run_that_needs_a_mutating_tool_still_suspends_at_the_gate(wired):
    """Arriving from Slack is not a reason the gate stops applying."""
    wired["client"].queue(send_turn())

    status, raw = slack_post(mention_event(event_id="Ev-gate"))
    run_id = body_json(raw)["run_id"]

    assert status == 200
    assert wired["spy"].calls == []
    assert wired["store"].load(run_id).status == "awaiting_approval"
    # The approval card went to the thread, with the buttons the interactive
    # endpoint answers.
    posted = wired["slack"].posts[-1]["payload"]
    assert posted["thread_ts"] == "1700000000.000100"
    action_ids = [
        element.get("action_id")
        for block in posted["blocks"] if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert slack_lib.APPROVE_ACTION in action_ids


def test_a_second_message_in_the_thread_continues_the_same_run(wired):
    wired["client"].queue(Msg([Block("text", text="Two are waiting.")]))
    _, raw = slack_post(mention_event(event_id="Ev-a"))
    first_run = body_json(raw)["run_id"]

    wired["client"].queue(Msg([Block("text", text="The Tahoe one.")]))
    _, raw2 = slack_post(mention_event(event_id="Ev-b", text="<@U0BOT> which is biggest?",
                                       thread_ts="1700000000.000100"))

    assert body_json(raw2)["run_id"] == first_run
    assert len(all_runs(wired["store"])) == 1


def test_a_thread_awaiting_a_decision_refuses_a_new_instruction(wired):
    wired["client"].queue(send_turn())
    slack_post(mention_event(event_id="Ev-hold"))

    wired["client"].queue(Msg([Block("text", text="should not be reached")]))
    slack_post(mention_event(event_id="Ev-hold2", text="<@U0BOT> never mind, do something else",
                             thread_ts="1700000000.000100"))

    assert len(all_runs(wired["store"])) == 1
    assert wired["spy"].calls == []
    assert "waiting on a human decision" in wired["slack"].posts[-1]["payload"]["text"]


def test_slack_post_reply_refuses_a_destination_that_did_not_come_from_an_event(wired):
    """Tripwire on invariant 4: a channel must look like one Slack sent."""
    client = wired["build"].slack
    with pytest.raises(ValueError, match="inbound Slack event"):
        client.post_reply("ar-escalations", "1700000000.000100", "hi")
