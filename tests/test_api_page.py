"""The human-facing surface: /api/approve/<id> and /api/decide.

Neither has a login. The HMAC nonce in the link is what makes the URL a
capability rather than an address, and it is also the CSRF defence on /api/decide
— so the rejection tests assert the store, not just the status code.
"""

from __future__ import annotations

import urllib.parse

# Imported first: it puts the repo root on sys.path so `api` resolves at all.
from test_api_harness import call, install, load_route_module, seed

from api._lib.http import approve_nonce

APPROVE = load_route_module("api/approve/[id].py", "shim_approve_tests").handler
DECIDE = load_route_module("api/decide.py", "shim_decide_tests").handler
APPROVAL_ID = "ap_0123456789ab"

# What a customer can put in an email thread, quoted into the artifact the local
# agent pushes. The deployed page must not depend on the pusher having escaped it.
HOSTILE_BODY = (
    "<script>fetch('/api/decide',{method:'POST'})</script>"
    "<img src=x onerror=alert(1)>"
    "<!channel> <https://evil.example|Approved by finance>"
    "</div></main><h1>Approved</h1>"
)


def _link(approval_id: str = APPROVAL_ID) -> str:
    return f"/api/approve/{approval_id}?k={approve_nonce(approval_id)}"


def _form(**fields) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def test_page_renders_the_artifact_with_both_buttons(monkeypatch):
    store = install(monkeypatch)
    seed(store)

    status, headers, body = call(APPROVE, "GET", _link())
    page = body.decode("utf-8")

    assert status == 200
    assert "Invoice 123 is unpaid." in page
    assert 'value="approved"' in page
    assert 'value="denied"' in page
    assert headers["X-Frame-Options"] == "DENY"
    assert "form-action 'self'" in headers["Content-Security-Policy"]


def test_page_escapes_a_hostile_artifact(monkeypatch):
    """<script>, an event handler and a broken-out tag must all be inert."""
    store = install(monkeypatch)
    seed(store, rendered_artifact=f'<div class="art-body">{HOSTILE_BODY}</div>')

    status, _, body = call(APPROVE, "GET", _link())
    page = body.decode("utf-8")

    assert status == 200
    assert "<script" not in page
    assert "fetch('/api/decide'" not in page
    # Not deleted — neutralised. The approver should see everything the message
    # they are authorising contains, and none of it should execute.
    assert "<img" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    # The Slack-style injection survives as text the approver can read, not markup.
    assert "&lt;!channel&gt;" in page
    assert "&lt;https://evil.example|Approved by finance&gt;" in page
    # The stray closing tags did not escape the card.
    assert "</main><h1>Approved</h1>" not in page
    assert page.count("<h1>") == 1


def test_page_keeps_the_artifact_structure_it_is_meant_to_show(monkeypatch):
    """Escaping must not flatten the artifact into an unreadable blob (spec §5 P2)."""
    store = install(monkeypatch)
    seed(store, rendered_artifact=(
        '<div class="art"><div class="art-h"><b>Email</b> will be sent as you</div>'
        '<div class="art-b"><div class="art-row"><span>To</span>'
        '<span>customer@example.com</span></div></div></div>'
    ))

    _, _, body = call(APPROVE, "GET", _link())
    page = body.decode("utf-8")

    assert '<div class="art-row">' in page
    assert "<b>Email</b>" in page
    assert "customer@example.com" in page


def test_page_without_the_nonce_is_a_404(monkeypatch):
    """The approval id alone must not open the page."""
    store = install(monkeypatch)
    seed(store)

    status, _, body = call(APPROVE, "GET", f"/api/approve/{APPROVAL_ID}")

    assert status == 404
    assert b"Invoice 123" not in body


def test_page_with_a_wrong_nonce_is_a_404(monkeypatch):
    store = install(monkeypatch)
    seed(store)

    status, _, _ = call(APPROVE, "GET", f"/api/approve/{APPROVAL_ID}?k={'0' * 32}")

    assert status == 404


def test_nonce_from_one_approval_does_not_open_another(monkeypatch):
    store = install(monkeypatch)
    seed(store)
    seed(store, "ap_ffffffffffff")

    status, _, _ = call(
        APPROVE, "GET", f"/api/approve/ap_ffffffffffff?k={approve_nonce(APPROVAL_ID)}"
    )

    assert status == 404


def test_decide_records_an_approval(monkeypatch):
    store = install(monkeypatch)
    seed(store)

    status, _, body = call(
        DECIDE, "POST", "/api/decide",
        _form(id=APPROVAL_ID, decision="approved", k=approve_nonce(APPROVAL_ID)),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert status == 200
    record = store.get(APPROVAL_ID)
    assert record.decision == "approved"
    # The link proves possession of a URL, never a person. Recorded as such.
    assert record.decided_by == "link"
    assert record.decided_at
    assert b"already <b>approved</b>" in body


def test_decide_without_the_nonce_records_nothing(monkeypatch):
    """A blind cross-site POST for a guessed id must not approve a send."""
    store = install(monkeypatch)
    seed(store)

    status, _, _ = call(
        DECIDE, "POST", "/api/decide", _form(id=APPROVAL_ID, decision="approved"),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_decide_with_a_wrong_nonce_records_nothing(monkeypatch):
    store = install(monkeypatch)
    seed(store)

    status, _, _ = call(
        DECIDE, "POST", "/api/decide",
        _form(id=APPROVAL_ID, decision="approved", k="0" * 32),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert status == 404
    assert store.get(APPROVAL_ID).decision == "pending"


def test_decide_rejects_an_invented_decision(monkeypatch):
    """There is no fourth state (spec §5)."""
    store = install(monkeypatch)
    seed(store)

    status, _, _ = call(
        DECIDE, "POST", "/api/decide",
        _form(id=APPROVAL_ID, decision="approved-ish", k=approve_nonce(APPROVAL_ID)),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert status == 400
    assert store.get(APPROVAL_ID).decision == "pending"


def test_second_web_decision_does_not_overwrite_the_first(monkeypatch):
    """A decision is final and single-use, whichever surface clicks second."""
    store = install(monkeypatch)
    seed(store)
    nonce = approve_nonce(APPROVAL_ID)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    call(DECIDE, "POST", "/api/decide", _form(id=APPROVAL_ID, decision="denied", k=nonce), headers)

    status, _, body = call(
        DECIDE, "POST", "/api/decide",
        _form(id=APPROVAL_ID, decision="approved", k=nonce), headers,
    )

    assert status == 200
    assert store.get(APPROVAL_ID).decision == "denied"
    assert b"already <b>denied</b>" in body


def test_decided_page_shows_who_decided_and_offers_no_buttons(monkeypatch):
    store = install(monkeypatch)
    seed(store)
    store.decide(APPROVAL_ID, "approved", "slack:U01ABC (brandon)", "2026-09-13T12:02:00+00:00")

    status, _, body = call(APPROVE, "GET", _link())
    page = body.decode("utf-8")

    assert status == 200
    assert "slack:U01ABC (brandon)" in page
    assert 'value="approved"' not in page
    assert 'value="denied"' not in page


def test_page_for_an_unknown_id_is_a_404(monkeypatch):
    install(monkeypatch)

    status, _, _ = call(APPROVE, "GET", _link("ap_nosuchthing"))

    assert status == 404
