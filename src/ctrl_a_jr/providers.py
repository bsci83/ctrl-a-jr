"""Inference providers, and the gated switch between them.

Failover is NOT automatic. Changing the model changes what the agent is, and a
run that begins on one model and silently finishes on another is not the system
the operator authorized. `provider_switch` is a mutating tool, so it goes
through the same gate as sending an email.

Spec 7a: failover triggers on transport failure, never on a response the model
actually produced — so the model must never be able to call this itself.
`model_callable=False` keeps it out of `registry.schemas()` (what the model is
offered) while leaving it in `registry.mutating_names()` (still dispatchable,
and gated, from the CLI's transport-failure handler).
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
                 provider: str, max_tokens: int = 2048) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.provider = provider
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


def _require_key(name: str) -> str:
    """Name the missing variable instead of raising a KeyError traceback.

    The CLI's credential lookups already do this; these two were bare dict access,
    so a missing model key failed differently from a missing Stripe key. Found in a
    verification pass — an ugly failure here lands mid-run, which is the worst place
    for it.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}. See .env.example.")
    return value


def client_from_env(state: ProviderState) -> AnthropicCompatClient:
    if state.provider == "openrouter":
        return AnthropicCompatClient(
            api_key=_require_key("OPENROUTER_API_KEY"),
            # The Anthropic SDK appends /v1/messages itself. Including /v1 here
            # produced https://openrouter.ai/api/v1/v1/messages — a 404, meaning
            # the entire spec 7a failover path could never have succeeded.
            base_url="https://openrouter.ai/api",
            model=state.model,
            provider="openrouter",
        )
    return AnthropicCompatClient(
        api_key=_require_key("ANTHROPIC_API_KEY"),
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        model=state.model,
        provider=state.provider,
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
        model_callable=False,
        run=switch,
        render=lambda provider, model, reason: (
            f"Switch inference from {state.provider}/{state.model} to {provider}/{model}\n\n"
            f"Reason given: {reason}\n\n"
            f"Approving changes which model produced the rest of this run."
        ),
    ))
