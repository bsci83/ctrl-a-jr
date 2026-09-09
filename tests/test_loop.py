import pytest

from ctrl_a_jr import activity
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.guard import Guard
from ctrl_a_jr.loop import run_loop
from ctrl_a_jr.registry import Registry, ToolSpec
from ctrl_a_jr.types import Decision, ToolResult


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(tmp_path / "a.jsonl"))


class Block:
    """Minimal stand-in for an Anthropic content block."""

    def __init__(self, type_, text=None, name=None, input_=None, id_=None):
        self.type = type_
        self.text = text
        self.name = name
        self.input = input_ or {}
        self.id = id_ or "tu_1"


class Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class ScriptedClient:
    """Replays responses, recording the tools offered and the messages sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.tools_seen = []
        self.messages_seen = []
        self.calls = 0

    def create(self, system, messages, tools):
        self.calls += 1
        self.tools_seen.append(tools)
        # Deep-ish copy: the loop mutates its own list between turns.
        self.messages_seen.append([dict(m) for m in messages])
        return self.responses.pop(0)


def _guard(calls):
    reg = Registry()
    reg.register(ToolSpec("ping", "p", {"type": "object", "properties": {}}, False,
                          lambda **kw: (calls.append(kw), ToolResult(True, "pong"))[1]))
    return Guard(reg, ApprovalStore(), type("A", (), {"decide": lambda s, r: Decision.APPROVED})())


def test_returns_text_when_model_stops():
    client = ScriptedClient([Msg([Block("text", text="all done")])])
    out = run_loop(client, _guard([]), "sys", "do it")
    assert out.text == "all done"
    assert out.rounds == 1


def test_executes_tool_and_feeds_result_back():
    calls = []
    client = ScriptedClient([
        Msg([Block("tool_use", name="ping", input_={}, id_="tu_a")], stop_reason="tool_use"),
        Msg([Block("text", text="finished")]),
    ])
    out = run_loop(client, _guard(calls), "sys", "go")
    assert calls == [{}]
    assert out.text == "finished"
    assert out.rounds == 2


def test_one_tool_result_per_tool_use_in_order():
    """Anthropic requires every tool_use to be answered, in order, in the next turn."""
    client = ScriptedClient([
        Msg([Block("tool_use", name="ping", input_={}, id_="tu_a"),
             Block("tool_use", name="ping", input_={}, id_="tu_b")], stop_reason="tool_use"),
        Msg([Block("text", text="ok")]),
    ])
    run_loop(client, _guard([]), "sys", "go")

    # The SECOND request carries the tool results as the final user message.
    second_request = client.messages_seen[1]
    results = second_request[-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["tu_a", "tu_b"]
    assert all(b["type"] == "tool_result" for b in results)
    assert len(results) == 2


def test_hits_max_rounds_and_forces_a_tools_off_final_turn():
    looping = [Msg([Block("tool_use", name="ping", input_={}, id_=f"tu_{i}")],
                   stop_reason="tool_use") for i in range(5)]
    looping.append(Msg([Block("text", text="forced summary")]))
    client = ScriptedClient(looping)
    out = run_loop(client, _guard([]), "sys", "go", max_rounds=5)
    assert out.hit_limit is True
    assert out.text == "forced summary"
    assert client.tools_seen[-1] == []  # final turn offered NO tools


def test_denied_tool_still_returns_a_result_block():
    reg = Registry()
    reg.register(ToolSpec("send", "s", {"type": "object", "properties": {}}, True,
                          lambda **kw: ToolResult(True, "sent"), render=lambda **kw: "x"))
    guard = Guard(reg, ApprovalStore(),
                  type("D", (), {"decide": lambda s, r: Decision.DENIED})())
    client = ScriptedClient([
        Msg([Block("tool_use", name="send", input_={}, id_="tu_a")], stop_reason="tool_use"),
        Msg([Block("text", text="I was denied and stopped.")]),
    ])
    out = run_loop(client, guard, "sys", "go")
    assert "denied" in out.text.lower()


def test_unrecognised_content_blocks_are_logged_not_silently_dropped():
    """A dropped block breaks the signature chain on a LATER turn — record it now."""
    client = ScriptedClient([
        Msg([Block("thinking", text="hmm"),
             Block("tool_use", name="ping", input_={}, id_="tu_a")], stop_reason="tool_use"),
        Msg([Block("text", text="done")]),
    ])
    run_loop(client, _guard([]), "sys", "go")
    dropped = [r for r in activity.read_log() if r["event"] == "content_block_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["block_type"] == "thinking"
