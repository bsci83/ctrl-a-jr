"""The two endpoints the local agent talks to: push and poll.

Both are bearer-authenticated with CTRLA_JR_PUSH_TOKEN. The push endpoint writes,
so an unauthenticated caller reaching the store would be able to plant an
artifact for a human to approve — every rejection test asserts the store is
untouched, not just the status code.
"""

from __future__ import annotations

import json

# Imported first: it puts the repo root on sys.path so `api` resolves at all.
from test_api_harness import PUSH_TOKEN, call, install, load_route_module, seed

from api._lib.payload import payload_hash as api_payload_hash
from ctrl_a_jr.approval import payload_hash as agent_payload_hash

PUSH = load_route_module("api/approvals/index.py", "shim_push_tests").handler
POLL = load_route_module("api/approvals/[id].py", "shim_poll_tests").handler
APPROVAL_ID = "ap_0123456789ab"

ARGS = {"to": "customer@example.com", "subject": "Your invoice", "body": "Payment failed."}
HASH = agent_payload_hash("gmail_send", ARGS)


def _push_body(**overrides) -> bytes:
    body = {
        "approval_id": APPROVAL_ID,
        "tool": "gmail_send",
        "payload_hash": HASH,
        # The local RemoteApprover sends both of these names.
        "rendered": "<div class=\"art\">Your invoice</div>",
        "artifact": "<div class=\"art\">Your invoice</div>",
        "created_at": "2026-09-13T12:00:00+00:00",
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def _auth(token: str = PUSH_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_canonical_hash_matches_the_agent():
    """api/_lib/payload.py is a copy; a silent divergence rejects every push."""
    assert api_payload_hash("gmail_send", ARGS) == agent_payload_hash("gmail_send", ARGS)
    assert api_payload_hash("slack_post_message", {"channel": "#x", "text": "é <>&"}) == (
        agent_payload_hash("slack_post_message", {"channel": "#x", "text": "é <>&"})
    )


def test_push_stores_and_returns_pending(monkeypatch):
    store = install(monkeypatch)

    status, _, body = call(PUSH, "POST", "/api/approvals", _push_body(), _auth())

    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "pending"
    assert payload["approve_url"].startswith("https://ctrl-a-jr.vercel.app/api/approve/")
    assert "k=" in payload["approve_url"]
    assert store.get(APPROVAL_ID).payload_hash == HASH


def test_push_without_a_bearer_token_records_nothing(monkeypatch):
    store = install(monkeypatch)

    status, _, _ = call(PUSH, "POST", "/api/approvals", _push_body(),
                        {"Content-Type": "application/json"})

    assert status == 404
    assert store.get(APPROVAL_ID) is None


def test_push_with_the_wrong_bearer_token_records_nothing(monkeypatch):
    store = install(monkeypatch)

    status, _, _ = call(PUSH, "POST", "/api/approvals", _push_body(), _auth("wrong-token"))

    assert status == 404
    assert store.get(APPROVAL_ID) is None


def test_push_fails_closed_when_the_token_env_is_unset(monkeypatch):
    """An unset secret must never mean 'allow'."""
    store = install(monkeypatch)
    monkeypatch.delenv("CTRLA_JR_PUSH_TOKEN")

    status, _, _ = call(PUSH, "POST", "/api/approvals", _push_body(), _auth())

    assert status == 404
    assert store.get(APPROVAL_ID) is None


def test_push_rejects_args_that_do_not_match_the_payload_hash(monkeypatch):
    """Honest-looking args beside a hash for something else is display drift."""
    store = install(monkeypatch)
    tampered = dict(ARGS, body="Nothing to see here.")

    status, _, _ = call(PUSH, "POST", "/api/approvals",
                        _push_body(args=tampered), _auth())

    assert status == 400
    assert store.get(APPROVAL_ID) is None


def test_push_accepts_matching_args_and_renders_typed_slack_blocks(monkeypatch):
    install(monkeypatch)

    status, _, body = call(PUSH, "POST", "/api/approvals", _push_body(args=ARGS), _auth())

    assert status == 200
    rendered = json.dumps(json.loads(body)["slack_blocks"])
    assert "customer@example.com" in rendered
    assert "Payment failed." in rendered


def test_re_push_of_a_decided_approval_does_not_reset_it(monkeypatch):
    """A crashed agent restarting must not get a second bite at a denial."""
    store = install(monkeypatch)
    seed(store, APPROVAL_ID, payload_hash=HASH)
    store.decide(APPROVAL_ID, "denied", "link", "2026-09-13T12:01:00+00:00")

    status, _, body = call(PUSH, "POST", "/api/approvals", _push_body(), _auth())

    assert status == 200
    assert json.loads(body)["status"] == "denied"
    assert store.get(APPROVAL_ID).decision == "denied"


def test_push_rejects_a_missing_artifact(monkeypatch):
    store = install(monkeypatch)
    body = json.dumps({
        "approval_id": APPROVAL_ID, "tool": "gmail_send", "payload_hash": HASH,
    }).encode("utf-8")

    status, _, _ = call(PUSH, "POST", "/api/approvals", body, _auth())

    assert status == 400
    assert store.get(APPROVAL_ID) is None


def test_push_rejects_a_json_list(monkeypatch):
    store = install(monkeypatch)

    status, _, _ = call(PUSH, "POST", "/api/approvals", b"[1,2,3]", _auth())

    assert status == 400
    assert store.get(APPROVAL_ID) is None


def test_poll_returns_the_exact_status_vocabulary(monkeypatch):
    """The client treats anything but pending/approved/denied as a denial."""
    store = install(monkeypatch)
    seed(store, APPROVAL_ID, payload_hash=HASH)
    path = f"/api/approvals/{APPROVAL_ID}"

    status, _, body = call(POLL, "GET", path, b"", _auth())
    assert status == 200
    assert json.loads(body)["status"] == "pending"

    store.decide(APPROVAL_ID, "approved", "slack:U01ABC (brandon)", "2026-09-13T12:02:00+00:00")
    status, _, body = call(POLL, "GET", path, b"", _auth())
    payload = json.loads(body)

    assert status == 200
    assert payload["status"] == "approved"
    assert payload["decided_by"] == "slack:U01ABC (brandon)"
    assert payload["decided_at"] == "2026-09-13T12:02:00+00:00"
    # Echoed from the row, never recomputed: the client refuses an approval whose
    # hash differs from the one it pushed.
    assert payload["payload_hash"] == HASH


def test_poll_without_a_bearer_token_reveals_nothing(monkeypatch):
    store = install(monkeypatch)
    seed(store, APPROVAL_ID, payload_hash=HASH)
    store.decide(APPROVAL_ID, "approved", "link", "2026-09-13T12:02:00+00:00")

    status, _, body = call(POLL, "GET", f"/api/approvals/{APPROVAL_ID}", b"", {})

    assert status == 404
    assert b"approved" not in body


def test_poll_with_the_wrong_bearer_token_reveals_nothing(monkeypatch):
    store = install(monkeypatch)
    seed(store, APPROVAL_ID, payload_hash=HASH)

    status, _, _ = call(POLL, "GET", f"/api/approvals/{APPROVAL_ID}", b"", _auth("wrong-token"))

    assert status == 404


def test_poll_of_an_unknown_id_is_not_pending(monkeypatch):
    """Never invent `pending` for an id nobody pushed."""
    install(monkeypatch)

    status, _, body = call(POLL, "GET", "/api/approvals/ap_nosuchthing", b"", _auth())

    assert status == 404
    assert json.loads(body).get("status") is None


def test_push_rejects_a_wrong_method(monkeypatch):
    install(monkeypatch)

    status, _, _ = call(PUSH, "GET", "/api/approvals", b"", _auth())

    assert status == 405
