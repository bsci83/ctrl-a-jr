"""Inference providers, and the gated switch between them.

Failover is NOT automatic. Changing the model changes what the agent is, and a
run that begins on one model and silently finishes on another is not the system
the operator authorized. `provider_switch` is a mutating tool, so it goes
through the same gate as sending an email.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .registry import Registry, ToolSpec
from .types import ToolResult


@dataclass
class ProviderState:
    provider: str
    model: str


class AnthropicCompatClient:
    """Anthropic SDK against any Anthropic-compatible base URL (MiniMax, OpenRouter)."""

    def __init__(self, api_key: str, base_url: str | None, model: str,
                 max_tokens: int = 2048) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.max_tokens = max_tokens

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)


def client_from_env(state: ProviderState) -> AnthropicCompatClient:
    if state.provider == "openrouter":
        return AnthropicCompatClient(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            model=state.model,
        )
    return AnthropicCompatClient(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        model=state.model,
    )


def register_provider_tools(registry: Registry, state: ProviderState) -> None:
    def switch(provider: str, model: str, reason: str) -> ToolResult:
        state.provider, state.model = provider, model
        return ToolResult(True, f"switched to {provider}/{model}")

    registry.register(ToolSpec(
        name="provider_switch",
        description="Switch inference provider after a transport failure. Requires human "
                    "approval. Do not call this because you dislike a response — only "
                    "when the provider itself is unreachable.",
        schema={"type": "object",
                "properties": {"provider": {"type": "string"}, "model": {"type": "string"},
                               "reason": {"type": "string"}},
                "required": ["provider", "model", "reason"]},
        mutating=True,
        run=switch,
        render=lambda provider, model, reason: (
            f"Switch inference from {state.provider}/{state.model} to {provider}/{model}\n\n"
            f"Reason given: {reason}\n\n"
            f"Approving changes which model produced the rest of this run."
        ),
    ))
