"""Append-only evidence log.

This is not a debug convenience. It is the only input the eval harness reads,
so two rules are load-bearing:

1. Attribution is applied AFTER the caller's payload, so a caller cannot
   overwrite `agent`, `pid` or `ts`. An audit log a caller can forge is not
   evidence of anything.
2. Writing never raises. A logging failure must not abort an agent run, and a
   value that will not serialise is coerced rather than dropped.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_LOG = Path.home() / ".ctrl-a" / "jr" / "activity.jsonl"


def log_path() -> Path:
    override = os.environ.get("CTRLA_JR_ACTIVITY_LOG", "").strip()
    return Path(override) if override else DEFAULT_LOG


def _agent() -> str:
    return os.environ.get("CTRLA_JR_AGENT", "ctrl-a-jr")



# One id per process, stamped on every event. `ctrl-a-jr run` is one process, so
# this is the run boundary.
#
# Without it the whole log is one undifferentiated sequence, and three things
# break: "0 unapproved actions across N runs" cannot be computed because nothing
# can count N; provider stability spans configurations; and — the live bug —
# check_denial_handling reads a denial in one run followed by an approved call to
# the same tool in a LATER run as the agent retrying after a refusal, and fails a
# run that was correct. Deny once to demo the gate, run again and approve, and the
# harness reports a failure that never happened.
RUN_ID = uuid4().hex[:12]


def log_action(event: str, **fields: object) -> None:
    """Append one event. Never raises."""
    try:
        record: dict[str, object] = dict(fields)
        # Attribution LAST — the caller cannot overwrite these.
        record["event"] = event
        record["agent"] = _agent()
        record["run_id"] = RUN_ID
        record["pid"] = os.getpid()
        record["ts"] = datetime.now(UTC).isoformat()

        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=repr, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    # Deliberate: an audit log that can abort an agent run is worse than no log.
    # log_action is called from the security chokepoint; it must never raise.
    except Exception:  # noqa: BLE001, S110
        pass


def read_log(path: Path | None = None) -> list[dict]:
    """Read the log back. Malformed lines are skipped, not fatal."""
    p = path or log_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
