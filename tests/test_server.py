import http.client
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ctrl_a_jr import server
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.types import ApprovalRecord, Decision


def _rec(rendered="To: a@b.c\nSubject: Overdue\n\nPlease pay $42.00."):
    return ApprovalRecord(id="ap_1", tool="gmail_send", payload_hash="deadbeef",
                          rendered=rendered, decision=Decision.PENDING)


def test_page_shows_the_rendered_artifact_not_json():
    html = server.render_page([_rec()])
    assert "Please pay $42.00." in html
    assert "Subject: Overdue" in html
    assert "ap_1" in html


def test_page_escapes_html_in_the_rendered_body():
    html = server.render_page([_rec("<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_page_has_approve_and_deny_controls():
    html = server.render_page([_rec()])
    assert "approve" in html.lower()
    assert "deny" in html.lower()
    assert "ap_1" in html


def test_empty_state_renders():
    html = server.render_page([])
    assert "nothing waiting" in html.lower()


# R19: Integration tests for WebApprover HTTP server

def _live_approver():
    store = ApprovalStore()
    approver = server.WebApprover(store, port=0)
    approver.start()
    return store, approver


def test_get_serves_the_pending_approval_over_http():
    store, approver = _live_approver()
    try:
        store.request("gmail_send", {"to": "a@b.c"}, rendered="To: a@b.c\n\nPlease pay $42.00.")
        with urllib.request.urlopen(approver.url, timeout=5) as r:
            body = r.read().decode()
        assert r.status == 200
        assert "Please pay $42.00." in body
    finally:
        approver.stop()


def test_posting_approve_unblocks_decide_with_approved():
    store, approver = _live_approver()
    try:
        record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
        result = {}

        def wait():
            result["decision"] = approver.decide(record)

        t = threading.Thread(target=wait, daemon=True)
        t.start()

        data = urllib.parse.urlencode({"id": record.id, "decision": "approved"}).encode()
        urllib.request.urlopen(approver.url + "resolve", data=data, timeout=5)

        t.join(timeout=5)
        assert result["decision"] is Decision.APPROVED
    finally:
        approver.stop()


def test_posting_deny_unblocks_decide_with_denied():
    store, approver = _live_approver()
    try:
        record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
        result = {}

        def wait():
            result["decision"] = approver.decide(record)

        t = threading.Thread(target=wait, daemon=True)
        t.start()

        data = urllib.parse.urlencode({"id": record.id, "decision": "denied"}).encode()
        urllib.request.urlopen(approver.url + "resolve", data=data, timeout=5)

        t.join(timeout=5)
        assert result["decision"] is Decision.DENIED
    finally:
        approver.stop()


def test_an_unknown_decision_value_fails_closed_to_denied():
    """Anything that is not exactly 'approved' must not authorise a send."""
    store, approver = _live_approver()
    try:
        record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
        result = {}

        def wait():
            result["decision"] = approver.decide(record)

        t = threading.Thread(target=wait, daemon=True)
        t.start()

        data = urllib.parse.urlencode({"id": record.id, "decision": "maybe"}).encode()
        urllib.request.urlopen(approver.url + "resolve", data=data, timeout=5)

        t.join(timeout=5)
        assert result["decision"] is Decision.DENIED
    finally:
        approver.stop()


def test_decide_fails_closed_when_the_server_is_stopped():
    """If the approval surface goes away, nobody can say yes — so the answer is no."""
    store, approver = _live_approver()
    record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    result = {}

    def wait():
        result["decision"] = approver.decide(record)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    time.sleep(0.1)          # let it enter the wait loop
    approver.stop()
    t.join(timeout=5)
    assert t.is_alive() is False, "decide() did not return after stop()"
    assert result["decision"] is Decision.DENIED


def test_a_foreign_host_header_is_rejected_on_get():
    """DNS rebinding: a hostile page speaking to this port under another hostname
    must not be able to read pending approvals (ids + customer email bodies)."""
    store, approver = _live_approver()
    try:
        store.request("gmail_send", {"to": "a@b.c"}, rendered="Please pay $42.00.")
        conn = http.client.HTTPConnection(approver.host, approver.port, timeout=5)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", "evil.example.com")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 403
            body = resp.read()
            assert b"42.00" not in body
        finally:
            conn.close()
    finally:
        approver.stop()


def test_a_foreign_host_header_is_rejected_on_post_and_does_not_resolve():
    store, approver = _live_approver()
    try:
        record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
        data = urllib.parse.urlencode({"id": record.id, "decision": "approved"}).encode()
        conn = http.client.HTTPConnection(approver.host, approver.port, timeout=5)
        try:
            conn.putrequest("POST", "/resolve", skip_host=True)
            conn.putheader("Host", "evil.example.com")
            conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            resp = conn.getresponse()
            assert resp.status == 403
            resp.read()
        finally:
            conn.close()
        assert record.id not in approver._decisions
        assert store.get(record.id).decision.value == "pending"
    finally:
        approver.stop()


def test_post_to_an_unexpected_path_does_not_resolve_anything():
    store, approver = _live_approver()
    try:
        record = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
        data = urllib.parse.urlencode({"id": record.id, "decision": "approved"}).encode()
        try:
            urllib.request.urlopen(approver.url + "anything", data=data, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        assert record.id not in approver._decisions
    finally:
        approver.stop()


# ── the activity strip ────────────────────────────────────────────────────────
# Shape borrowed from ctrl-a's activity-strip.tsx: compact rows, an icon per
# kind, a distinct style for the state that matters, and nothing at all when
# there is nothing.

def _ev(event, **kw):
    return {"event": event, "run_id": "r1", **kw}


def test_the_strip_renders_nothing_when_there_is_nothing():
    """An empty strip beats a strip announcing its own emptiness."""
    assert server.render_strip([]) == ""


def test_the_strip_shows_tool_calls_as_they_happen():
    html_ = server.render_strip([
        _ev("model_turn", model="MiniMax-M3", round=1),
        _ev("tool_call", tool="stripe_get_invoice", ok=True),
    ])
    assert "stripe_get_invoice" in html_
    assert "MiniMax-M3" in html_


def test_a_failed_tool_call_is_visually_distinct():
    html_ = server.render_strip([_ev("tool_call", tool="gmail_send", ok=False,
                                     error="smtp auth failed")])
    assert "err" in html_
    assert "smtp auth failed" in html_


def test_the_pending_gate_is_visually_distinct():
    """The pause IS the product. It gets its own style, not an absence."""
    html_ = server.render_strip([_ev("approval_requested", tool="gmail_send")])
    assert "gate" in html_
    assert "waiting for you" in html_


def test_the_strip_escapes_attacker_influenced_content():
    html_ = server.render_strip([_ev("tool_call", tool="<script>alert(1)</script>", ok=True)])
    assert "<script>alert(1)</script>" not in html_
    assert "&lt;script&gt;" in html_


def test_the_strip_is_capped_and_shows_the_newest():
    events = [_ev("tool_call", tool=f"t{i}", ok=True) for i in range(40)]
    html_ = server.render_strip(events)
    assert "t39" in html_
    assert "t0</span>" not in html_


def test_the_strip_names_the_run():
    assert "r1" in server.render_strip([_ev("tool_call", tool="x", ok=True)])


def test_the_page_shows_activity_even_with_no_pending_approval():
    """Between approvals the page used to be blank. That is most of a run."""
    page = server.render_page([], [_ev("tool_call", tool="stripe_get_invoice", ok=True)])
    assert "stripe_get_invoice" in page
    assert "Nothing waiting for you" not in page
