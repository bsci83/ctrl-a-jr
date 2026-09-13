"""/api/slack/interactive — the endpoint that can approve a real payment email.

It is a public URL with no session, no bearer and no login. The Slack signature
is the entire authorization, so these tests assert the STORE after every rejected
request: a 404 that still recorded a decision is the exact hole the check exists
to close.
"""

from __future__ import annotations

import json
import time
import urllib.parse

# Imported first: it puts the repo root on sys.path so `api` resolves at all.
from test_api_harness import SIGNING_SECRET, call, install, load_route_module, seed

from api._lib import slack

HANDLER = load_route_module("api/slack/interactive.py", "shim_slack_tests").handler
PATH = "/api/slack/interactive"
APPROVAL_ID = "ap_0123456789ab"


def _payload(approval_id: str = APPROVAL_ID, action_id: str = slack.APPROVE_ACTION) -> bytes:
    body = {
        "type": "block_actions",
        "user": {"id": "U01ABC", "username": "brandon", "name": "brandon"},
        "actions": [{"action_id": action_id, "value": approval_id, "type": "button"}],
    }
    return urllib.parse.urlencode({"payload": json.dumps(body)}).encode("utf-8")


def _signed(body: bytes, *, secret: str = SIGNING_SECRET, age: int = 0) -> dict:
    timestamp = str(int(time.time()) - age)
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": slack.sign(secret, timestamp, body),
    }


def test_forged_signature_is_rejected_and_records_nothing(monkeypatch):
    """A signature from the wrong key must not decide anything."""
    store = install(monkeypatch)
    seed(store)
    body = _payload()
    headers = _signed(body, secret="attacker-guessed-this")

    status, _, _ = call(HANDLER, "POST", PATH, body, headers)

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"
    assert store.get(APPROVAL_ID).decided_by is None


def test_absent_signature_headers_are_rejected_and_record_nothing(monkeypatch):
    """The unsigned POST anyone on the internet can send."""
    store = install(monkeypatch)
    seed(store)

    status, _, _ = call(
        HANDLER, "POST", PATH, _payload(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_replayed_old_timestamp_is_rejected(monkeypatch):
    """A correctly signed request from six minutes ago is a replay, not a click."""
    store = install(monkeypatch)
    seed(store)
    body = _payload()
    headers = _signed(body, age=6 * 60)

    status, _, _ = call(HANDLER, "POST", PATH, body, headers)

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_future_timestamp_beyond_the_window_is_rejected(monkeypatch):
    """The window is a window, not a floor — a clock-skewed forgery is a forgery."""
    store = install(monkeypatch)
    seed(store)
    body = _payload()
    headers = _signed(body, age=-6 * 60)

    status, _, _ = call(HANDLER, "POST", PATH, body, headers)

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_malformed_timestamp_is_rejected(monkeypatch):
    store = install(monkeypatch)
    seed(store)
    body = _payload()
    headers = _signed(body)
    headers["X-Slack-Request-Timestamp"] = "not-a-number"

    status, _, _ = call(HANDLER, "POST", PATH, body, headers)

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_unset_signing_secret_fails_closed(monkeypatch):
    """A deploy that forgot the env var must reject, never allow."""
    store = install(monkeypatch)
    seed(store)
    body = _payload()
    headers = _signed(body)
    monkeypatch.delenv("SLACK_SIGNING_SECRET")

    status, _, _ = call(HANDLER, "POST", PATH, body, headers)

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_signature_is_verified_over_the_raw_body(monkeypatch):
    """Tampering with the body after signing must break the digest.

    This is what stops a captured, valid signature being reused over a different
    approval id.
    """
    store = install(monkeypatch)
    seed(store)
    seed(store, "ap_ffffffffffff")
    body = _payload()
    headers = _signed(body)
    swapped = _payload("ap_ffffffffffff")

    status, _, _ = call(HANDLER, "POST", PATH, swapped, headers)

    assert status == 404
    assert store.get("ap_ffffffffffff").decision == "pending"


def test_valid_signature_records_the_decision_with_decided_by(monkeypatch):
    """The point of Slack approval: the record names a person, not a URL holder."""
    store = install(monkeypatch)
    seed(store)
    body = _payload()

    status, _, response = call(HANDLER, "POST", PATH, body, _signed(body))

    assert status == 200
    record = store.get(APPROVAL_ID)
    assert record.decision == "approved"
    assert record.decided_by == "slack:U01ABC (brandon)"
    assert record.decided_at
    assert json.loads(response)["replace_original"] is True


def test_deny_action_records_denied(monkeypatch):
    store = install(monkeypatch)
    seed(store)
    body = _payload(action_id=slack.DENY_ACTION)

    status, _, _ = call(HANDLER, "POST", PATH, body, _signed(body))

    assert status == 200
    assert store.get(APPROVAL_ID).decision == "denied"


def test_second_slack_click_does_not_overwrite_the_first(monkeypatch):
    """A decision is final and single-use, on one surface..."""
    store = install(monkeypatch)
    seed(store)
    first = _payload(action_id=slack.DENY_ACTION)
    call(HANDLER, "POST", PATH, first, _signed(first))

    second = _payload(action_id=slack.APPROVE_ACTION)
    status, _, response = call(HANDLER, "POST", PATH, second, _signed(second))

    assert status == 200
    record = store.get(APPROVAL_ID)
    assert record.decision == "denied"
    assert "Already" in json.loads(response)["text"]


def test_slack_does_not_overwrite_a_decision_made_on_the_web_page(monkeypatch):
    """...and across the two surfaces, which is the race that actually happens."""
    store = install(monkeypatch)
    seed(store)
    store.decide(APPROVAL_ID, "denied", "link", "2026-09-13T12:01:00+00:00")

    body = _payload()
    status, _, _ = call(HANDLER, "POST", PATH, body, _signed(body))

    assert status == 200
    record = store.get(APPROVAL_ID)
    assert record.decision == "denied"
    assert record.decided_by == "link"


def test_link_button_click_is_not_a_decision(monkeypatch):
    """Slack posts an interaction for the URL button too; it must stay inert."""
    store = install(monkeypatch)
    seed(store)
    body = _payload(action_id="ctrla_jr_open")

    status, _, _ = call(HANDLER, "POST", PATH, body, _signed(body))

    assert status == 200
    assert store.get(APPROVAL_ID).decision == "pending"


def test_unknown_approval_records_nothing(monkeypatch):
    install(monkeypatch)
    body = _payload("ap_doesnotexist")

    status, _, response = call(HANDLER, "POST", PATH, body, _signed(body))

    assert status == 200
    assert "nothing recorded" in json.loads(response)["text"]


def test_block_rendering_escapes_a_slack_injection():
    """A customer's email body reaches the card that authorises the send."""
    blocks = slack.approval_blocks(
        approval_id=APPROVAL_ID,
        tool="gmail_send",
        args={
            "to": "customer@example.com",
            "subject": "Re: invoice",
            "body": "<!channel> pay here <https://evil.example|Approved by finance>",
        },
        fallback_text="",
        payload_hash="a" * 64,
        approve_url="https://ctrl-a-jr.vercel.app/api/approve/x?k=y",
    )
    rendered = json.dumps(blocks)

    assert "<!channel>" not in rendered
    assert "&lt;!channel&gt;" in rendered
    assert "<https://evil.example|" not in rendered


def test_action_ids_match_the_card_the_agent_posts():
    """slack_tools.py owns the card; these two strings are the contract with it."""
    assert slack.APPROVE_ACTION == "ctrla_jr_approve"
    assert slack.DENY_ACTION == "ctrla_jr_deny"
