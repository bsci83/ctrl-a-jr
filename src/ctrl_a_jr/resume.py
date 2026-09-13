"""The resumable driver: run until a human is needed, persist, return.

`run_loop` blocks inside `guard.dispatch` while a human decides. A serverless
function has seconds and a human has minutes, so `advance` does the same work
with the wait taken out: it runs rounds until a mutating tool needs a decision,
records everything required to continue, and returns. A later invocation —
different process, different machine — calls `advance` again and continues from
exactly that point.

What is NOT different about this path
-------------------------------------
Every tool still executes through `Guard.dispatch`, which is still the only
caller of `spec.run`. The denial text the model reads, the approval events the
eval harness scores, and the execution-time payload re-hash all come from the
guard running normally, not from anything reimplemented here. A resume supplies
the DECISION; it does not supply the enforcement.

Failing closed
--------------
Every unknown — unreadable storage, missing run, corrupt history, an undecided
approval, a claim lost to a concurrent invocation, a payload that no longer
hashes to what was approved — ends with the pending tool NOT running. Reporting
"approved" because state could not be read would be the worst bug this file
could have.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .activity import log_action
from .approval import payload_hash
from .guard import Guard
from .loop import (
    Gate,
    ModelClient,
    ToolUse,
    execute_uses,
    final_turn,
    result_block,
    run_round,
)
from .registry import Registry
from .runstate import (
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    PendingCall,
    RunState,
    RunStore,
    StorageError,
    set_active_run_id,
)
from .types import Decision, ToolResult


@dataclass(frozen=True)
class RunStatus:
    """What one invocation of `advance` achieved."""

    run_id: str
    status: str                       # running | awaiting_approval | done | failed
    text: str = ""                    # the model's final text, when done
    rounds: int = 0                   # rounds consumed across ALL invocations
    hit_limit: bool = False
    approval_id: str | None = None    # the ticket a human must decide
    tool: str | None = None
    rendered: str = ""                # the ARTIFACT to show the human, not the args
    detail: str = ""


class SuspendingApprover:
    """An approver that can only ever fail closed.

    `advance` never routes an undecided mutating call to `dispatch` — the gate
    below suspends first. This approver is the backstop for the case where that
    is wrong: if a tool were ever misrouted here, a blocking approver would hang
    a serverless function until it timed out, and whether the tool ran would
    depend on where the timeout landed. Raising makes the guard's fail-closed
    path take over instead, with the attempt on the record.
    """

    def decide(self, record: object) -> Decision:
        raise RuntimeError(
            "no human decision is available in this invocation; the run must suspend"
        )


class _PreDecidedApprover:
    """Replays one decision a human already made, for one call.

    Second use raises, so a single approval can never authorise a second tool
    call inside the same invocation — the guard then fails closed on it.
    """

    def __init__(self, decision: Decision) -> None:
        self._decision = decision
        self._used = False

    def decide(self, record: object) -> Decision:
        if self._used:
            raise RuntimeError("this approval has already authorised a call")
        self._used = True
        return self._decision


def _suspending_gate(registry: Registry, guard: Guard) -> Gate:
    """Answer read-only calls; suspend on anything mutating.

    `mutating` is read from the registry, the same field the guard itself gates
    on, so the two cannot disagree about which tools need a human. A tool the
    registry does not know goes to the guard, which refuses it by name.
    """

    def gate(use: ToolUse) -> ToolResult | None:
        spec = registry.get(use.name)
        if spec is not None and spec.mutating:
            return None
        return guard.dispatch(use.name, dict(use.input))

    return gate


def _render(registry: Registry, tool: str, args: dict) -> str:
    spec = registry.get(tool)
    # The human must approve the ARTIFACT, not a JSON blob (spec §5 P2).
    return spec.render_for_approval(args) if spec is not None else repr(args)


def advance(
    run_id: str,
    client: ModelClient,
    guard: Guard,
    store: RunStore | None = None,
) -> RunStatus:
    """Carry `run_id` forward as far as it can go without a human.

    `guard` supplies the registry and the approval store; its approver is
    deliberately IGNORED. A caller that handed in a blocking approver would
    otherwise turn a suspend into a serverless timeout, and the resumable path
    must not depend on the caller remembering that.
    """
    try:
        store = store or RunStore.from_env()
    except StorageError as exc:
        return RunStatus(run_id, STATUS_FAILED, detail=f"run state unavailable: {exc}")

    try:
        state = store.load(run_id)
    except StorageError as exc:
        # Unknown state. The pending tool is untouched, which is the only safe
        # reading of "we could not find out whether it was approved".
        return RunStatus(run_id, STATUS_FAILED, detail=f"run state unreadable: {exc}")

    if state is None:
        return RunStatus(run_id, STATUS_FAILED, detail="no such run")

    # Before anything else logs. Every event from here belongs to the LOGICAL
    # run, not to this process, or the eval harness scores fragments.
    set_active_run_id(state.run_id)

    registry = guard.registry
    # A guard that cannot block, for every call this invocation makes itself.
    working_guard = Guard(registry, guard.store, SuspendingApprover())
    gate = _suspending_gate(registry, working_guard)

    try:
        return _drive(state, client, working_guard, registry, gate, store)
    except StorageError as exc:
        return _fail(store, state, f"run state write failed: {exc}")


def _drive(state: RunState, client: ModelClient, guard: Guard, registry: Registry,
           gate: Gate, store: RunStore) -> RunStatus:
    if state.status in {STATUS_DONE, STATUS_FAILED}:
        return RunStatus(state.run_id, state.status, text=state.result_text,
                         rounds=state.round_index, hit_limit=state.hit_limit,
                         detail=state.detail or "run already finished")

    if state.status == STATUS_RUNNING and state.pending is not None:
        # Claimed by an earlier invocation that never reported back. Whether the
        # tool went out is unknowable from here, so it is never retried.
        return _fail(store, state,
                     f"a previous invocation claimed approval {state.pending.approval_id} "
                     "and did not finish; the pending call is not retried")

    if state.status == STATUS_AWAITING:
        resumed = _resume_pending(state, guard, registry, gate, store)
        if isinstance(resumed, RunStatus):
            return resumed
        state = resumed

    elif state.status != STATUS_RUNNING:
        return _fail(store, state, f"unknown run status {state.status!r}")

    return _run_rounds(state, client, registry, gate, store)


def _resume_pending(state: RunState, guard: Guard, registry: Registry,
                    gate: Gate, store: RunStore) -> RunState | RunStatus:
    """Execute (or refuse) the decided call and finish its turn.

    Returns a RunState to keep going, or a RunStatus meaning "stop here".
    """
    pending = state.pending
    if pending is None:
        return _fail(store, state, "awaiting approval but no pending call was recorded")

    decision = _decision_of(pending, state.decision)
    if decision is None:
        # Nobody has decided yet. Not an error, and emphatically not an approval.
        return RunStatus(state.run_id, STATUS_AWAITING, rounds=state.round_index,
                         approval_id=pending.approval_id, tool=pending.tool,
                         rendered=pending.rendered,
                         detail="waiting on a human decision")

    try:
        uses = [ToolUse.from_stored(u) for u in pending.uses]
    except ValueError as exc:
        return _fail(store, state, f"pending tool calls are corrupt: {exc}")

    if not uses:
        return _fail(store, state, "pending call recorded no tool_use blocks")

    # P3 across a process boundary, checked FIRST so a divergence is reported as
    # what it is. `approval.verify` re-hashes inside the guard too, but that
    # compares against a record this process just created from these same args;
    # only this comparison — against the hash captured when the human was ASKED —
    # can show that storage handed back something other than what it was given.
    head = uses[0]
    if (payload_hash(pending.tool, pending.args) != pending.payload_hash
            or head.name != pending.tool
            or head.input != pending.args):
        log_action("payload_mismatch", tool=pending.tool,
                   approval_id=pending.approval_id, stage="resume")
        return _fail(store, state,
                     "the pending payload is not the one that was approved")

    # At-most-once. Everything above is a read; this is the point of no return,
    # and it is a database compare-and-set so a concurrent second invocation
    # loses rather than executing the same send twice.
    if not store.claim_pending(state.run_id, pending.approval_id):
        return RunStatus(state.run_id, state.status, rounds=state.round_index,
                         approval_id=pending.approval_id, tool=pending.tool,
                         detail="this approval was already claimed; nothing was executed")

    log_action("run_resumed", run_id=state.run_id, approval_id=pending.approval_id,
               tool=pending.tool, decision=decision.value, round=state.round_index + 1)

    # Through the chokepoint, with the human's decision standing in for the
    # blocking prompt. A denial therefore produces the guard's own denial
    # ToolResult (P4) and the guard's own approval_resolved event — not a
    # paraphrase, and not an event shape the eval checks would miss.
    decided_guard = Guard(registry, guard.store, _PreDecidedApprover(decision))
    outcome = decided_guard.dispatch(head.name, dict(head.input))

    results = list(pending.results) + [result_block(head, outcome)]
    more, still_pending = execute_uses(uses[1:], gate)
    results.extend(more)

    if still_pending:
        # Another mutating call in the SAME turn. Suspend again on it, keeping
        # the results produced so far so emission order survives (invariant 2).
        return _suspend(state, registry, store, still_pending, results)

    messages = list(state.messages) + [{"role": "user", "content": results}]
    cleared = RunState(
        run_id=state.run_id, status=STATUS_RUNNING, system=state.system,
        messages=messages, round_index=state.round_index + 1,
        max_rounds=state.max_rounds, pending=None, decision=None,
        created_at=state.created_at,
    )
    return store.save(cleared)


def _run_rounds(state: RunState, client: ModelClient, registry: Registry,
                gate: Gate, store: RunStore) -> RunStatus:
    messages = list(state.messages)
    round_index = state.round_index
    tools = registry.schemas()

    while round_index < state.max_rounds:
        step = run_round(client, state.system, messages, tools, round_index, gate)

        if step.done:
            finished = store.save(RunState(
                run_id=state.run_id, status=STATUS_DONE, system=state.system,
                messages=messages, round_index=round_index + 1,
                max_rounds=state.max_rounds, result_text=step.text,
                created_at=state.created_at,
            ))
            return RunStatus(finished.run_id, STATUS_DONE, text=step.text,
                             rounds=round_index + 1)

        # The assistant turn is persisted as soon as it exists, so a suspend
        # stores a history whose last message is the unanswered tool_use turn.
        # The results are appended only when every use in it has been answered.
        messages.append(step.assistant_turn)

        if step.pending:
            return _suspend(
                RunState(run_id=state.run_id, status=STATUS_AWAITING,
                         system=state.system, messages=messages,
                         round_index=round_index, max_rounds=state.max_rounds,
                         created_at=state.created_at),
                registry, store, step.pending, step.results,
            )

        messages.append({"role": "user", "content": step.results})
        round_index += 1

    result = final_turn(client, state.system, messages, state.max_rounds)
    store.save(RunState(
        run_id=state.run_id, status=STATUS_DONE, system=state.system,
        messages=messages, round_index=result.rounds, max_rounds=state.max_rounds,
        result_text=result.text, hit_limit=True, created_at=state.created_at,
    ))
    return RunStatus(state.run_id, STATUS_DONE, text=result.text,
                     rounds=result.rounds, hit_limit=True)


def _suspend(state: RunState, registry: Registry, store: RunStore,
             pending_uses: list[ToolUse], results: list[dict]) -> RunStatus:
    """Persist everything needed to continue, then return without blocking."""
    head = pending_uses[0]
    args = dict(head.input)
    pending = PendingCall(
        # Distinct prefix from ApprovalStore's `ap_`: this is the TICKET a human
        # decides, minted before any ApprovalRecord exists. The record the gate
        # checks at execution time is created later, inside the guard, from the
        # args stored here.
        approval_id=f"pa_{uuid4().hex[:12]}",
        tool=head.name,
        args=args,
        payload_hash=payload_hash(head.name, args),
        rendered=_render(registry, head.name, args),
        uses=[u.to_stored() for u in pending_uses],
        results=results,
    )
    saved = store.save(RunState(
        run_id=state.run_id, status=STATUS_AWAITING, system=state.system,
        messages=state.messages, round_index=state.round_index,
        max_rounds=state.max_rounds, pending=pending, decision=None,
        created_at=state.created_at,
    ))
    log_action("run_suspended", run_id=saved.run_id, approval_id=pending.approval_id,
               tool=pending.tool, payload_hash=pending.payload_hash,
               round=state.round_index + 1)
    return RunStatus(saved.run_id, STATUS_AWAITING, rounds=state.round_index,
                     approval_id=pending.approval_id, tool=pending.tool,
                     rendered=pending.rendered,
                     detail="waiting on a human decision")


def _decision_of(pending: PendingCall, raw: str | None) -> Decision | None:
    """Only two strings authorise anything. Everything else waits."""
    if raw == Decision.APPROVED.value:
        return Decision.APPROVED
    if raw == Decision.DENIED.value:
        return Decision.DENIED
    if raw not in (None, "", Decision.PENDING.value):
        log_action("run_decision_unrecognised", approval_id=pending.approval_id,
                   tool=pending.tool, decision=str(raw))
    return None


def _fail(store: RunStore, state: RunState, detail: str) -> RunStatus:
    """Mark the run failed. The pending tool has not run and will not be retried."""
    log_action("run_failed_closed", run_id=state.run_id, detail=detail,
               approval_id=(state.pending.approval_id if state.pending else None))
    try:
        store.save(RunState(
            run_id=state.run_id, status=STATUS_FAILED, system=state.system,
            messages=state.messages, round_index=state.round_index,
            max_rounds=state.max_rounds, detail=detail, created_at=state.created_at,
        ))
    except StorageError:
        # Storage is what failed in at least one path that reaches here. The
        # verdict for THIS invocation still stands: nothing executed.
        pass
    return RunStatus(state.run_id, STATUS_FAILED, rounds=state.round_index, detail=detail)
