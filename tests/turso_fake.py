"""A Turso pipeline endpoint backed by real SQLite. No network.

Deliberately not a stub that returns canned rows: `claim_pending` is a
compare-and-set whose whole value is `affected_row_count`, and a hand-written
fake would have to decide that number itself — which is the thing under test.
Running the actual SQL against stdlib sqlite3 means the at-most-once claim is
exercised, not asserted.

Not named `test_*` so pytest imports it rather than collecting it.
"""

from __future__ import annotations

import sqlite3


class TransportDown(RuntimeError):
    """Stands in for the HTTP layer being unreachable."""


class FakeTurso:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.down = False
        self.requests = 0

    # -- raw access for tests that need to corrupt the stored row ----------

    def sql(self, statement: str, args: tuple = ()) -> list[tuple]:
        cur = self.conn.execute(statement, args)
        self.conn.commit()
        return cur.fetchall()

    # -- the transport itself ---------------------------------------------

    def __call__(self, payload: dict) -> dict:
        self.requests += 1
        if self.down:
            raise TransportDown("turso unreachable")
        results = []
        for request in payload["requests"]:
            if request["type"] != "execute":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = request["stmt"]
            args = tuple(_from_wire(a) for a in stmt.get("args", []))
            try:
                cur = self.conn.execute(stmt["sql"], args)
            except sqlite3.Error as exc:
                results.append({"type": "error", "error": {"message": str(exc)}})
                continue
            self.conn.commit()
            cols = [{"name": d[0]} for d in (cur.description or [])]
            rows = [[_to_wire(v) for v in row] for row in cur.fetchall()]
            results.append({
                "type": "ok",
                "response": {
                    "type": "execute",
                    "result": {
                        "cols": cols,
                        "rows": rows,
                        "affected_row_count": max(cur.rowcount, 0),
                    },
                },
            })
        return {"results": results}


def _from_wire(arg: dict):
    kind = arg.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(arg["value"])
    if kind == "float":
        return float(arg["value"])
    return arg["value"]


def _to_wire(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}
