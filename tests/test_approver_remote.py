"""The remote approver must fail CLOSED.

Every test here asserts on the Decision, not merely on "did not raise". An
approver that returns APPROVED when it cannot reach a human is the single
worst failure this codebase has, so each way the network can betray us gets
its own named test.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

import ctrl_a_jr
from ctrl_a_jr import activity
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.approver_remote import RemoteApprover
from ctrl_a_jr.types import Decision

API = "https://approvals.example.test"


def test_the_imported_package_is_this_checkout():
    """An editable install points `ctrl_a_jr` at ONE checkout, and a pytest run
    inside a worktree then scores the MAIN checkout's source. This has already
    fooled two agents. Fail loudly rather than report green over code that never
    ran."""
    pkg = Path(ctrl_a_jr.__file__).resolve()
    repo = Path(__file__).resolve().parent.parent
    assert repo in pkg.parents, f"{pkg} is not inside {repo}"


class FakeHTTP:
    """Scripted transport. Each entry is (status, body) or an exception to raise."""

    def __init__(self, push=(201, {"ok": True}), polls=None):
        self.push = push
        self.polls = list(polls or [])
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": json, "timeout": timeout})
        outcome = self.push if method == "POST" else (
            self.polls.pop(0) if self.polls else (200, {"status": "pending"}))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _record(tool="gmail_send", args=None):
    args = args or {"to": "a@b.c", "subject": "Invoice", "body": "You owe $799."}
    return ApprovalStore().request(tool, args, "Email to a@b.c")


def _approver(http, **kw):
    kw.setdefault("poll_interval_s", 0.0)
    kw.setdefault("max_poll_interval_s", 0.0)
    return RemoteApprover(API, "push-secret", http=http, **kw)


def _events(name):
    return [r for r in activity.read_log() if r["event"] == name]


# ── the four ways the network fails, each ending in DENIED ───────────────────

def test_a_connection_error_denies():
    http = FakeHTTP(push=httpx.ConnectError("no route to host"))
    assert _approver(http).decide(_record()) is Decision.DENIED
    assert _events("approval_auto_denied")[0]["reason"] == "push_failed"


def test_a_timeout_waiting_for_a_human_denies():
    """Bounded by construction: the wait ends, and it ends in a refusal."""
    clock = iter([0.0, 0.0, 1000.0])
    http = FakeHTTP(polls=[(200, {"status": "pending"})])
    approver = _approver(http, timeout_s=5.0, monotonic=lambda: next(clock))
    assert approver.decide(_record()) is Decision.DENIED
    denied = _events("approval_auto_denied")[0]
    assert denied["reason"] == "approval_timeout"


def test_an_http_500_from_the_api_denies():
    http = FakeHTTP(polls=[(500, {"error": "boom"})])
    assert _approver(http).decide(_record()) is Decision.DENIED
    assert _events("approval_auto_denied")[0]["reason"] == "poll_failed"


def test_an_unrecognised_status_string_denies():
    """'yes', 'ok', 'ACCEPTED' are not approvals. Only the exact contract is."""
    for weird in ("yes", "ok", "expired", "", "approved-ish"):
        http = FakeHTTP(polls=[(200, {"status": weird})])
        assert _approver(http).decide(_record()) is Decision.DENIED, weird


def test_a_malformed_body_denies():
    http = FakeHTTP(polls=[(200, None), (200, ["approved"])])
    assert _approver(http).decide(_record()) is Decision.DENIED


def test_a_push_rejected_with_401_denies_before_any_polling():
    http = FakeHTTP(push=(401, {"error": "bad token"}))
    assert _approver(http).decide(_record()) is Decision.DENIED
    assert [c["method"] for c in http.calls] == ["POST"]


# ── the one way it may approve ───────────────────────────────────────────────

def test_an_approved_decision_returns_approved_and_records_who_decided():
    http = FakeHTTP(polls=[(200, {"status": "pending"}),
                           (200, {"status": "approved", "decided_by": "brandon@example.com"})])
    rec = _record()
    assert _approver(http).decide(rec) is Decision.APPROVED
    decided = _events("approval_remote_decided")[0]
    assert decided["decision"] == "approved"
    assert decided["decided_by"] == "brandon@example.com"
    assert decided["approver"] == "remote"
    assert decided["approval_id"] == rec.id


def test_an_approval_echoing_a_different_payload_hash_is_refused():
    """An approval granted for another payload is not an approval for this one."""
    http = FakeHTTP(polls=[(200, {"status": "approved", "payload_hash": "deadbeef"})])
    assert _approver(http).decide(_record()) is Decision.DENIED
    assert _events("approval_auto_denied")[0]["reason"] == "payload_hash_mismatch"


def test_a_human_denial_is_recorded_as_a_decision_not_an_auto_denial():
    """A person saying no and the surface being unable to ask are different
    evidence; the harness scores them differently."""
    http = FakeHTTP(polls=[(200, {"status": "denied", "decided_by": "brandon"})])
    assert _approver(http).decide(_record()) is Decision.DENIED
    assert _events("approval_remote_decided")[0]["decision"] == "denied"
    assert _events("approval_auto_denied") == []


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_stop_while_blocked_denies_promptly_and_logs_approval_auto_denied():
    http = FakeHTTP(polls=[])  # polls forever: always pending
    approver = _approver(http, poll_interval_s=0.05, timeout_s=30.0)
    out = {}
    t = threading.Thread(target=lambda: out.update(d=approver.decide(_record())))
    t.start()
    # Let it get into the poll loop, then pull the surface out from under it.
    for _ in range(200):
        if http.calls:
            break
    approver.stop()
    t.join(timeout=5.0)
    assert not t.is_alive(), "stop() must unblock decide(), not leave it waiting"
    assert out["d"] is Decision.DENIED
    assert _events("approval_auto_denied")[0]["reason"] == "approver_stopped"


def test_decide_after_stop_never_reaches_the_network():
    http = FakeHTTP()
    approver = _approver(http)
    approver.stop()
    assert approver.decide(_record()) is Decision.DENIED
    assert http.calls == []


# ── what goes on the wire ────────────────────────────────────────────────────

def test_the_push_carries_the_rendered_artifact_and_the_payload_hash():
    http = FakeHTTP(polls=[(200, {"status": "denied"})])
    rec = _record(args={"to": "ada@example.com", "subject": "Overdue",
                        "body": "Invoice in_1 for $799 is 30 days overdue."})
    _approver(http).decide(rec)
    body = http.calls[0]["json"]
    assert body["approval_id"] == rec.id
    assert body["payload_hash"] == rec.payload_hash
    assert body["tool"] == "gmail_send"
    assert body["created_at"]
    # The ARTIFACT, derived from the args that will execute — not a JSON blob.
    assert "ada@example.com" in body["artifact"]
    assert "30 days overdue" in body["artifact"]


def test_the_bearer_token_is_a_header_and_never_appears_in_the_body():
    http = FakeHTTP(polls=[(200, {"status": "denied"})])
    _approver(http).decide(_record())
    push = http.calls[0]
    assert push["headers"]["Authorization"] == "Bearer push-secret"
    assert "push-secret" not in str(push["json"])


def test_the_poll_targets_the_approval_by_id():
    http = FakeHTTP(polls=[(200, {"status": "approved"})])
    rec = _record()
    _approver(http).decide(rec)
    assert http.calls[1]["url"] == f"{API}/api/approvals/{rec.id}"


def test_missing_configuration_is_refused_at_construction():
    for base, token in (("", "t"), ("   ", "t"), (API, ""), (None, "t"), (API, None)):
        with pytest.raises(ValueError):
            RemoteApprover(base, token, http=FakeHTTP())


# ── the Slack card is a side channel, not the authorization ──────────────────

def test_a_failing_slack_card_does_not_change_the_decision():
    class Boom:
        def post_approval_request(self, *a, **kw):
            raise RuntimeError("slack down")

    http = FakeHTTP(polls=[(200, {"status": "approved"})])
    approver = _approver(http, slack=Boom())
    assert approver.decide(_record()) is Decision.APPROVED
    assert _events("approval_card_failed")


def test_the_card_is_posted_with_the_approval_id():
    class Card:
        def __init__(self):
            self.seen = []

        def post_approval_request(self, approval_id, tool, rendered):
            self.seen.append((approval_id, tool, rendered))

    card = Card()
    http = FakeHTTP(polls=[(200, {"status": "denied"})])
    rec = _record()
    _approver(http, slack=card).decide(rec)
    assert card.seen[0][0] == rec.id
    assert card.seen[0][1] == "gmail_send"
