"""Durable run state, and the one property everything else rests on.

The pending tool's arguments ARE the payload that executes. `approval.verify`
re-hashes them at execution time and compares to what the human approved, so a
storage round-trip that reorders keys or reformats a number aborts a legitimate
approved call as if it were a tamper — indistinguishable, in the log, from a
real attack.
"""

from __future__ import annotations

import json

import pytest

from ctrl_a_jr.approval import canonical_json, payload_hash
from ctrl_a_jr.runstate import (
    STATUS_AWAITING,
    PendingCall,
    RunState,
    RunStore,
    StorageError,
    round_trip_args,
    turso_pipeline_url,
)
from tests.turso_fake import FakeTurso

AWKWARD_ARGS = {
    "to": "renée@example.com",           # non-ascii, ensure_ascii=False path
    "amount": 42.10,                      # float formatting
    "zero": 0.0,
    "count": 3,
    "cc": [],                             # empty list
    "tags": ["b", "a"],                   # order-significant list
    "meta": {"z": 1, "a": {"n": None, "t": True}},   # nested, key order differs from sorted
    "body": "line one\nline two\ttabbed",  # control characters
    "empty_obj": {},
}


@pytest.fixture
def store():
    return RunStore(FakeTurso())


def test_payload_hash_survives_the_storage_round_trip():
    """P3: what comes back out of storage must hash to what went in.

    Covers a float, a nested dict, a unicode string and an empty list, because
    each has its own way of not surviving a naive JSON round-trip.
    """
    before = payload_hash("gmail_send", AWKWARD_ARGS)
    after = payload_hash("gmail_send", round_trip_args(AWKWARD_ARGS))
    assert after == before
    # And the stored form is byte-identical to the material the hash is taken
    # over, not merely hash-equivalent by luck.
    assert canonical_json(round_trip_args(AWKWARD_ARGS)) == canonical_json(AWKWARD_ARGS)


def test_payload_hash_survives_a_full_database_round_trip(store):
    """The same property through the real SQL path, not just the serialiser."""
    state = store.create_run("sys", "go", max_rounds=8)
    pending = PendingCall(
        approval_id="pa_1", tool="gmail_send", args=AWKWARD_ARGS,
        payload_hash=payload_hash("gmail_send", AWKWARD_ARGS), rendered="artifact",
        uses=[{"id": "t1", "name": "gmail_send", "input": AWKWARD_ARGS}], results=[],
    )
    store.save(RunState(
        run_id=state.run_id, status=STATUS_AWAITING, system="sys",
        messages=state.messages, round_index=0, max_rounds=8, pending=pending,
    ))
    loaded = store.load(state.run_id)
    assert loaded.pending.args == AWKWARD_ARGS
    assert payload_hash("gmail_send", loaded.pending.args) == pending.payload_hash


def test_a_tampered_stored_payload_no_longer_matches_its_hash(store):
    """The comparison actually discriminates; it is not vacuously true."""
    tampered = dict(AWKWARD_ARGS, to="attacker@example.com")
    assert payload_hash("gmail_send", tampered) != payload_hash("gmail_send", AWKWARD_ARGS)


def test_create_load_round_trips_the_run(store):
    state = store.create_run("system prompt", "recover the payments", max_rounds=8)
    loaded = store.load(state.run_id)
    assert loaded.run_id == state.run_id
    assert loaded.system == "system prompt"
    assert loaded.messages == [{"role": "user", "content": "recover the payments"}]
    assert loaded.max_rounds == 8
    assert loaded.pending is None


def test_load_of_an_unknown_run_is_none_not_an_error(store):
    assert store.load("nope") is None


def test_claim_pending_succeeds_exactly_once(store):
    """The at-most-once guarantee, as a database compare-and-set."""
    state = store.create_run("sys", "go", max_rounds=8)
    pending = PendingCall("pa_1", "gmail_send", {"to": "a@b.c"},
                          payload_hash("gmail_send", {"to": "a@b.c"}), "artifact",
                          uses=[{"id": "t1", "name": "gmail_send", "input": {"to": "a@b.c"}}])
    store.save(RunState(state.run_id, STATUS_AWAITING, "sys", state.messages, 0, 8,
                        pending=pending))
    assert store.record_decision(state.run_id, "pa_1", "approved") is True
    assert store.claim_pending(state.run_id, "pa_1") is True
    assert store.claim_pending(state.run_id, "pa_1") is False


def test_claim_pending_refuses_an_undecided_approval(store):
    state = store.create_run("sys", "go", max_rounds=8)
    pending = PendingCall("pa_1", "gmail_send", {}, payload_hash("gmail_send", {}), "x",
                          uses=[{"id": "t1", "name": "gmail_send", "input": {}}])
    store.save(RunState(state.run_id, STATUS_AWAITING, "sys", state.messages, 0, 8,
                        pending=pending))
    assert store.claim_pending(state.run_id, "pa_1") is False


def test_a_decision_for_a_stale_ticket_is_refused(store):
    """A decision must name the approval the run is actually waiting on."""
    state = store.create_run("sys", "go", max_rounds=8)
    pending = PendingCall("pa_now", "gmail_send", {}, payload_hash("gmail_send", {}), "x",
                          uses=[{"id": "t1", "name": "gmail_send", "input": {}}])
    store.save(RunState(state.run_id, STATUS_AWAITING, "sys", state.messages, 0, 8,
                        pending=pending))
    assert store.record_decision(state.run_id, "pa_old", "approved") is False
    assert store.load(state.run_id).decision is None


def test_corrupt_stored_messages_raise_rather_than_returning_a_run(store):
    state = store.create_run("sys", "go", max_rounds=8)
    store._send.sql("UPDATE jr_runs SET messages = ? WHERE run_id = ?",
                    ("{not json", state.run_id))
    with pytest.raises(StorageError):
        store.load(state.run_id)


def test_messages_that_are_not_messages_raise(store):
    state = store.create_run("sys", "go", max_rounds=8)
    store._send.sql("UPDATE jr_runs SET messages = ? WHERE run_id = ?",
                    (json.dumps([{"role": "wat", "content": "x"}]), state.run_id))
    with pytest.raises(StorageError):
        store.load(state.run_id)


def test_corrupt_pending_args_raise(store):
    state = store.create_run("sys", "go", max_rounds=8)
    store._send.sql(
        "UPDATE jr_runs SET status = ?, pending_approval_id = ?, pending_args = ? "
        "WHERE run_id = ?",
        (STATUS_AWAITING, "pa_1", "[[[", state.run_id),
    )
    with pytest.raises(StorageError):
        store.load(state.run_id)


def test_a_transport_failure_becomes_a_storage_error(store):
    state = store.create_run("sys", "go", max_rounds=8)
    store._send.down = True
    with pytest.raises(StorageError):
        store.load(state.run_id)


def test_a_transport_failure_message_does_not_echo_anything_it_was_sent():
    """The request carries the auth token; its failure message must not."""
    class Exploding:
        def __call__(self, payload):
            raise RuntimeError("Bearer super-secret-token leaked")

    with pytest.raises(StorageError) as caught:
        RunStore(Exploding()).load("r1")
    assert "super-secret-token" not in str(caught.value)


def test_from_env_without_credentials_refuses_instead_of_guessing(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    with pytest.raises(StorageError):
        RunStore.from_env()


def test_from_env_error_never_prints_the_token(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "super-secret-token")
    with pytest.raises(StorageError) as caught:
        RunStore.from_env()
    assert "super-secret-token" not in str(caught.value)


@pytest.mark.parametrize("given", [
    "libsql://agents.turso.io",
    "https://agents.turso.io",
    "https://agents.turso.io/v2/pipeline",
    "libsql://agents.turso.io/",
])
def test_pipeline_url_normalisation(given):
    assert turso_pipeline_url(given) == "https://agents.turso.io/v2/pipeline"
