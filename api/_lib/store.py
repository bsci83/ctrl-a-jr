"""Approval storage on Turso, over the plain HTTP pipeline API.

No database driver. The project's dependency budget is exactly four
(anthropic, httpx, pytest, ruff) and a serverless function that drags in a
libsql client to run four statements has bought a cold start for nothing.

The single-use rule lives in SQL, not in Python: `decide()` updates only rows
still `pending`, so two approval surfaces racing on one record (the web page and
a Slack button) cannot both win. A read-then-write in the function would lose
that race roughly as often as a human uses both.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import httpx

DDL = """
CREATE TABLE IF NOT EXISTS approvals (
  approval_id       TEXT PRIMARY KEY,
  tool              TEXT NOT NULL,
  payload_hash      TEXT NOT NULL,
  rendered_artifact TEXT NOT NULL,
  args_json         TEXT,
  created_at        TEXT NOT NULL,
  decision          TEXT NOT NULL DEFAULT 'pending',
  decided_by        TEXT,
  decided_at        TEXT
)
""".strip()

COLUMNS = (
    "approval_id, tool, payload_hash, rendered_artifact, args_json, "
    "created_at, decision, decided_by, decided_at"
)


class StoreUnavailable(RuntimeError):
    """Storage is not configured or not reachable. Never carries the token."""


@dataclass(frozen=True)
class Record:
    approval_id: str
    tool: str
    payload_hash: str
    rendered_artifact: str
    created_at: str
    decision: str = "pending"
    decided_by: str | None = None
    decided_at: str | None = None
    args_json: str | None = None

    def status_payload(self) -> dict:
        return {
            "status": self.decision,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }


def pipeline_url(database_url: str) -> str:
    """`libsql://db.turso.io` and `https://db.turso.io` both name the same HTTP API."""
    url = database_url.strip().rstrip("/")
    for scheme in ("libsql://", "wss://", "ws://"):
        if url.startswith(scheme):
            url = "https://" + url[len(scheme) :]
            break
    if not url.startswith("https://"):
        raise StoreUnavailable("TURSO_DATABASE_URL must be a libsql:// or https:// URL")
    if url.endswith("/v2/pipeline"):
        return url
    return url + "/v2/pipeline"


def _arg(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _value(cell: dict):
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")


class TursoStore:
    """Every call is one pipeline round trip. Statements in a pipeline run in
    order on one connection, which is what makes the conditional UPDATE and the
    SELECT that reads back the winner a single consistent step."""

    def __init__(self, database_url: str, auth_token: str, client: httpx.Client | None = None):
        self._url = pipeline_url(database_url)
        self._token = auth_token
        self._client = client
        self._schema_ready = False

    @classmethod
    def from_env(cls) -> TursoStore:
        url = (os.environ.get("TURSO_DATABASE_URL") or "").strip()
        token = (os.environ.get("TURSO_AUTH_TOKEN") or "").strip()
        if not url or not token:
            # Fail closed and loudly. The alternative — falling back to process
            # memory — would make every serverless invocation its own universe:
            # a decision recorded by one would be invisible to the poll served by
            # the next, and the "already decided" check would never see a prior
            # decision, so the single-use rule would silently stop holding.
            raise StoreUnavailable("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        return cls(url, token)

    def _post(self, statements: list[dict]) -> list[dict]:
        requests = [{"type": "execute", "stmt": s} for s in statements]
        requests.append({"type": "close"})
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            if self._client is not None:
                resp = self._client.post(self._url, json={"requests": requests}, headers=headers)
            else:
                resp = httpx.post(
                    self._url, json={"requests": requests}, headers=headers, timeout=10.0
                )
        except httpx.HTTPError as exc:
            # type(exc).__name__ only: an httpx repr can include the request, and
            # the request carries the Authorization header.
            raise StoreUnavailable(f"turso request failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise StoreUnavailable(f"turso http {resp.status_code}")
        results = resp.json().get("results", [])
        # Errors first: a failed statement stops the pipeline, so the response is
        # BOTH short and carrying the reason. Checking the length first would
        # replace "no such table" with a message about counting.
        for item in results:
            if item.get("type") != "ok":
                message = (item.get("error") or {}).get("message", "unknown")
                raise StoreUnavailable(f"turso statement failed: {message}")
        if len(results) < len(statements):
            # Without this the close-result gets read as a query result, which
            # looks like an empty table rather than a failure.
            raise StoreUnavailable("turso returned fewer results than statements")
        return [
            item.get("response", {}).get("result", {}) for item in results[: len(statements)]
        ]

    @staticmethod
    def _rows(result: dict) -> list[dict]:
        names = [c.get("name") for c in result.get("cols", [])]
        return [dict(zip(names, (_value(c) for c in row), strict=False))
                for row in result.get("rows", [])]

    def _ensure(self) -> list[dict]:
        # Cheap enough to send every cold start; a serverless function has no
        # deploy hook to run migrations from.
        if self._schema_ready:
            return []
        return [{"sql": DDL, "args": []}]

    def put(self, rec: Record) -> Record:
        """Insert if absent, then return whatever the row actually says.

        INSERT OR IGNORE, not REPLACE: a retried push of an id that has already
        been decided must not reset it back to pending. A crashed agent re-pushing
        on restart would otherwise get a second bite at an approval a human
        already denied.
        """
        statements = self._ensure() + [
            {
                "sql": (
                    "INSERT OR IGNORE INTO approvals "
                    "(approval_id, tool, payload_hash, rendered_artifact, args_json, "
                    "created_at, decision) VALUES (?, ?, ?, ?, ?, ?, 'pending')"
                ),
                "args": [
                    _arg(rec.approval_id), _arg(rec.tool), _arg(rec.payload_hash),
                    _arg(rec.rendered_artifact), _arg(rec.args_json), _arg(rec.created_at),
                ],
            },
            {
                "sql": f"SELECT {COLUMNS} FROM approvals WHERE approval_id = ?",
                "args": [_arg(rec.approval_id)],
            },
        ]
        results = self._post(statements)
        self._schema_ready = True
        rows = self._rows(results[-1])
        if not rows:
            raise StoreUnavailable("insert did not produce a row")
        return Record(**rows[0])

    def get(self, approval_id: str) -> Record | None:
        statements = self._ensure() + [
            {
                "sql": f"SELECT {COLUMNS} FROM approvals WHERE approval_id = ?",
                "args": [_arg(approval_id)],
            }
        ]
        results = self._post(statements)
        self._schema_ready = True
        rows = self._rows(results[-1])
        return Record(**rows[0]) if rows else None

    def decide(
        self, approval_id: str, decision: str, decided_by: str, decided_at: str
    ) -> tuple[Record | None, bool]:
        """Conditional write. Returns (record, this_call_decided_it)."""
        statements = self._ensure() + [
            {
                "sql": (
                    "UPDATE approvals SET decision = ?, decided_by = ?, decided_at = ? "
                    "WHERE approval_id = ? AND decision = 'pending'"
                ),
                "args": [
                    _arg(decision), _arg(decided_by), _arg(decided_at), _arg(approval_id)
                ],
            },
            {
                "sql": f"SELECT {COLUMNS} FROM approvals WHERE approval_id = ?",
                "args": [_arg(approval_id)],
            },
        ]
        results = self._post(statements)
        self._schema_ready = True
        changed = int(results[-2].get("affected_row_count") or 0) == 1
        rows = self._rows(results[-1])
        return (Record(**rows[0]) if rows else None, changed)


class MemoryStore:
    """Same interface, backed by a dict. For tests only.

    Never reachable in production: `routes` asks `TursoStore.from_env()`, which
    raises rather than degrading to this. See the comment there for why a memory
    fallback on serverless quietly breaks the single-use rule.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Record] = {}
        self._lock = threading.Lock()

    def put(self, rec: Record) -> Record:
        with self._lock:
            existing = self._rows.get(rec.approval_id)
            if existing is not None:
                return existing
            self._rows[rec.approval_id] = rec
            return rec

    def get(self, approval_id: str) -> Record | None:
        with self._lock:
            return self._rows.get(approval_id)

    def decide(
        self, approval_id: str, decision: str, decided_by: str, decided_at: str
    ) -> tuple[Record | None, bool]:
        with self._lock:
            rec = self._rows.get(approval_id)
            if rec is None:
                return (None, False)
            if rec.decision != "pending":
                return (rec, False)
            updated = Record(
                approval_id=rec.approval_id, tool=rec.tool, payload_hash=rec.payload_hash,
                rendered_artifact=rec.rendered_artifact, args_json=rec.args_json,
                created_at=rec.created_at, decision=decision, decided_by=decided_by,
                decided_at=decided_at,
            )
            self._rows[approval_id] = updated
            return (updated, True)
