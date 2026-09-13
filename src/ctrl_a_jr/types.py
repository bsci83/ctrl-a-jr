from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class ToolResult:
    """What every tool returns. Failure is a value, never an exception."""

    ok: bool
    content: str
    error: str | None = None

    def to_model(self) -> str:
        """The string the model sees as its tool_result."""
        return self.content if self.ok else f"ERROR: {self.error}"


class Decision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    tool: str
    payload_hash: str
    rendered: str
    decision: Decision
    note: str | None = None
    # The exact arguments this approval authorises. The display is DERIVED from
    # these, so what the human sees cannot drift from what executes.
    args: dict = field(default_factory=dict)
