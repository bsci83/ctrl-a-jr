"""The Turso HTTP client.

The rest of the suite runs against MemoryStore, so without these the production
storage path would be entirely unexercised — the classic untested layer beneath a
green suite. These drive the real TursoStore with a fake httpx transport and
assert the bytes it puts on the wire.
"""

from __future__ import annotations

import json

import httpx
import pytest

# Imported first: it puts the repo root on sys.path so `api` resolves at all.
from test_api_harness import ROOT  # noqa: F401

from api._lib.store import DDL, Record, StoreUnavailable, TursoStore, pipeline_url

TOKEN = "turso-token-should-never-be-echoed"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(results: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": [
        {"type": "ok", "response": {"type": "execute", "result": r}} for r in results
    ] + [{"type": "ok", "response": {"type": "close"}}]})


def _row(**overrides) -> dict:
    values = {
        "approval_id": "ap_0123456789ab", "tool": "gmail_send", "payload_hash": "a" * 64,
        "rendered_artifact": "<div>x</div>", "args_json": None,
        "created_at": "2026-09-13T12:00:00+00:00", "decision": "pending",
        "decided_by": None, "decided_at": None,
    }
    values.update(overrides)
    cols = [{"name": k} for k in values]
    cells = [{"type": "null"} if v is None else {"type": "text", "value": v}
             for v in values.values()]
    return {"cols": cols, "rows": [cells], "affected_row_count": 0}


def test_pipeline_url_accepts_both_schemes():
    assert pipeline_url("libsql://db.turso.io") == "https://db.turso.io/v2/pipeline"
    assert pipeline_url("https://db.turso.io/") == "https://db.turso.io/v2/pipeline"
    assert pipeline_url("https://db.turso.io/v2/pipeline") == "https://db.turso.io/v2/pipeline"


def test_pipeline_url_rejects_a_non_url():
    with pytest.raises(StoreUnavailable):
        pipeline_url("db.turso.io")


def test_put_sends_the_ddl_and_an_insert_or_ignore():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return _ok([{}, {"cols": [], "rows": [], "affected_row_count": 1}, _row()])

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    record = store.put(Record(
        approval_id="ap_0123456789ab", tool="gmail_send", payload_hash="a" * 64,
        rendered_artifact="<div>x</div>", created_at="2026-09-13T12:00:00+00:00",
    ))

    statements = [r["stmt"]["sql"] for r in seen["body"]["requests"] if r["type"] == "execute"]
    assert statements[0] == DDL
    assert "INSERT OR IGNORE" in statements[1]
    assert seen["body"]["requests"][-1]["type"] == "close"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert record.decision == "pending"


def test_decide_is_conditional_on_still_being_pending():
    """The single-use rule lives in the WHERE clause, not in Python."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        update = dict(_row(), affected_row_count=0)
        return _ok([{}, update, _row(decision="denied", decided_by="link")])

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    record, changed = store.decide("ap_0123456789ab", "approved", "slack:U1 (x)", "2026-09-13")

    sql = [r["stmt"]["sql"] for r in seen["body"]["requests"] if r["type"] == "execute"][1]
    assert "WHERE approval_id = ? AND decision = 'pending'" in sql
    assert changed is False
    assert record.decision == "denied"
    assert record.decided_by == "link"


def test_decide_reports_the_write_it_won():
    def handler(request: httpx.Request) -> httpx.Response:
        update = dict(_row(), affected_row_count=1)
        return _ok([{}, update, _row(decision="approved", decided_by="slack:U1 (x)")])

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    record, changed = store.decide("ap_0123456789ab", "approved", "slack:U1 (x)", "2026-09-13")

    assert changed is True
    assert record.decided_by == "slack:U1 (x)"


def test_transport_failure_never_carries_the_token():
    """An httpx repr includes the request, and the request carries the header."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    with pytest.raises(StoreUnavailable) as caught:
        store.get("ap_0123456789ab")

    assert TOKEN not in str(caught.value)


def test_http_error_never_carries_the_token():
    store = TursoStore(
        "libsql://db.turso.io", TOKEN,
        client=_client(lambda request: httpx.Response(401, json={"error": TOKEN})),
    )
    with pytest.raises(StoreUnavailable) as caught:
        store.get("ap_0123456789ab")

    assert TOKEN not in str(caught.value)


def test_statement_error_surfaces_the_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"type": "error", "error": {"message": "no such table: approvals"}},
        ]})

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    with pytest.raises(StoreUnavailable, match="no such table"):
        store.get("ap_0123456789ab")


def test_get_returns_none_for_an_unknown_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{}, {"cols": [], "rows": []}])

    store = TursoStore("libsql://db.turso.io", TOKEN, client=_client(handler))
    assert store.get("ap_nosuchthing") is None


def test_from_env_fails_closed_without_configuration(monkeypatch):
    """A memory fallback on serverless would silently break the single-use rule."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)

    with pytest.raises(StoreUnavailable):
        TursoStore.from_env()
