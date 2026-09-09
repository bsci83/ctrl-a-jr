"""Tool registration and dispatch metadata.

`mutating` is the only thing standing between the agent and an unapproved
side effect, so it is a required field with no default. Forgetting it is a
TypeError, not a silent `False`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .types import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    mutating: bool
    run: Callable[..., ToolResult]
    render: Callable[..., str] | None = field(default=None)

    def render_for_approval(self, args: dict) -> str:
        """What the human sees. Falls back to canonical args, never nothing."""
        if self.render is not None:
            try:
                return self.render(**args)
            except Exception as exc:  # noqa: BLE001
                return f"(render failed: {exc!r})\n{args!r}"
        return "\n".join(f"{k}: {v}" for k, v in sorted(args.items()))


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def mutating_names(self) -> list[str]:
        return sorted(n for n, s in self._tools.items() if s.mutating)

    def schemas(self) -> list[dict]:
        """Anthropic tool-definition shape."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.schema}
            for s in (self._tools[n] for n in self.names())
        ]
