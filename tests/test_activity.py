import json
import os

import pytest

from ctrl_a_jr import activity


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "activity.jsonl"
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(p))
    return p


def test_appends_one_json_object_per_line(log):
    activity.log_action("tool_call", tool="stripe_get_customer")
    activity.log_action("tool_call", tool="gmail_send")
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "stripe_get_customer"


def test_caller_keys_do_not_clobber_attribution(log):
    # An audit log a caller can forge is not evidence. Positive assertions: `!=`
    # against the forged values passes for literally any real value, so assert
    # the actual expected attribution instead.
    activity.log_action("tool_call", tool="x", agent="somebody-else", pid=1, ts="1999")
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "ctrl-a-jr"
    assert rec["pid"] == os.getpid()
    assert rec["ts"] != "1999"
    # and it must actually be a recent ISO timestamp, not just "not the forged value"
    from datetime import UTC, datetime
    parsed = datetime.fromisoformat(rec["ts"])
    assert (datetime.now(UTC) - parsed).total_seconds() < 60


def test_read_log_returns_dicts(log):
    activity.log_action("tool_refused", tool="gmail_send", reason="denied")
    out = activity.read_log(log)
    assert out[0]["event"] == "tool_refused"
    assert out[0]["reason"] == "denied"


def test_read_log_missing_file_is_empty(tmp_path):
    assert activity.read_log(tmp_path / "nope.jsonl") == []


def test_never_raises_on_unserialisable_payload(log):
    activity.log_action("tool_call", tool="x", blob=object())
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["tool"] == "x"
