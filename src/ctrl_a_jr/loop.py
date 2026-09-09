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

MAX_ROUNDS = 5


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
    blocks: list[dict] = []
    for b in response.content:
        if b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
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
    final = client.create(system=system, messages=messages, tools=[])
    return LoopResult(_text_of(final), max_rounds + 1, hit_limit=True)
