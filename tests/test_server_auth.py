"""Authentication on the approval surface.

The gate's whole claim is that a mutating action needs a human. Bound to
localhost that claim held without a secret. Put behind a public tunnel so a
judge can drive it, an unauthenticated `POST /resolve` hands anyone with the URL
the authority to send a real email and a real Stripe invoice.

The test that matters here is not the status code. It is that an unauthenticated
POST leaves the approval STATE untouched — a 404 that still recorded the
decision would be the hole, wearing the response of a fix.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from ctrl_a_jr import server
from ctrl_a_jr.activity import read_log
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.types import Decision


@pytest.fixture
def live():
    store = ApprovalStore()
    approver = server.WebApprover(store, port=0, token="tok_correct_horse")
    approver.start()
    try:
        yield store, approver
    finally:
        approver.stop()


def _get(approver, query="", headers=None):
    req = urllib.request.Request(approver.url + query, headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


def _post_resolve(approver, record_id, query="", headers=None):
    data = urllib.parse.urlencode({"id": record_id, "decision": "approved"}).encode()
    req = urllib.request.Request(approver.url + "resolve" + query, data=data,
                                 headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


def _auth_failures():
    return [e for e in read_log() if e.get("event") == "approval_auth_failed"]


# ── reading the surface ───────────────────────────────────────────────────────

def test_a_correct_token_on_get_serves_the_page(live):
    """One pasted link has to work: the token rides in on the query string."""
    store, approver = live
    store.request("gmail_send", {"to": "a@b.c", "subject": "Overdue",
                                 "body": "Please pay $42.00."},
                  rendered="Please pay $42.00.")
    with _get(approver, f"?{server.TOKEN_PARAM}={approver.token}") as r:
        body = r.read().decode()
    assert r.status == 200
    assert "Please pay $42.00." in body


def test_a_bearer_header_is_accepted(live):
    """Scripted callers get a channel that never lands in a URL or a log line."""
    store, approver = live
    with _get(approver, headers={"Authorization": f"Bearer {approver.token}"}) as r:
        assert r.status == 200


def test_the_cookie_set_by_the_first_get_carries_the_later_post(live):
    """The form POST has no query string of its own. If the cookie did not carry
    the token, clicking Approve on a correctly-opened page would 404."""
    store, approver = live
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    with _get(approver, f"?{server.TOKEN_PARAM}={approver.token}") as r:
        cookie = r.headers.get("Set-Cookie")
    assert cookie and server.TOKEN_COOKIE in cookie
    jar = cookie.split(";")[0]
    _post_resolve(approver, record.id, headers={"Cookie": jar}).read()
    assert approver._decisions[record.id] is Decision.APPROVED


def test_a_wrong_token_gets_404_and_the_body_leaks_nothing(live):
    """404, not 401/403: a public URL that says 'unauthorized' has confirmed an
    approval surface lives here and invited a guessing campaign."""
    store, approver = live
    store.request("gmail_send", {"to": "a@b.c"}, rendered="Please pay $42.00.")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(approver, f"?{server.TOKEN_PARAM}=wrong_guess")
    assert exc.value.code == 404
    body = exc.value.read().decode()
    assert approver.token not in body
    assert "token" not in body.lower()
    assert "42.00" not in body


def test_a_missing_token_gets_404(live):
    store, approver = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(approver)
    assert exc.value.code == 404


# ── the mutating route ────────────────────────────────────────────────────────

def test_an_unauthenticated_post_does_not_change_the_approval_state(live):
    """The one that matters. Assert on STATE, not on the status code: a 404 that
    still recorded the decision would have sent the email."""
    store, approver = live
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_resolve(approver, record.id)
    assert exc.value.code == 404
    assert approver._decisions == {}
    assert store.get(record.id).decision is Decision.PENDING


def test_a_wrong_token_post_does_not_change_the_approval_state(live):
    store, approver = live
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_resolve(approver, record.id, query=f"?{server.TOKEN_PARAM}=wrong_guess")
    assert exc.value.code == 404
    assert approver._decisions == {}
    assert store.get(record.id).decision is Decision.PENDING


def test_a_stolen_cookie_value_that_is_merely_a_prefix_is_rejected(live):
    """compare_digest, not `==`. A prefix match must be as wrong as any other
    wrong answer, and take the same time to say so."""
    store, approver = live
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    prefix = approver.token[:-1]
    with pytest.raises(urllib.error.HTTPError):
        _post_resolve(approver, record.id,
                      headers={"Cookie": f"{server.TOKEN_COOKIE}={prefix}"})
    assert approver._decisions == {}


def test_a_correct_token_post_still_records_the_decision(live):
    """The positive control: the tests above must be failing on AUTH, not because
    the route stopped working."""
    store, approver = live
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    _post_resolve(approver, record.id, query=f"?{server.TOKEN_PARAM}={approver.token}").read()
    assert approver._decisions[record.id] is Decision.APPROVED


# ── evidence ──────────────────────────────────────────────────────────────────

def test_auth_failure_is_logged_with_the_path_and_whether_a_token_was_offered(live):
    store, approver = live
    with pytest.raises(urllib.error.HTTPError):
        _post_resolve(approver, "ap_x")
    with pytest.raises(urllib.error.HTTPError):
        _get(approver, f"?{server.TOKEN_PARAM}=wrong_guess")

    events = _auth_failures()
    assert len(events) == 2
    post, get = events
    assert (post["method"], post["path"], post["token_supplied"]) == ("POST", "/resolve", False)
    assert (get["method"], get["path"], get["token_supplied"]) == ("GET", "/", True)


def test_the_auth_failure_record_carries_no_secret(live):
    """The activity log is the artifact we hand to reviewers. A near-miss written
    into it puts attacker-supplied guesses — and one copy-paste later, the real
    secret — into evidence."""
    store, approver = live
    with pytest.raises(urllib.error.HTTPError):
        _get(approver, f"?{server.TOKEN_PARAM}=wrong_guess")
    raw = json.dumps(_auth_failures())
    assert approver.token not in raw
    assert "wrong_guess" not in raw


def test_a_successful_request_logs_no_auth_failure(live):
    store, approver = live
    with _get(approver, f"?{server.TOKEN_PARAM}={approver.token}") as r:
        r.read()
    assert _auth_failures() == []


# ── where the token comes from ────────────────────────────────────────────────

def test_the_token_comes_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv(server.TOKEN_ENV, "env_token_value")
    approver = server.WebApprover(ApprovalStore(), port=0)
    assert approver.token == "env_token_value"
    assert approver.token_is_generated is False


def test_an_unset_environment_generates_a_token_rather_than_disabling_auth(monkeypatch):
    """Auth you have to opt into is auth that is absent the one time it matters."""
    monkeypatch.delenv(server.TOKEN_ENV, raising=False)
    approver = server.WebApprover(ApprovalStore(), port=0)
    assert len(approver.token) >= 32
    assert approver.token_is_generated is True


def test_the_authed_url_carries_the_token(monkeypatch):
    approver = server.WebApprover(ApprovalStore(), port=0, token="tok_abc")
    assert approver.authed_url.endswith(f"?{server.TOKEN_PARAM}=tok_abc")


def test_a_non_ascii_token_guess_is_rejected_rather_than_crashing(live):
    """compare_digest raises TypeError on non-ASCII str; a 500 here would be a
    remote crash on the authorization boundary."""
    store, approver = live
    guess = urllib.parse.quote("pässword")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(approver, f"?{server.TOKEN_PARAM}={guess}")
    assert exc.value.code == 404


def test_a_malformed_cookie_header_is_rejected_rather_than_crashing(live):
    store, approver = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(approver, headers={"Cookie": "=====;;;"})
    assert exc.value.code == 404
