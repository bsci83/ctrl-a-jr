"""The single chokepoint.

Every tool call in the system goes through `Guard.dispatch`. There is no
second path, no bypass flag and no per-tool exemption — the only way to
create a hole is to misclassify a tool as non-mutating, which is why the
tool list is small and hand-audited.
"""

from __future__ import annotations

from typing import Protocol

from .activity import log_action
from .approval import ApprovalStore
from .registry import Registry
from .types import ApprovalRecord, Decision, ToolResult


class Approver(Protocol):
    def decide(self, record: ApprovalRecord) -> Decision: ...


class Guard:
    def __init__(self, registry: Registry, store: ApprovalStore, approver: Approver) -> None:
        self.registry = registry
        self.store = store
        self.approver = approver

    def dispatch(self, name: str, args: dict) -> ToolResult:
        spec = self.registry.get(name)
        if spec is None:
            log_action("tool_refused", tool=name, reason="unknown_tool")
            return ToolResult(False, "", f"unknown tool {name!r}")

        if spec.mutating:
            record = self.store.request(name, args, spec.render_for_approval(args))
            decision = self.approver.decide(record)
            self.store.resolve(record.id, decision)

            if decision is not Decision.APPROVED:
                log_action("tool_refused", tool=name, reason="denied_by_operator",
                           approval_id=record.id)
                return ToolResult(
                    False, "",
                    "denied by the operator. Do not retry this call or attempt it "
                    "by another route; report the denial and stop.",
                )

            if not self.store.verify(record.id, args):
                log_action("payload_mismatch", tool=name, approval_id=record.id)
                return ToolResult(False, "", "payload integrity check failed; call aborted")

        try:
            result = spec.run(**args)
        except Exception as exc:  # noqa: BLE001 - a failing tool must not abort the run
            log_action("tool_call", tool=name, ok=False, error=repr(exc))
            return ToolResult(False, "", str(exc))

        log_action("tool_call", tool=name, ok=result.ok, mutating=spec.mutating,
                   result_chars=len(result.content))
        return result
