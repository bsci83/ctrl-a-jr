"""The resumable loop: suspend on a gate, continue in another process.

Every test here that claims a tool did not execute asserts it with a SPY on the
tool implementation, not with a returned status. A status is what the code under
test says happened; the spy is what happened.
"""

from __future__ import annotations

import json

import pytest

from ctrl_a_jr import activity
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.evals.runner import group_by_run
from ctrl_a_jr.guard import Guard
from ctrl_a_jr.loop import MAX_ROUNDS
from ctrl_a_jr.registry import Registry, ToolSpec
from ctrl_a_jr.resume import SuspendingApprover, advance
from ctrl_a_jr.runstate import (
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    RunStore,
)
from ctrl_a_jr.types import ToolResult
from tests.turso_fake import FakeTurso, TransportDown


@pytest.fixture(autouse=True)
def _restore_run_id():
    """`advance` rebinds the process-global `activity.RUN_ID` on purpose.

    Leaving it rebound would silently relabel every later test's events, so the
    blast radius is contained here rather than trusted to each test.
    """
    original = activity.RUN_ID
    yield
    activity.RUN_ID = original


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
    """One invocation's worth of model responses, recording what it was sent."""

    provider = "test"
    model = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []
        self.tools_seen = []
        self.calls = 0

    def create(self, system, messages, tools):
        self.calls += 1
        self.tools_seen.append(tools)
        self.messages_seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0)


class Spy:
    """Records every actual execution of a tool implementation."""

    def __init__(self, name, result=None):
        self.name = name
        self.calls = []
        self.result = result or ToolResult(True, "sent")

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _guard(send_spy, read_spy=None):
    registry = Registry()
    registry.register(ToolSpec(
        "stripe_get_invoice", "read", {"type": "object", "properties": {}}, False,
        read_spy or Spy("read", ToolResult(True, '{"amount_due":4200}')),
    ))
    registry.register(ToolSpec(
        "gmail_send", "send", {"type": "object", "properties": {}}, True,
        send_spy, render=lambda **kw: f"To: {kw.get('to')}\n\n{kw.get('body')}",
    ))
    # The approver is irrelevant on this path — `advance` replaces it — and a
    # blocking one here would prove nothing, so it is the failing one.
    return Guard(registry, ApprovalStore(), SuspendingApprover())


def _start(store, user="recover"):
    return store.create_run("sys", user, max_rounds=8).run_id


SEND_ARGS = {"to": "a@b.c", "body": "You owe $42.00"}


def _send_turn(id_="t_send", args=None):
    return Msg([Block("tool_use", name="gmail_send", input_=args or SEND_ARGS, id_=id_)],
               stop_reason="tool_use")


# --------------------------------------------------------------------------
# suspend
# --------------------------------------------------------------------------

def test_max_rounds_is_eight():
    """Raised from 5: a live run hit the limit before writing its report."""
    assert MAX_ROUNDS == 8


def test_advance_suspends_on_a_mutating_tool_and_does_not_execute_it():
    """The gate holds across a process boundary: nothing ran, state says awaiting."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)

    status = advance(run_id, Scripted([_send_turn()]), _guard(spy), store)

    assert spy.calls == []                     # the implementation was never reached
    assert status.status == STATUS_AWAITING
    assert status.approval_id and status.tool == "gmail_send"
    assert store.load(run_id).status == STATUS_AWAITING
    # The human is shown the artifact, not the arguments (P2).
    assert "You owe $42.00" in status.rendered


def test_suspending_does_not_create_an_approval_record_or_run_the_gate():
    """A suspend is not an approval request that timed out; nothing was decided."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)

    events = [r["event"] for r in activity.read_log()]
    assert "approval_resolved" not in events
    assert "tool_call" not in events
    assert "run_suspended" in events
    assert store.load(run_id).decision is None


def test_read_only_tools_still_run_without_suspending():
    """Only mutating calls cost a round trip to a human."""
    store = RunStore(FakeTurso())
    read_spy = Spy("read", ToolResult(True, "{}"))
    run_id = _start(store)
    client = Scripted([
        Msg([Block("tool_use", name="stripe_get_invoice", id_="t1")], stop_reason="tool_use"),
        Msg([Block("text", text="nothing to recover")]),
    ])
    status = advance(run_id, client, _guard(Spy("gmail_send"), read_spy), store)
    assert len(read_spy.calls) == 1
    assert status.status == STATUS_DONE
    assert status.text == "nothing to recover"


# --------------------------------------------------------------------------
# resume: approved
# --------------------------------------------------------------------------

def test_resume_with_approval_executes_exactly_once():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)

    pending = store.load(run_id).pending
    assert store.record_decision(run_id, pending.approval_id, "approved") is True

    status = advance(run_id, Scripted([Msg([Block("text", text="Recovered 1 of 1.")])]),
                     _guard(spy), store)

    assert spy.calls == [SEND_ARGS]            # executed, once, with the approved payload
    assert status.status == STATUS_DONE
    assert status.text == "Recovered 1 of 1."


def test_a_second_advance_on_the_same_decided_approval_does_not_execute_twice():
    """At-most-once. A retried webhook or a double-clicked button must not resend."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")

    advance(run_id, Scripted([Msg([Block("text", text="done")])]), _guard(spy), store)
    second = advance(run_id, Scripted([Msg([Block("text", text="done again")])]),
                     _guard(spy), store)

    assert len(spy.calls) == 1                 # the send did NOT go out twice
    assert second.status == STATUS_DONE        # already finished; nothing re-run


def test_a_concurrent_invocation_loses_the_claim_and_executes_nothing():
    """Two invocations race on one decided approval; the claim is a CAS, so one loses."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")

    assert store.claim_pending(run_id, pending.approval_id) is True   # the other invocation
    status = advance(run_id, Scripted([Msg([Block("text", text="x")])]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED
    assert "not retried" in status.detail


def test_an_undecided_approval_returns_awaiting_and_executes_nothing():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_AWAITING


def test_an_unrecognised_decision_string_is_not_an_approval():
    """Anything that is not exactly 'approved' or 'denied' waits."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    store._send.sql("UPDATE jr_runs SET decision = ? WHERE run_id = ?", ("APPROVED!", run_id))

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_AWAITING


# --------------------------------------------------------------------------
# resume: denied
# --------------------------------------------------------------------------

def test_resume_with_denial_produces_the_guards_own_denial_text_and_executes_nothing():
    """P4: the denial the model reads must be the SAME text the local path emits.

    It is not copied here — the resume runs the real `Guard.dispatch` with the
    human's DENIED decision, so the wording is the guard's by construction.
    """
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "denied")

    client = Scripted([Msg([Block("text", text="I was denied, so I stopped.")])])
    status = advance(run_id, client, _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_DONE

    # The model's next request carries the denial as a tool_result it can read.
    results = client.messages_seen[0][-1]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[0]["is_error"] is True
    expected = ToolResult(
        False, "",
        "denied by the operator. Do not retry this call or attempt it "
        "by another route; report the denial and stop.",
    ).to_model()
    assert results[0]["content"] == expected


def test_a_denial_is_recorded_as_an_operator_denial_the_eval_harness_can_score():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "denied")
    advance(run_id, Scripted([Msg([Block("text", text="stopped")])]), _guard(spy), store)

    resolved = [r for r in activity.read_log() if r["event"] == "approval_resolved"]
    assert resolved and resolved[-1]["decision"] == "denied"
    assert resolved[-1]["tool"] == "gmail_send"


# --------------------------------------------------------------------------
# invariant 2 across the boundary
# --------------------------------------------------------------------------

def test_a_multi_tool_use_turn_is_answered_once_per_use_in_emission_order():
    """Invariant 2 survives a suspend in the MIDDLE of a turn.

    Three uses, only the second needs a human. The first executed before the
    suspend, the third after it, in a different process — and the results must
    still come back one per use, in the order the model emitted them, or the
    next request is a 400.
    """
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    read_spy = Spy("read", ToolResult(True, "{}"))
    run_id = _start(store)
    turn = Msg([
        Block("tool_use", name="stripe_get_invoice", id_="t_a"),
        Block("tool_use", name="gmail_send", input_=SEND_ARGS, id_="t_b"),
        Block("tool_use", name="stripe_get_invoice", id_="t_c"),
    ], stop_reason="tool_use")

    advance(run_id, Scripted([turn]), _guard(spy, read_spy), store)
    assert len(read_spy.calls) == 1            # only the use BEFORE the gate ran
    assert spy.calls == []

    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    client = Scripted([Msg([Block("text", text="done")])])
    advance(run_id, client, _guard(spy, read_spy), store)

    results = client.messages_seen[0][-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["t_a", "t_b", "t_c"]
    assert all(b["type"] == "tool_result" for b in results)
    assert len(results) == 3
    assert len(spy.calls) == 1
    assert len(read_spy.calls) == 2

    # The assistant turn that emitted them survived storage unmangled.
    assistant = client.messages_seen[0][-2]
    assert [b["type"] for b in assistant["content"]] == ["tool_use"] * 3
    assert [b["id"] for b in assistant["content"]] == ["t_a", "t_b", "t_c"]


def test_two_mutating_calls_in_one_turn_suspend_one_at_a_time():
    """One approval authorises exactly one call, even inside the same turn."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    turn = Msg([
        Block("tool_use", name="gmail_send", input_={"to": "one@x.c", "body": "1"}, id_="t_a"),
        Block("tool_use", name="gmail_send", input_={"to": "two@x.c", "body": "2"}, id_="t_b"),
    ], stop_reason="tool_use")

    advance(run_id, Scripted([turn]), _guard(spy), store)
    first = store.load(run_id).pending
    store.record_decision(run_id, first.approval_id, "approved")
    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert status.status == STATUS_AWAITING     # suspended again on the SECOND send
    assert spy.calls == [{"to": "one@x.c", "body": "1"}]
    second = store.load(run_id).pending
    assert second.approval_id != first.approval_id
    assert second.args == {"to": "two@x.c", "body": "2"}


# --------------------------------------------------------------------------
# failing closed
# --------------------------------------------------------------------------

def test_storage_failure_fails_closed():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")

    store._send.down = True                    # the database goes away
    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []                     # approved, but NOT executed
    assert status.status == STATUS_FAILED
    assert "unreadable" in status.detail


def test_a_transport_exception_does_not_escape_advance():
    """An escaping exception is an outcome nobody classified; this path has none."""
    store = RunStore(FakeTurso())
    run_id = _start(store)
    store._send.down = True
    status = advance(run_id, Scripted([]), _guard(Spy("gmail_send")), store)
    assert status.status == STATUS_FAILED
    with pytest.raises(TransportDown):          # the fake really does raise
        store._send({"requests": []})


def test_missing_state_fails_closed():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    store.create_run("sys", "go", 8)           # table exists, this run does not
    status = advance("no_such_run", Scripted([]), _guard(spy), store)
    assert spy.calls == []
    assert status.status == STATUS_FAILED
    assert status.detail == "no such run"


def test_corrupt_messages_fail_closed():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    store._send.sql("UPDATE jr_runs SET messages = ? WHERE run_id = ?", ("<<corrupt", run_id))

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED


def test_corrupt_pending_tool_uses_fail_closed():
    """A use with no id cannot be answered; refusing beats a 400 two turns later."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    store._send.sql("UPDATE jr_runs SET pending_uses = ? WHERE run_id = ?",
                    (json.dumps([{"name": "gmail_send", "input": SEND_ARGS}]), run_id))

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED


def test_a_payload_edited_in_storage_after_approval_is_not_executed():
    """P3 across processes: approve one email, store another, send neither."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    tampered = json.dumps({"to": "attacker@evil.example", "body": "You owe $42.00"},
                          sort_keys=True, separators=(",", ":"))
    store._send.sql("UPDATE jr_runs SET pending_args = ? WHERE run_id = ?", (tampered, run_id))

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED
    assert "payload_mismatch" in [r["event"] for r in activity.read_log()]


def test_a_tool_use_swapped_in_storage_after_approval_is_not_executed():
    """The other half of P3: the approved args are intact, the QUEUED call is not."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    swapped = json.dumps([{"id": "t_send", "name": "gmail_send",
                           "input": {"to": "attacker@evil.example", "body": "x"}}])
    store._send.sql("UPDATE jr_runs SET pending_uses = ? WHERE run_id = ?", (swapped, run_id))

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED
    assert "payload_mismatch" in [r["event"] for r in activity.read_log()]


def test_a_run_claimed_but_never_finished_is_never_retried():
    """The tool may already have gone out; 'unknown' is not 'do it again'."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    store.claim_pending(run_id, pending.approval_id)   # invocation crashes here

    status = advance(run_id, Scripted([]), _guard(spy), store)

    assert spy.calls == []
    assert status.status == STATUS_FAILED


def test_an_unknown_status_fails_closed():
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    store._send.sql("UPDATE jr_runs SET status = ? WHERE run_id = ?", ("weird", run_id))
    status = advance(run_id, Scripted([]), _guard(spy), store)
    assert spy.calls == []
    assert status.status == STATUS_FAILED
    assert "unknown run status" in status.detail


# --------------------------------------------------------------------------
# invariant 1 and run identity
# --------------------------------------------------------------------------

def test_the_round_limit_still_ends_with_a_tools_off_final_turn():
    """Invariant 1 on the resumable path: nothing dangles at the boundary."""
    store = RunStore(FakeTurso())
    read_spy = Spy("read", ToolResult(True, "{}"))
    run_id = store.create_run("sys", "go", max_rounds=3).run_id
    client = Scripted(
        [Msg([Block("tool_use", name="stripe_get_invoice", id_=f"t{i}")],
             stop_reason="tool_use") for i in range(3)]
        + [Msg([Block("text", text="forced summary")])]
    )
    status = advance(run_id, client, _guard(Spy("gmail_send"), read_spy), store)

    assert status.hit_limit is True
    assert status.text == "forced summary"
    assert client.tools_seen[-1] == []          # the final turn offered NO tools
    assert client.calls == 4
    assert status.rounds == client.calls


def test_the_limit_is_honoured_across_a_suspend():
    """A resumed run cannot buy itself extra rounds by suspending."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = store.create_run("sys", "go", max_rounds=1).run_id
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")

    client = Scripted([Msg([Block("text", text="forced summary")])])
    status = advance(run_id, client, _guard(spy), store)

    assert status.hit_limit is True
    assert client.tools_seen == [[]]            # only the tools-off boundary turn
    assert len(spy.calls) == 1


def test_all_events_of_a_resumed_run_share_one_run_id():
    """The eval harness groups by run_id; fragments make its verdict meaningless."""
    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    read_spy = Spy("read", ToolResult(True, "{}"))
    run_id = _start(store)

    advance(run_id, Scripted([
        Msg([Block("tool_use", name="stripe_get_invoice", id_="t0")], stop_reason="tool_use"),
        _send_turn(),
    ]), _guard(spy, read_spy), store)

    activity.RUN_ID = "someoneelse"             # a different process would differ here
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    advance(run_id, Scripted([Msg([Block("text", text="Recovered 1 of 1.")])]),
            _guard(spy, read_spy), store)

    records = activity.read_log()
    groups = group_by_run(records)
    assert len(groups) == 1
    assert groups[0][0] == run_id
    # And the evidence actually spans both invocations.
    events = [r["event"] for r in records]
    assert "run_suspended" in events and "tool_call" in events


def test_the_gate_checks_pass_over_a_resumed_run():
    """The point of one run_id: the deterministic checks see a whole run."""
    from ctrl_a_jr.evals.checks import check_gate_integrity

    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    run_id = _start(store)
    advance(run_id, Scripted([_send_turn()]), _guard(spy), store)
    pending = store.load(run_id).pending
    store.record_decision(run_id, pending.approval_id, "approved")
    advance(run_id, Scripted([Msg([Block("text", text="done")])]), _guard(spy), store)

    rows = group_by_run(activity.read_log())[0][1]
    assert check_gate_integrity(rows).verdict == "pass"


def test_advance_ignores_a_blocking_approver_handed_in_by_the_caller():
    """A caller cannot accidentally turn a suspend into a serverless timeout."""
    class Blocking:
        def __init__(self):
            self.asked = 0

        def decide(self, record):
            self.asked += 1
            raise AssertionError("advance must never consult the caller's approver")

    store, spy = RunStore(FakeTurso()), Spy("gmail_send")
    registry = _guard(spy).registry
    approver = Blocking()
    run_id = _start(store)

    status = advance(run_id, Scripted([_send_turn()]),
                     Guard(registry, ApprovalStore(), approver), store)

    assert approver.asked == 0
    assert status.status == STATUS_AWAITING
    assert spy.calls == []


def test_a_fresh_run_starts_from_the_running_status():
    store = RunStore(FakeTurso())
    run_id = _start(store)
    assert store.load(run_id).status == STATUS_RUNNING
    status = advance(run_id, Scripted([Msg([Block("text", text="hi")])]),
                     _guard(Spy("gmail_send")), store)
    assert status.status == STATUS_DONE
