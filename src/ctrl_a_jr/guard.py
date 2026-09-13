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

        approval_id = None
        if spec.mutating:
            record = None
            try:
                record = self.store.request(name, args, spec.render_for_approval(args))
                decision = self.approver.decide(record)
                self.store.resolve(record.id, decision)
            except Exception as exc:  # noqa: BLE001 - the chokepoint never raises outward
                # Fails closed: the tool has not run, and the attempt is on the record.
                log_action("tool_refused", tool=name, reason="approval_failed",
                           approval_id=(record.id if record is not None else None),
                           error=repr(exc))
                return ToolResult(False, "", f"approval could not be obtained: {exc}")

            if decision is not Decision.APPROVED:
                reason = ("denied_by_operator" if decision is Decision.DENIED
                          else f"not_approved:{decision.value}")
                log_action("tool_refused", tool=name, reason=reason, approval_id=record.id)
                return ToolResult(
                    False, "",
                    "denied by the operator. Do not retry this call or attempt it "
                    "by another route; report the denial and stop.",
                )

            if not self.store.verify(record.id, args):
                log_action("payload_mismatch", tool=name, approval_id=record.id)
                return ToolResult(False, "", "payload integrity check failed; call aborted")

            approval_id = record.id

        try:
            result = spec.run(**args)
        except Exception as exc:  # noqa: BLE001 - a failing tool must not abort the run
            log_action("tool_call", tool=name, ok=False, mutating=spec.mutating,
                       approval_id=approval_id, error=repr(exc))
            return ToolResult(False, "", str(exc))

        # A tool that RETURNS a failure logged ok=False and nothing else, so the
        # first live Slack failure recorded that something went wrong and not what.
        # Diagnosing it needed the API re-called by hand. The exception path already
        # logged an error; this path did not.
        log_action("tool_call", tool=name, ok=result.ok, mutating=spec.mutating,
                   approval_id=approval_id, result_chars=len(result.content),
                   error=(result.error or None) if not result.ok else None)
        return result
