"""The agent loop. Hand-rolled: no framework.

Four invariants, each present because omitting it produces a specific failure:

1. MAX_ROUNDS bounds the run, and at the boundary a final turn is issued with
   NO tools. Otherwise a run can end holding an unanswered tool_use block,
   which is an API error rather than a result.
2. Exactly one tool_result per tool_use, in the order the model emitted them.
3. A failing tool becomes an error tool_result; it never raises out of the loop.
4. Scope is never taken from the model — see the tool implementations.

There are now TWO drivers of this loop: `run_loop`, which blocks while a human
decides, and `resume.advance`, which suspends instead. They share `run_round`
and `execute_uses` rather than each carrying a copy of the invariants — a
second copy is a second place for them to drift, and invariant 2 fails on a
LATER turn, far from whichever copy broke it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .activity import log_action
from .guard import Guard
from .types import ToolResult

# 5 was one round short of the real task. A live run spent an extra round on a
# second gmail_read_thread and hit round_limit_reached before it could write its
# report, so the operator got a forced summary instead of the artifact. The
# boundary behaviour is unchanged — invariant 1 still issues a final turn with no
# tools; only the budget moved.
MAX_ROUNDS = 8


class ModelClient(Protocol):
    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Any: ...


@dataclass(frozen=True)
class LoopResult:
    text: str
    rounds: int
    hit_limit: bool


@dataclass(frozen=True)
class ToolUse:
    """One tool_use block, normalised.

    `run_loop` gets these as SDK objects; `advance` gets them as dicts rehydrated
    from storage after a process boundary. Normalising at the edge is what lets
    both drive the SAME execution code — two block shapes would mean two copies
    of invariant 2.
    """

    id: str
    name: str
    input: dict

    @classmethod
    def from_block(cls, block: Any) -> ToolUse:
        return cls(id=block.id, name=block.name, input=dict(block.input))

    @classmethod
    def from_stored(cls, raw: object) -> ToolUse:
        """Rehydrate a persisted tool_use, refusing anything unanswerable.

        A use missing its id cannot be answered with a matching tool_result, and
        the resulting unpaired tool_use is an API 400 on the NEXT turn. Refusing
        here turns that into a fail-closed storage error at a point where the
        pending tool has not run.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"corrupt stored tool_use (not an object): {raw!r}")
        use_id, name, args = raw.get("id"), raw.get("name"), raw.get("input", {})
        if not isinstance(use_id, str) or not use_id:
            raise ValueError(f"corrupt stored tool_use (no id): {raw!r}")
        if not isinstance(name, str) or not name:
            raise ValueError(f"corrupt stored tool_use (no name): {raw!r}")
        if not isinstance(args, dict):
            raise ValueError(f"corrupt stored tool_use (input is not an object): {raw!r}")
        return cls(id=use_id, name=name, input=dict(args))

    def to_stored(self) -> dict:
        return {"id": self.id, "name": self.name, "input": dict(self.input)}


# A gate answers one tool_use. Returning None means "no human decision exists for
# this call yet" and suspends the round; only the resumable driver uses that.
Gate = Callable[[ToolUse], "ToolResult | None"]


def _text_of(response: Any) -> str:
    return "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text" and b.text)


def _tool_uses(response: Any) -> list[Any]:
    return [b for b in response.content if getattr(b, "type", "") == "tool_use"]


def _assistant_turn(response: Any) -> dict:
    """Rebuild the assistant turn for the message history.

    Only `text` and `tool_use` blocks round-trip. Any other block type is
    DROPPED — we cannot faithfully re-serialise a shape we do not model — but
    the drop is logged rather than silent, because a dropped `thinking` block
    breaks the signature chain and the resulting 400 arrives on a LATER turn,
    far from its cause.

    This runs ONCE per turn, before persistence. Storage round-trips the dicts
    produced here and never re-derives them from a model object, so a resumed
    run cannot drop a second time or drop without logging.
    """
    blocks: list[dict] = []
    for b in response.content:
        block_type = getattr(b, "type", "")
        if block_type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif block_type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        else:
            log_action("content_block_dropped", block_type=block_type or "unknown")
    return {"role": "assistant", "content": blocks}


def result_block(use: ToolUse, outcome: ToolResult) -> dict:
    """The one place a tool_result is shaped, so both drivers emit the same thing."""
    return {
        "type": "tool_result",
        "tool_use_id": use.id,
        "content": outcome.to_model(),
        "is_error": not outcome.ok,
    }


def dispatch_gate(guard: Guard) -> Gate:
    """The blocking gate: every use goes straight to the chokepoint.

    Never returns None, so a round driven by this gate always answers every
    tool_use in the turn.
    """

    def gate(use: ToolUse) -> ToolResult:
        return guard.dispatch(use.name, dict(use.input))

    return gate


def execute_uses(uses: list[ToolUse], gate: Gate) -> tuple[list[dict], list[ToolUse]]:
    """Invariant 2, in the only place it is implemented.

    Returns (results, remaining). `results` holds one tool_result per use
    ANSWERED, in emission order. `remaining` is empty unless the gate suspended,
    in which case remaining[0] is the use that needs a decision and the rest are
    the uses after it, still in emission order. Concatenating results with the
    results of a later `execute_uses(remaining, ...)` reproduces the original
    order exactly — which is what makes a resume across a process boundary safe.
    """
    results: list[dict] = []
    for index, use in enumerate(uses):
        outcome = gate(use)
        if outcome is None:
            return results, list(uses[index:])
        results.append(result_block(use, outcome))
    return results, []


@dataclass(frozen=True)
class RoundStep:
    """One model turn plus whatever tool work it triggered."""

    done: bool                      # the model emitted no tool_use; `text` is the answer
    text: str
    assistant_turn: dict | None     # None only when done
    results: list[dict]             # tool_results produced, emission order
    pending: list[ToolUse]          # non-empty => the gate suspended at pending[0]


def run_round(
    client: ModelClient,
    system: str,
    messages: list[dict],
    tools: list[dict],
    round_index: int,
    gate: Gate,
) -> RoundStep:
    """One round: ask the model, then answer every tool_use it emitted.

    `messages` is READ, never mutated — the caller owns history, because the
    resumable driver has to persist a specific prefix of it.
    """
    log_action("model_turn", provider=getattr(client, "provider", "unknown"),
               model=getattr(client, "model", "unknown"), round=round_index + 1)
    response = client.create(system=system, messages=messages, tools=tools)
    uses = [ToolUse.from_block(b) for b in _tool_uses(response)]

    if not uses:
        return RoundStep(True, _text_of(response), None, [], [])

    assistant_turn = _assistant_turn(response)
    results, pending = execute_uses(uses, gate)
    return RoundStep(False, _text_of(response), assistant_turn, results, pending)


def final_turn(client: ModelClient, system: str, messages: list[dict],
               max_rounds: int) -> LoopResult:
    """Invariant 1: the boundary turn offers NO tools, so nothing can dangle."""
    log_action("round_limit_reached", max_rounds=max_rounds)
    log_action("model_turn", provider=getattr(client, "provider", "unknown"),
               model=getattr(client, "model", "unknown"), round=max_rounds + 1)
    final = client.create(system=system, messages=messages, tools=[])
    return LoopResult(_text_of(final), max_rounds + 1, hit_limit=True)


def run_loop(
    client: ModelClient,
    guard: Guard,
    system: str,
    user: str,
    max_rounds: int = MAX_ROUNDS,
) -> LoopResult:
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = guard.registry.schemas()
    gate = dispatch_gate(guard)

    for round_index in range(max_rounds):
        step = run_round(client, system, messages, tools, round_index, gate)
        if step.done:
            return LoopResult(step.text, round_index + 1, hit_limit=False)
        if step.pending:
            # Unreachable with `dispatch_gate`, which always returns a result. If
            # it ever were reachable, the turn would carry an unanswered tool_use
            # and fail on the NEXT request instead of here — so it fails here.
            raise RuntimeError(
                "the blocking gate left tool_use blocks unanswered; invariant 2 "
                f"would break on the next turn ({[u.name for u in step.pending]})"
            )
        messages.append(step.assistant_turn)
        messages.append({"role": "user", "content": step.results})

    return final_turn(client, system, messages, max_rounds)
