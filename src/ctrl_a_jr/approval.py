"""Approval records and payload integrity.

The load-bearing rule: the hash is computed HERE, from the arguments the
process holds. A caller never supplies a hash. Without that, an agent can
render one email to the approver and send another, and the approval record
still looks clean.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from .activity import log_action
from .types import ApprovalRecord, Decision


def canonical_json(obj: object) -> str:
    """Stable serialisation: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)


def payload_hash(tool: str, args: dict) -> str:
    """SHA-256 over tool name + canonical arguments."""
    material = canonical_json({"tool": tool, "args": args})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ApprovalStore:
    """In-process approval records. One operator, one run — no locking needed."""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def request(self, tool: str, args: dict, rendered: str) -> ApprovalRecord:
        rec = ApprovalRecord(
            id=f"ap_{uuid.uuid4().hex[:12]}",
            tool=tool,
            payload_hash=payload_hash(tool, args),
            rendered=rendered,
            decision=Decision.PENDING,
        )
        self._records[rec.id] = rec
        log_action(
            "approval_requested",
            approval_id=rec.id,
            tool=tool,
            payload_hash=rec.payload_hash,
            rendered_chars=len(rendered),
        )
        return rec

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    def pending(self) -> list[ApprovalRecord]:
        return [r for r in self._records.values() if r.decision is Decision.PENDING]

    def resolve(self, approval_id: str, decision: Decision, note: str | None = None) -> ApprovalRecord:
        rec = self._records.get(approval_id)
        if rec is None:
            raise KeyError(f"unknown approval {approval_id!r}")
        if rec.decision is not Decision.PENDING:
            raise ValueError(f"approval {approval_id} already {rec.decision.value}")
        updated = ApprovalRecord(
            id=rec.id,
            tool=rec.tool,
            payload_hash=rec.payload_hash,
            rendered=rec.rendered,
            decision=decision,
            note=note,
        )
        self._records[rec.id] = updated
        log_action(
            "approval_resolved",
            approval_id=rec.id,
            tool=rec.tool,
            decision=decision.value,
            payload_hash=rec.payload_hash,
            note=note,
        )
        return updated

    def verify(self, approval_id: str, args: dict) -> bool:
        """Re-derive the hash from what is ABOUT to execute and compare."""
        rec = self._records.get(approval_id)
        if rec is None or rec.decision is not Decision.APPROVED:
            return False
        return payload_hash(rec.tool, args) == rec.payload_hash
