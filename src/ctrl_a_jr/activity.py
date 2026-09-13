"""Append-only evidence log.

This is not a debug convenience. It is the only input the eval harness reads,
so two rules are load-bearing:

1. Attribution is applied AFTER the caller's payload, so a caller cannot
   overwrite `agent`, `pid` or `ts`. An audit log a caller can forge is not
   evidence of anything.
2. Writing never raises. A logging failure must not abort an agent run, and a
   value that will not serialise is coerced rather than dropped.

Rule 2 used to be absolute, and that was the hole: a swallowed write, a
half-written line and an absent file all produced the same thing a clean
read produces — a shorter list — so the harness scored a damaged evidence
stream as a quiet run. Failures are still never raised; they are now
COUNTED, and `read_log_with_integrity` reports them so a verdict cannot come
back green over a log that lost events.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_LOG = Path.home() / ".ctrl-a" / "jr" / "activity.jsonl"


def log_path() -> Path:
    override = os.environ.get("CTRLA_JR_ACTIVITY_LOG", "").strip()
    return Path(override) if override else DEFAULT_LOG


def errors_path(path: Path | None = None) -> Path:
    """Sidecar for write failures.

    The obvious place to report "the log write failed" is the log, which is
    precisely what just failed. A separate file is the only channel left that
    survives to the next process — and the eval runs in a different process from
    the agent, so an in-memory counter would never reach the verdict.
    """
    return (path or log_path()).with_name((path or log_path()).name + ".errors")


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
    # log_action is called from the security chokepoint; it must never raise —
    # but it must not vanish either, so the failure gets a breadcrumb.
    except Exception as exc:  # noqa: BLE001
        _record_write_failure(exc)


def _record_write_failure(exc: BaseException) -> None:
    """Best-effort breadcrumb. Every step is individually guarded: this runs
    because something already failed, and it must not fail louder."""
    try:
        sidecar = errors_path()
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with sidecar.open("a", encoding="utf-8") as fh:
            fields = (datetime.now(UTC).isoformat(), RUN_ID, repr(exc))
            fh.write(chr(9).join(fields) + chr(10))
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        print(f"ctrl-a-jr: activity log write FAILED ({exc!r}) — "
              f"this run's evidence is incomplete", file=sys.stderr)
    except Exception:  # noqa: BLE001, S110
        pass


@dataclass(frozen=True)
class LogIntegrity:
    """Whether the evidence stream is whole. Absence of events is not evidence
    of absence of actions."""

    path: str
    exists: bool
    records: int
    malformed_lines: int
    write_failures: int

    @property
    def sound(self) -> bool:
        return self.exists and self.malformed_lines == 0 and self.write_failures == 0

    def describe(self) -> str:
        losses = []
        if self.malformed_lines:
            losses.append(f"{self.malformed_lines} unparseable line(s)")
        if self.write_failures:
            losses.append(f"{self.write_failures} failed write(s)")
        if not self.exists:
            # An absent log and a failed write are usually the SAME fact, and
            # reporting only the absence throws away the reason for it.
            absent = f"no activity log at {self.path} — nothing was recorded"
            return absent if not losses else f"{absent}: {' and '.join(losses)}"
        if not losses:
            return f"{self.records} record(s), no losses"
        return (f"{self.records} record(s) read, but the log lost events: "
                + " and ".join(losses))


def read_log(path: Path | None = None) -> list[dict]:
    """Read the log back. Malformed lines are skipped, not fatal."""
    return read_log_with_integrity(path)[0]


def read_log_with_integrity(path: Path | None = None) -> tuple[list[dict], LogIntegrity]:
    """Read the log AND say what was lost getting there.

    A malformed line is still skipped rather than fatal — a truncated final write
    must not make the whole log unreadable — but the count travels with the
    records so a caller can refuse to score an incomplete stream.
    """
    p = path or log_path()
    out: list[dict] = []
    malformed = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    return out, LogIntegrity(
        path=str(p),
        exists=p.exists(),
        records=len(out),
        malformed_lines=malformed,
        write_failures=_count_write_failures(p),
    )


def _count_write_failures(path: Path) -> int:
    try:
        sidecar = errors_path(path)
        if not sidecar.exists():
            return 0
        return sum(1 for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:  # noqa: BLE001 - a missing breadcrumb must not break the read
        return 0
