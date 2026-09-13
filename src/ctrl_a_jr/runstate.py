"""Durable run state, over the Turso HTTP pipeline API.

Why HTTP and not a driver: the dependency budget is exactly four (anthropic,
httpx, pytest, ruff). A libsql driver would be a fifth, and the pipeline
endpoint is plain JSON over httpx.

What this file is actually protecting
-------------------------------------
The pending tool's arguments ARE the payload that will execute. `approval.verify`
re-derives `payload_hash` from the args held at EXECUTION time and compares it to
the hash the human approved (property P3). Once execution happens in a different
process from approval, those args have to survive a serialisation round-trip
byte-for-byte in the eyes of that hash, or a legitimate approved call aborts with
"payload integrity check failed" and nobody can tell it from a real tamper.

So args are stored as `approval.canonical_json` output — the SAME serialisation
the hash is computed over — and the stored hash travels beside them so a resume
can re-derive and compare BEFORE dispatching, not only inside the guard.

The Turso auth token is never logged, never put in an exception message, and
never written to the activity log.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from . import activity
from .approval import canonical_json, payload_hash

# The four statuses a logical run can be in. `running` with a pending approval
# recorded means a previous invocation CLAIMED that approval and never came
# back — the pending tool may or may not have executed, so it is never retried.
STATUS_RUNNING = "running"
STATUS_AWAITING = "awaiting_approval"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jr_runs (
  run_id               TEXT PRIMARY KEY,
  status               TEXT NOT NULL,
  system               TEXT NOT NULL,
  messages             TEXT NOT NULL,
  round_index          INTEGER NOT NULL,
  max_rounds           INTEGER NOT NULL,
  pending_approval_id  TEXT,
  pending_tool         TEXT,
  pending_args         TEXT,
  pending_hash         TEXT,
  pending_rendered     TEXT,
  pending_uses         TEXT,
  pending_results      TEXT,
  decision             TEXT,
  result_text          TEXT,
  hit_limit            INTEGER NOT NULL DEFAULT 0,
  detail               TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
)
"""

_COLUMNS = (
    "run_id", "status", "system", "messages", "round_index", "max_rounds",
    "pending_approval_id", "pending_tool", "pending_args", "pending_hash",
    "pending_rendered", "pending_uses", "pending_results", "decision",
    "result_text", "hit_limit", "detail", "created_at", "updated_at",
)


class StorageError(RuntimeError):
    """Any failure to read or write run state.

    Callers treat this as "we do not know the state of this run", which always
    means the pending tool does NOT execute.
    """


def set_active_run_id(run_id: str) -> None:
    """Make every subsequent activity event in THIS process belong to `run_id`.

    `activity.log_action` reads the module-level `RUN_ID` at call time, so
    rebinding it redirects the rest of the process's events. That indirection is
    the point: one logical run now spans many serverless invocations, and the
    eval harness groups by `run_id`. If each invocation kept its own per-process
    id, `check_denial_handling` and `check_gate_integrity` would be computed over
    fragments of a run — and a denial in fragment 1 followed by the approved
    retry-free continuation in fragment 2 would not even be comparable. The
    verdict would be arithmetic over pieces, which is worse than no verdict.

    This is deliberately the SAME mechanism `cli.py` already uses when it mints a
    fresh id after a provider switch, rather than a parallel one. It is process
    global, so a process handling two logical runs must call this once per run;
    `resume.advance` does exactly that, at entry, before anything else logs.
    """
    activity.RUN_ID = run_id


def new_run_id() -> str:
    """Same shape as `activity.RUN_ID`, because it goes in the same field."""
    return uuid4().hex[:12]


@dataclass(frozen=True)
class PendingCall:
    """The one tool call a human has been asked about but not yet decided.

    `uses` is the REMAINING tool_use blocks of the suspended turn, emission order,
    with this call at index 0; `results` is the tool_results already produced in
    that turn, also emission order. Holding both is what lets a resume rebuild the
    turn with exactly one result per use, in order (invariant 2), across a process
    boundary.
    """

    approval_id: str
    tool: str
    args: dict
    payload_hash: str
    rendered: str
    uses: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RunState:
    run_id: str
    status: str
    system: str
    messages: list[dict]
    round_index: int
    max_rounds: int
    pending: PendingCall | None = None
    decision: str | None = None
    result_text: str = ""
    hit_limit: bool = False
    detail: str = ""
    created_at: str = ""
    updated_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------
# Turso pipeline wire format
# --------------------------------------------------------------------------

def _to_arg(value: object) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_cell(cell: object) -> object:
    if not isinstance(cell, dict):
        return cell
    kind = cell.get("type")
    if kind == "null":
        return None
    value = cell.get("value")
    if kind == "integer":
        return int(value)
    if kind == "float":
        return float(value)
    return value


def http_transport(url: str, token: str, timeout: float = 15.0) -> Callable[[dict], dict]:
    """The real transport. Imported lazily so tests never need httpx installed."""

    def send(payload: dict) -> dict:
        import httpx

        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport detail, not the token
            raise StorageError(f"run-state request failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            # Body only, never headers: the Authorization header carries the token
            # and an exception message ends up in logs and in operator output.
            raise StorageError(
                f"run-state request rejected ({response.status_code}): {response.text[:200]}"
            ) from None
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            raise StorageError("run-state response was not JSON") from None

    return send


def turso_pipeline_url(database_url: str) -> str:
    """`libsql://x.turso.io` and `https://x.turso.io` both mean the same host."""
    url = database_url.strip().rstrip("/")
    for prefix in ("libsql://", "wss://", "ws://"):
        if url.startswith(prefix):
            url = "https://" + url[len(prefix):]
            break
    if not url.startswith("http"):
        url = "https://" + url
    if not url.endswith("/v2/pipeline"):
        url = url + "/v2/pipeline"
    return url


class RunStore:
    """Run state, one row per logical run."""

    def __init__(self, transport: Callable[[dict], dict]) -> None:
        self._send = transport

    @classmethod
    def from_env(cls) -> RunStore:
        database_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
        token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        if not database_url or not token:
            # Named, not printed: TURSO_AUTH_TOKEN's VALUE must never reach a log.
            raise StorageError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must both be set to persist run state"
            )
        return cls(http_transport(turso_pipeline_url(database_url), token))

    # -- low level ---------------------------------------------------------

    def _execute(self, statements: list[tuple[str, list]]) -> list[dict]:
        requests: list[dict] = [
            {"type": "execute", "stmt": {"sql": sql, "args": [_to_arg(a) for a in args]}}
            for sql, args in statements
        ]
        requests.append({"type": "close"})
        try:
            body = self._send({"requests": requests})
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - every transport failure is ONE kind
            # Normalised here so callers have a single thing to catch. A transport
            # exception escaping as its own type would sail past `advance`'s
            # fail-closed handler and surface as a crash of unknown consequence.
            # Type name only: the request carried the Authorization header.
            raise StorageError(f"run-state transport failed: {type(exc).__name__}") from None
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise StorageError("run-state response had no results")
        out: list[dict] = []
        for entry in body["results"]:
            if not isinstance(entry, dict) or entry.get("type") != "ok":
                message = "unknown error"
                if isinstance(entry, dict) and isinstance(entry.get("error"), dict):
                    message = str(entry["error"].get("message", message))
                raise StorageError(f"run-state statement failed: {message}")
            response = entry.get("response") or {}
            if response.get("type") == "execute":
                out.append(response.get("result") or {})
        return out

    def _rows(self, result: dict) -> list[dict]:
        cols = [c.get("name") for c in (result.get("cols") or [])]
        return [
            {name: _from_cell(cell) for name, cell in zip(cols, row, strict=False)}
            for row in (result.get("rows") or [])
        ]

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> None:
        self._execute([(SCHEMA, [])])

    # -- serialisation -----------------------------------------------------

    @staticmethod
    def _dump_messages(messages: list[dict]) -> str:
        # No `default=` here on purpose. A value the JSON encoder cannot represent
        # must surface as a storage error while the pending tool has NOT run, not
        # be coerced to repr() and silently change the history the model sees.
        try:
            return json.dumps(messages, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"message history is not serialisable: {exc}") from None

    @staticmethod
    def _load_messages(raw: object) -> list[dict]:
        if not isinstance(raw, str):
            raise StorageError("stored message history is missing")
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(f"stored message history is corrupt: {exc}") from None
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise StorageError("stored message history is not a list of messages")
        for message in messages:
            if message.get("role") not in {"user", "assistant"}:
                raise StorageError(f"stored message has no usable role: {message!r}")
        return messages

    @staticmethod
    def _load_json_list(raw: object, what: str) -> list:
        if raw in (None, ""):
            return []
        if not isinstance(raw, str):
            raise StorageError(f"stored {what} is not text")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(f"stored {what} is corrupt: {exc}") from None
        if not isinstance(value, list):
            raise StorageError(f"stored {what} is not a list")
        return value

    def _row_to_state(self, row: dict) -> RunState:
        pending = None
        if row.get("pending_approval_id"):
            raw_args = row.get("pending_args")
            if not isinstance(raw_args, str):
                raise StorageError("pending call has no stored arguments")
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                raise StorageError(f"pending arguments are corrupt: {exc}") from None
            if not isinstance(args, dict):
                raise StorageError("pending arguments are not an object")
            pending = PendingCall(
                approval_id=str(row["pending_approval_id"]),
                tool=str(row.get("pending_tool") or ""),
                args=args,
                payload_hash=str(row.get("pending_hash") or ""),
                rendered=str(row.get("pending_rendered") or ""),
                uses=self._load_json_list(row.get("pending_uses"), "pending tool calls"),
                results=self._load_json_list(row.get("pending_results"), "pending tool results"),
            )
        return RunState(
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            system=str(row.get("system") or ""),
            messages=self._load_messages(row.get("messages")),
            round_index=int(row.get("round_index") or 0),
            max_rounds=int(row.get("max_rounds") or 0),
            pending=pending,
            decision=(str(row["decision"]) if row.get("decision") else None),
            result_text=str(row.get("result_text") or ""),
            hit_limit=bool(row.get("hit_limit")),
            detail=str(row.get("detail") or ""),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    # -- operations --------------------------------------------------------

    def create_run(self, system: str, user: str, max_rounds: int,
                   run_id: str | None = None) -> RunState:
        self.ensure_schema()
        state = RunState(
            run_id=run_id or new_run_id(),
            status=STATUS_RUNNING,
            system=system,
            messages=[{"role": "user", "content": user}],
            round_index=0,
            max_rounds=max_rounds,
            created_at=_now(),
            updated_at=_now(),
        )
        self._execute([(
            f"INSERT INTO jr_runs ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))})",
            [
                state.run_id, state.status, state.system,
                self._dump_messages(state.messages), state.round_index, state.max_rounds,
                None, None, None, None, None, None, None, None,
                "", 0, "", state.created_at, state.updated_at,
            ],
        )])
        return state

    def load(self, run_id: str) -> RunState | None:
        # DDL rides along in the same pipeline request so a cold database is one
        # round trip, not two — and so "no such table" can never be mistaken for
        # "no such run", which is the difference between fail-closed and silence.
        results = self._execute([
            (SCHEMA, []),
            (f"SELECT {', '.join(_COLUMNS)} FROM jr_runs WHERE run_id = ?", [run_id]),
        ])
        rows = self._rows(results[-1]) if results else []
        if not rows:
            return None
        return self._row_to_state(rows[0])

    def save(self, state: RunState) -> RunState:
        pending = state.pending
        updated = replace(state, updated_at=_now())
        self._execute([(
            "UPDATE jr_runs SET status = ?, system = ?, messages = ?, round_index = ?, "
            "max_rounds = ?, pending_approval_id = ?, pending_tool = ?, pending_args = ?, "
            "pending_hash = ?, pending_rendered = ?, pending_uses = ?, pending_results = ?, "
            "decision = ?, result_text = ?, hit_limit = ?, detail = ?, updated_at = ? "
            "WHERE run_id = ?",
            [
                updated.status, updated.system, self._dump_messages(updated.messages),
                updated.round_index, updated.max_rounds,
                pending.approval_id if pending else None,
                pending.tool if pending else None,
                # canonical_json, NOT json.dumps: this string is re-parsed and
                # re-hashed at execution time and compared to what the human
                # approved. Matching approval.payload_hash's serialisation is what
                # keeps a legitimate approved call from aborting as a tamper (P3).
                canonical_json(pending.args) if pending else None,
                pending.payload_hash if pending else None,
                pending.rendered if pending else None,
                json.dumps(pending.uses, ensure_ascii=False) if pending else None,
                json.dumps(pending.results, ensure_ascii=False) if pending else None,
                updated.decision, updated.result_text, int(updated.hit_limit),
                updated.detail, updated.updated_at, updated.run_id,
            ],
        )])
        return updated

    def record_decision(self, run_id: str, approval_id: str, decision: str) -> bool:
        """Attach a human's decision to the pending call.

        Conditional on the run still awaiting THAT approval, so a decision for a
        stale ticket cannot authorise whatever the run is waiting on now.
        Returns False when nothing matched; it never creates an authorisation.
        """
        results = self._execute([(
            "UPDATE jr_runs SET decision = ?, updated_at = ? "
            "WHERE run_id = ? AND status = ? AND pending_approval_id = ? AND decision IS NULL",
            [decision, _now(), run_id, STATUS_AWAITING, approval_id],
        )])
        return bool(results and results[0].get("affected_row_count"))

    def claim_pending(self, run_id: str, approval_id: str) -> bool:
        """Take exclusive ownership of the decided call, once.

        This is the at-most-once guarantee, and it is a compare-and-set in the
        database rather than a check in Python, because two invocations can run
        concurrently (a retry, a double-clicked webhook) and a read-then-write
        would let both pass. Exactly one caller sees affected_row_count == 1; the
        loser executes nothing.

        The row is left in `running` WITH the pending call still recorded. That
        combination means "claimed, outcome unknown", and `advance` refuses to
        touch such a run — the tool may already have gone out.
        """
        results = self._execute([(
            "UPDATE jr_runs SET status = ?, updated_at = ? "
            "WHERE run_id = ? AND status = ? AND pending_approval_id = ? "
            "AND decision IN ('approved', 'denied')",
            [STATUS_RUNNING, _now(), run_id, STATUS_AWAITING, approval_id],
        )])
        return bool(results and results[0].get("affected_row_count"))

    def pending_for(self, run_id: str) -> PendingCall | None:
        state = self.load(run_id)
        return state.pending if state and state.status == STATUS_AWAITING else None


def pending_payload_hash(tool: str, args: dict) -> str:
    """Re-export so a resume never reaches for a second hashing implementation."""
    return payload_hash(tool, args)


def round_trip_args(args: dict) -> Any:
    """Exactly what storage does to a pending call's arguments.

    Exposed so the P3 round-trip property can be tested directly rather than
    inferred from a passing end-to-end run.
    """
    return json.loads(canonical_json(args))
