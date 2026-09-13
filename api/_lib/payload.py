"""The payload hash, recomputed server-side.

A deliberate duplicate of `ctrl_a_jr.approval.payload_hash`. The functions in
api/ ship to Vercel without `src/` on the path, so importing the original is not
available here; `tests/test_api_push_poll.py::test_canonical_hash_matches_agent`
asserts the two implementations agree, because a silent divergence would make
every pushed approval fail verification with no clue why.

Why the server recomputes at all: when the push carries `args`, the Slack and web
renderings are then provably derived from the same bytes the hash covers, which
is spec §5 P3 (the approver approves exactly what executes) extended to the
remote surface. Without it a caller could push honest args and a hash for
something else.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)


def payload_hash(tool: str, args: dict) -> str:
    material = canonical_json({"tool": tool, "args": args})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
