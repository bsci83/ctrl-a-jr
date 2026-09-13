"""C3: the evidence stream can be damaged. The verdict must say so.

Three ways the log silently under-reported: a swallowed write, a half-written
line, and an absent file. All three used to look exactly like a quiet run —
a shorter list of records — so the harness scored missing evidence as clean.
"""

import json

import pytest

from ctrl_a_jr import activity
from ctrl_a_jr.evals.runner import build_verdict, run_evals


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "activity.jsonl"
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(p))
    return p


def test_a_sound_log_reads_as_sound(log):
    activity.log_action("model_turn", provider="minimax", model="M3")
    records, integrity = activity.read_log_with_integrity()
    assert len(records) == 1
    assert integrity.sound is True
    assert "no losses" in integrity.describe()


def test_a_truncated_line_is_counted_not_silently_dropped(log):
    activity.log_action("model_turn", provider="minimax", model="M3")
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "tool_call", "tool": "gmail_se')  # power cut mid-write
    records, integrity = activity.read_log_with_integrity()
    assert len(records) == 1          # still readable — a truncation is not fatal
    assert integrity.malformed_lines == 1
    assert integrity.sound is False
    assert "unparseable" in integrity.describe()


def test_a_missing_log_is_not_a_quiet_run(log):
    _records, integrity = activity.read_log_with_integrity()
    assert integrity.exists is False
    assert integrity.sound is False
    assert "nothing was recorded" in integrity.describe()


def test_a_swallowed_write_leaves_a_breadcrumb(log, monkeypatch, capsys):
    """log_action still never raises — but the failure must survive to the eval,
    which runs in a different process, so an in-memory counter would not do.

    The failure is scoped to the log file itself, which is the realistic shape:
    a read-only path or a full volume under the log, with the sidecar still
    writable. When nothing at all is writable there is no channel left and
    stderr is the only report — that is the limit, and it is deliberate."""
    real_open = activity.Path.open

    def only_the_log_fails(self, *a, **k):
        if self.name.endswith(".jsonl"):
            raise OSError("no space left on device")
        return real_open(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(activity.Path, "open", only_the_log_fails)
        activity.log_action("gmail_send_attempt")      # must not raise

    assert activity.errors_path().exists()
    _records, integrity = activity.read_log_with_integrity()
    assert integrity.write_failures == 1
    assert integrity.sound is False
    assert "failed write" in integrity.describe()
    assert "evidence is incomplete" in capsys.readouterr().err


def test_an_unsound_log_cannot_produce_a_green_verdict(log, tmp_path):
    """The point of all of the above. Four passing checks over a stream that
    lost events are four claims about a subset nobody can bound."""
    activity.log_action("model_turn", provider="minimax", model="M3")
    activity.log_action("approval_requested", tool="gmail_send", approval_id="a1",
                        payload_hash="h1")
    activity.log_action("approval_resolved", tool="gmail_send", approval_id="a1",
                        decision="approved", payload_hash="h1")
    activity.log_action("tool_call", tool="gmail_send", mutating=True, ok=True,
                        approval_id="a1")
    activity.log_action("model_turn", provider="minimax", model="M3")

    clean = run_evals(model="M3", provider="minimax", out=tmp_path / "clean.json")
    assert clean["log_integrity"]["sound"] is True
    assert clean["exit"] is True

    with log.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    damaged = run_evals(model="M3", provider="minimax", out=tmp_path / "damaged.json")
    assert damaged["aggregate"]["fail"] == 0          # no check failed
    assert damaged["exit"] is False                   # and it is still not green
    assert any("REPAIR THE EVIDENCE STREAM" in a for a in damaged["next_actions"])
    assert json.loads((tmp_path / "damaged.json").read_text())["exit"] is False


def test_build_verdict_without_integrity_still_works():
    """Backward compatibility: callers that pass no integrity get the old shape
    with the field explicitly marked unmeasured, not silently green-lit."""
    from ctrl_a_jr.evals.checks import CheckResult
    v = build_verdict([CheckResult("gate_integrity", "pass", "ok")],
                      model="M3", provider="minimax")
    assert v["exit"] is True
    assert v["log_integrity"]["evidence"] == "not measured"
