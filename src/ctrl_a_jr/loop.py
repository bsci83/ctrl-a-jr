"""The agent loop. Hand-rolled: no framework.

Four invariants, each present because omitting it produces a specific failure:

1. MAX_ROUNDS bounds the run, and at the boundary a final turn is issued with
   NO tools. Otherwise a run can end holding an unanswered tool_use block,
   which is an API error rather than a result.
2. Exactly one tool_result per tool_use, in the order the model emitted them.
3. A failing tool becomes an error tool_result; it never raises out of the loop.
4. Scope is never taken from the model — see the tool implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .activity import log_action
from .guard import Guard

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


def run_loop(
    client: ModelClient,
    guard: Guard,
    system: str,
    user: str,
    max_rounds: int = MAX_ROUNDS,
) -> LoopResult:
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = guard.registry.schemas()

    for round_index in range(max_rounds):
        log_action("model_turn", provider=getattr(client, "provider", "unknown"),
                   model=getattr(client, "model", "unknown"), round=round_index + 1)
        response = client.create(system=system, messages=messages, tools=tools)
        uses = _tool_uses(response)

        if not uses:
            return LoopResult(_text_of(response), round_index + 1, hit_limit=False)

        messages.append(_assistant_turn(response))

        # Invariant 2: one result per use, in emission order.
        results = []
        for use in uses:
            outcome = guard.dispatch(use.name, dict(use.input))
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": outcome.to_model(),
                "is_error": not outcome.ok,
            })
        messages.append({"role": "user", "content": results})

    # Invariant 1: the boundary turn offers NO tools, so nothing can dangle.
    log_action("round_limit_reached", max_rounds=max_rounds)
    log_action("model_turn", provider=getattr(client, "provider", "unknown"),
               model=getattr(client, "model", "unknown"), round=max_rounds + 1)
    final = client.create(system=system, messages=messages, tools=[])
    return LoopResult(_text_of(final), max_rounds + 1, hit_limit=True)
