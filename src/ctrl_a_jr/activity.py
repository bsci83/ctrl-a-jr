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
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = Path.home() / ".ctrl-a" / "jr" / "activity.jsonl"


def log_path() -> Path:
    override = os.environ.get("CTRLA_JR_ACTIVITY_LOG", "").strip()
    return Path(override) if override else DEFAULT_LOG


def _agent() -> str:
    return os.environ.get("CTRLA_JR_AGENT", "ctrl-a-jr")


def log_action(event: str, **fields: object) -> None:
    """Append one event. Never raises."""
    try:
        record: dict[str, object] = dict(fields)
        # Attribution LAST — the caller cannot overwrite these.
        record["event"] = event
        record["agent"] = _agent()
        record["pid"] = os.getpid()
        record["ts"] = datetime.now(timezone.utc).isoformat()

        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=repr, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - logging must never break a run
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
