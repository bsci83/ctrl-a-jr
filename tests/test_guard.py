import pytest
from ctrl_a_jr import activity
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.guard import Guard
from ctrl_a_jr.registry import Registry, ToolSpec
from ctrl_a_jr.types import Decision, ToolResult


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(tmp_path / "a.jsonl"))


class Auto:
    """An approver that always answers the same way."""

    def __init__(self, decision):
        self.decision = decision
        self.seen = []

    def decide(self, record):
        self.seen.append(record)
        return self.decision


def _registry(calls):
    reg = Registry()
    reg.register(ToolSpec(
        name="read_thing", description="read", schema={"type": "object", "properties": {}},
        mutating=False, run=lambda **kw: (calls.append(("read", kw)), ToolResult(True, "read-ok"))[1],
    ))
    reg.register(ToolSpec(
        name="write_thing", description="write",
        schema={"type": "object", "properties": {"to": {"type": "string"}}},
        mutating=True,
        run=lambda **kw: (calls.append(("write", kw)), ToolResult(True, "sent"))[1],
        render=lambda **kw: f"To: {kw.get('to')}",
    ))
    return reg


def test_read_tool_runs_without_approval():
    calls = []
    approver = Auto(Decision.DENIED)  # would deny if asked
    g = Guard(_registry(calls), ApprovalStore(), approver)
    out = g.dispatch("read_thing", {})
    assert out.ok and out.content == "read-ok"
    assert approver.seen == []  # never asked


def test_mutating_tool_requires_approval_and_runs_when_approved():
    calls = []
    g = Guard(_registry(calls), ApprovalStore(), Auto(Decision.APPROVED))
    out = g.dispatch("write_thing", {"to": "a@b.c"})
    assert out.ok
    assert calls == [("write", {"to": "a@b.c"})]


def test_denied_mutating_tool_does_not_execute():
    calls = []
    g = Guard(_registry(calls), ApprovalStore(), Auto(Decision.DENIED))
    out = g.dispatch("write_thing", {"to": "a@b.c"})
    assert out.ok is False
    assert "denied" in (out.error or "").lower()
    assert calls == []  # the implementation was never reached


def test_denial_is_visible_to_the_model():
    g = Guard(_registry([]), ApprovalStore(), Auto(Decision.DENIED))
    out = g.dispatch("write_thing", {"to": "a@b.c"})
    assert out.to_model().startswith("ERROR:")


def test_refusal_is_logged():
    g = Guard(_registry([]), ApprovalStore(), Auto(Decision.DENIED))
    g.dispatch("write_thing", {"to": "a@b.c"})
    events = [r["event"] for r in activity.read_log()]
    assert "tool_refused" in events


def test_approved_call_is_logged_as_tool_call():
    g = Guard(_registry([]), ApprovalStore(), Auto(Decision.APPROVED))
    g.dispatch("write_thing", {"to": "a@b.c"})
    events = [r["event"] for r in activity.read_log()]
    assert "tool_call" in events


def test_payload_mismatch_aborts_and_logs():
    """If what is about to execute does not match what was approved, nothing runs."""
    calls = []

    class DivergentStore(ApprovalStore):
        def verify(self, approval_id, args):
            return False  # stand in for approval/execution divergence

    g = Guard(_registry(calls), DivergentStore(), Auto(Decision.APPROVED))
    out = g.dispatch("write_thing", {"to": "a@b.c"})
    assert out.ok is False
    assert "integrity" in (out.error or "").lower()
    assert calls == []  # the implementation was never reached
    assert "payload_mismatch" in [r["event"] for r in activity.read_log()]


def test_unknown_tool_returns_error_not_exception():
    g = Guard(_registry([]), ApprovalStore(), Auto(Decision.APPROVED))
    out = g.dispatch("nope", {})
    assert out.ok is False
    assert "unknown" in (out.error or "").lower()


def test_tool_exception_becomes_error_result():
    reg = Registry()

    def boom(**kw):
        raise RuntimeError("upstream 500")

    reg.register(ToolSpec("boom", "b", {"type": "object", "properties": {}}, False, boom))
    g = Guard(reg, ApprovalStore(), Auto(Decision.APPROVED))
    out = g.dispatch("boom", {})
    assert out.ok is False
    assert "upstream 500" in (out.error or "")


def test_registry_schemas_are_anthropic_shaped():
    reg = _registry([])
    schemas = reg.schemas()
    assert {s["name"] for s in schemas} == {"read_thing", "write_thing"}
    assert "input_schema" in schemas[0]
    assert "description" in schemas[0]


def test_mutating_field_is_required():
    with pytest.raises(TypeError):
        ToolSpec(
            name="oops", description="d", schema={"type": "object", "properties": {}},
            run=lambda **kw: ToolResult(True, "ok"),
        )
