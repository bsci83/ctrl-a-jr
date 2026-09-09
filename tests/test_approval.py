import pytest

from ctrl_a_jr import approval
from ctrl_a_jr.types import Decision


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(tmp_path / "a.jsonl"))


def test_canonical_json_is_key_order_independent():
    a = approval.canonical_json({"b": 1, "a": 2})
    b = approval.canonical_json({"a": 2, "b": 1})
    assert a == b


def test_hash_is_stable_and_differs_on_change():
    h1 = approval.payload_hash("gmail_send", {"to": "x@y.com", "body": "hi"})
    h2 = approval.payload_hash("gmail_send", {"body": "hi", "to": "x@y.com"})
    h3 = approval.payload_hash("gmail_send", {"to": "x@y.com", "body": "hi!"})
    assert h1 == h2
    assert h1 != h3


def test_hash_includes_tool_name():
    args = {"a": 1}
    assert approval.payload_hash("t1", args) != approval.payload_hash("t2", args)


def test_request_starts_pending():
    store = approval.ApprovalStore()
    rec = store.request("gmail_send", {"to": "a@b.c"}, rendered="To: a@b.c")
    assert rec.decision is Decision.PENDING
    assert store.pending()[0].id == rec.id


def test_resolve_records_decision():
    store = approval.ApprovalStore()
    rec = store.request("gmail_send", {"to": "a@b.c"}, rendered="x")
    out = store.resolve(rec.id, Decision.APPROVED)
    assert out.decision is Decision.APPROVED
    assert store.pending() == []


def test_verify_true_for_identical_args():
    store = approval.ApprovalStore()
    args = {"to": "a@b.c", "body": "pay please"}
    rec = store.request("gmail_send", args, rendered="x")
    store.resolve(rec.id, Decision.APPROVED)
    assert store.verify(rec.id, args) is True


def test_verify_false_when_args_changed_after_approval():
    # The attack this whole design exists to stop: render one email, send another.
    store = approval.ApprovalStore()
    rec = store.request("gmail_send", {"to": "a@b.c", "body": "pay please"}, rendered="x")
    store.resolve(rec.id, Decision.APPROVED)
    assert store.verify(rec.id, {"to": "attacker@evil.com", "body": "pay please"}) is False


def test_resolve_unknown_id_raises():
    with pytest.raises(KeyError):
        approval.ApprovalStore().resolve("nope", Decision.APPROVED)


def test_cannot_resolve_twice():
    store = approval.ApprovalStore()
    rec = store.request("gmail_send", {"a": 1}, rendered="x")
    store.resolve(rec.id, Decision.APPROVED)
    with pytest.raises(ValueError):
        store.resolve(rec.id, Decision.DENIED)


def test_verify_false_for_a_pending_record():
    """An unresolved approval must never authorise execution."""
    store = approval.ApprovalStore()
    args = {"to": "a@b.c", "body": "pay please"}
    rec = store.request("gmail_send", args, rendered="x")
    assert store.verify(rec.id, args) is False


def test_verify_false_for_a_denied_record():
    """A denial must not be bypassable by re-deriving a matching hash."""
    store = approval.ApprovalStore()
    args = {"to": "a@b.c", "body": "pay please"}
    rec = store.request("gmail_send", args, rendered="x")
    store.resolve(rec.id, Decision.DENIED)
    assert store.verify(rec.id, args) is False
