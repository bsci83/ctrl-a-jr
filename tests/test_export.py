"""The evidence bundle is the only artifact of this project that becomes public.

So the tests that matter here are not "does it serialise" — they are "can a
credential, a customer address or an unreviewed field reach the file", and
"can something that is not a pass arrive looking like one".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ctrl_a_jr
from ctrl_a_jr import export
from ctrl_a_jr.activity import LogIntegrity


def test_the_suite_is_testing_this_checkout():
    """`ctrl_a_jr` is installed editable against ONE checkout.

    A pytest run started inside a git worktree can therefore import the MAIN
    checkout's source and report a green suite over code this branch does not
    contain. `pythonpath = ["src"]` in pyproject.toml is the fix; this asserts
    the fix is actually in force, because the failure mode of an additive
    change is a pass over code that never executed.
    """
    imported = Path(ctrl_a_jr.__file__).resolve()
    here = Path(__file__).resolve().parents[1]
    assert here in imported.parents, (
        f"imported ctrl_a_jr from {imported}, which is outside {here}"
    )

SENTINELS = {
    "STRIPE_SECRET_KEY": "sk_test_SENTINEL0000000000",
    "GMAIL_APP_PASSWORD": "appp asss wwww dddd",
    "SLACK_BOT_TOKEN": "xoxb-SENTINEL-0000-0000",
    "ANTHROPIC_AUTH_TOKEN": "Bearer SENTINELbearertoken",
}


def _integrity(records: int = 0) -> LogIntegrity:
    return LogIntegrity(path="/home/someone/.ctrl-a/jr/activity.jsonl", exists=True,
                        records=records, malformed_lines=0, write_failures=0)


def _event(event: str, run_id: str = "run1", **fields) -> dict:
    return {"event": event, "run_id": run_id, "agent": "ctrl-a-jr", "pid": 4242,
            "ts": "2026-09-13T12:00:00+00:00", **fields}


def _approved_run(run_id: str = "run1") -> list[dict]:
    return [
        _event("model_turn", run_id, provider="minimax", model="MiniMax-M3", round=1),
        _event("tool_call", run_id, tool="stripe_list_failed_payments", ok=True,
               mutating=False, result_chars=812),
        _event("approval_requested", run_id, approval_id="ap_1", tool="gmail_send",
               payload_hash="a" * 64, rendered_chars=430),
        _event("approval_resolved", run_id, approval_id="ap_1", tool="gmail_send",
               decision="approved", payload_hash="a" * 64, note=None),
        _event("tool_call", run_id, tool="gmail_send", ok=True, mutating=True,
               approval_id="ap_1", result_chars=22),
    ]


def test_no_sentinel_secret_reaches_the_bundle(monkeypatch):
    """The most important test in this module.

    Every sentinel is planted BOTH in the environment (so a leak of the live
    credential would be caught) and inside the log records themselves, in the
    shapes a leak would really take: an exception repr carrying the URL and key,
    an Authorization header, a full message body, a customer address.
    """
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)

    records = _approved_run() + [
        _event("tool_call", tool="gmail_send", ok=False, mutating=True, approval_id="ap_1",
               error=f"SMTPAuthenticationError(535, 'auth failed for "
                     f"{SENTINELS['GMAIL_APP_PASSWORD']}')"),
        _event("tool_refused", tool="slack_post_message", reason="approval_failed",
               error=f"HTTPError('https://slack.com/api?token={SENTINELS['SLACK_BOT_TOKEN']}')"),
        _event("provider_transport_failure", provider="minimax",
               error=f"ConnectionError('{SENTINELS['ANTHROPIC_AUTH_TOKEN']}')"),
        # Fields no allowlist entry names. These are what a denylist misses.
        _event("approval_requested", approval_id="ap_2", tool="gmail_send",
               payload_hash="b" * 64, rendered_chars=12,
               to="customer@example.com", subject="Your invoice",
               body="Hi Dana, your invoice is overdue.",
               authorization=f"Bearer {SENTINELS['STRIPE_SECRET_KEY']}"),
    ]

    bundle = export.build_bundle(records, _integrity(len(records)), None, commit="deadbeef")
    serialised = json.dumps(bundle, ensure_ascii=False)

    for name, value in SENTINELS.items():
        assert value not in serialised, f"{name} leaked into the evidence bundle"
    assert "customer@example.com" not in serialised
    assert "Hi Dana" not in serialised
    assert "Your invoice" not in serialised

    # The derived fields the page reads (turn grouping, the per-run outcome
    # counts) are covered by the same sentinel planting: they are computed, so
    # they must not open a second door into the record. Asserted structurally
    # as well as by absence, because absence alone would pass if a future field
    # carried a value no sentinel happens to match.
    for run in bundle["runs"]:
        for event in run["events"]:
            assert isinstance(event["turn"], int)
            allowed = set(export.COMMON_FIELDS) | {
                "turn", "integration", "integration_label", "error_type",
                "artifact_html", "unknown_event",
            } | set(export.EVENT_FIELDS.get(event["event"], ()))
            assert set(event) <= allowed, f"unreviewed field on {event['event']}"
        assert _only_counts(run["outcome"])


def _only_counts(node) -> bool:
    """Every leaf of the outcome block is a count or an already-exported key."""
    if isinstance(node, bool) or isinstance(node, int):
        return True
    if isinstance(node, str):
        # The only strings are integration keys and their display labels, both
        # of which are already published in `integrations`.
        return True
    if isinstance(node, list):
        return all(_only_counts(v) for v in node)
    if isinstance(node, dict):
        return all(_only_counts(v) for v in node.values())
    return False


def test_turns_group_calls_under_the_model_turn_that_made_them():
    """The conversation view needs to know which calls belong to which turn.

    `round` only exists on `model_turn`, so the grouping is walked. Anything
    before the first model turn is turn 0 rather than turn 1 — attributing setup
    to a turn the model had not taken yet would misdescribe the run.
    """
    records = [
        _event("tool_call", tool="stripe_get_invoice", ok=True, mutating=False,
               result_chars=10),
        _event("model_turn", provider="minimax", model="M", round=1),
        _event("tool_call", tool="gmail_send", ok=True, mutating=True,
               approval_id="ap_1", result_chars=2),
        _event("model_turn", provider="minimax", model="M", round=2),
        _event("tool_call", tool="slack_post_message", ok=True, mutating=True,
               approval_id="ap_2", result_chars=2),
    ]
    bundle = export.build_bundle(records, _integrity(5), None, commit="c")
    assert [e["turn"] for e in bundle["runs"][0]["events"]] == [0, 1, 1, 2, 2]
    assert bundle["runs"][0]["outcome"]["turns"] == 2


def test_outcome_counts_separate_machine_denial_from_human_denial():
    """Same Decision, different evidence — the summary must not merge them."""
    records = [
        _event("approval_requested", approval_id="ap_h", tool="gmail_send",
               payload_hash="a" * 64, rendered_chars=10),
        _event("approval_resolved", approval_id="ap_h", tool="gmail_send",
               decision="denied"),
        _event("approval_requested", approval_id="ap_m", tool="slack_post_message",
               payload_hash="b" * 64, rendered_chars=10),
        _event("approval_auto_denied", approval_id="ap_m", tool="slack_post_message",
               reason="approver_stopped"),
        _event("approval_requested", approval_id="ap_p", tool="stripe_send_invoice",
               payload_hash="c" * 64, rendered_chars=10),
        _event("tool_call", tool="gmail_send", ok=False, mutating=True,
               approval_id="ap_h", error="SMTPException()"),
        _event("tool_refused", tool="slack_post_message", reason="approval_failed"),
    ]
    bundle = export.build_bundle(records, _integrity(7), None, commit="c")
    gate = bundle["runs"][0]["outcome"]["gate"]
    assert gate == {"requested": 3, "approved": 0, "denied_by_human": 1,
                    "denied_by_machine": 1, "undecided": 1}
    counts = bundle["runs"][0]["outcome"]
    assert counts["mutations_executed"] == 0
    assert counts["mutations_failed"] == 1
    assert counts["refusals"] == 1


def test_outcome_integrations_follow_the_data():
    bundle = export.build_bundle(_approved_run(), _integrity(5), None, commit="c")
    rows = {t["key"]: t for t in bundle["runs"][0]["outcome"]["integrations"]}
    assert set(rows) == {"stripe", "gmail"}
    assert rows["gmail"]["approvals"] == 1
    assert rows["stripe"]["approvals"] == 0


def test_unexpected_new_field_does_not_leak():
    """Redaction is allowlist-based: a field nobody has reviewed is simply absent.

    This is the regression that a denylist cannot have — a denylist does not know
    the name of a field added tomorrow.
    """
    records = [_event("tool_call", tool="gmail_send", ok=True, mutating=True,
                      approval_id="ap_1", result_chars=4,
                      newly_added_field="LEAKY-VALUE-9000")]
    bundle = export.build_bundle(records, _integrity(1), None, commit="c")
    assert "LEAKY-VALUE-9000" not in json.dumps(bundle)
    assert "newly_added_field" not in json.dumps(bundle)


def test_unknown_event_type_exports_only_common_fields():
    """A future event type fails closed rather than publishing itself."""
    records = [_event("customer_reply_captured", body="please stop emailing me",
                      email="dana@example.com")]
    bundle = export.build_bundle(records, _integrity(1), None, commit="c")
    exported = bundle["runs"][0]["events"][0]
    assert exported["unknown_event"] is True
    # `turn` is the one addition, and it is derived from the event's POSITION in
    # the run, not from anything in the record — so nothing the unknown event
    # carried can ride out on it. Everything else is still dropped.
    assert set(exported) <= {"event", "run_id", "ts", "agent", "unknown_event", "turn"}
    assert isinstance(exported["turn"], int)
    assert "please stop" not in json.dumps(bundle)


def test_error_reprs_are_reduced_to_a_class_name():
    records = [_event("tool_call", tool="gmail_send", ok=False, mutating=True,
                      approval_id="ap_1",
                      error="SMTPAuthenticationError(535, 'secretpassword123')")]
    bundle = export.build_bundle(records, _integrity(1), None, commit="c")
    exported = bundle["runs"][0]["events"][0]
    assert exported["error_type"] == "SMTPAuthenticationError"
    assert "secretpassword123" not in json.dumps(bundle)


def test_inconclusive_check_does_not_export_as_a_pass():
    verdict = {
        "checks": [
            {"id": "gate_integrity", "verdict": "inconclusive",
             "evidence": "no mutating call occurred", "severity": "high"},
            # A verdict word this exporter does not know must not arrive at the
            # page as something the page might colour green.
            {"id": "payload_integrity", "verdict": "green", "evidence": "?", "severity": "high"},
        ],
        "per_run": [{"run_id": "run1", "checks": [
            {"id": "gate_integrity", "verdict": "inconclusive", "evidence": "x",
             "severity": "high"}]}],
        "runs": 1, "model": "MiniMax-M3", "log_integrity": {"sound": True},
    }
    bundle = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    verdicts = [c["verdict"] for c in bundle["verdict"]["checks"]]
    assert verdicts == ["inconclusive", "inconclusive"]
    assert bundle["runs"][0]["checks"][0]["verdict"] == "inconclusive"
    assert bundle["verdict"]["per_run"][0]["checks"][0]["verdict"] == "inconclusive"
    assert bundle["headline"]["status"] == "inconclusive"
    assert "0 unapproved" not in bundle["headline"]["claim"]


@pytest.mark.parametrize("word", ["inconclusive", "green", "PASS", "pass ", "ok",
                                  "passed", "", None, True, 1])
def test_only_the_exact_word_pass_survives_as_a_pass(word):
    """Everything that is not exactly "pass" or "fail" leaves as inconclusive.

    Case, whitespace, a truthy non-string and a plausible synonym are each a way
    a check could have arrived on the page coloured green without ever having
    passed. None of them do. The page applies the same rule independently, so an
    unknown verdict word has to get past both to be rendered as a pass.
    """
    verdict = {
        "checks": [{"id": "gate_integrity", "verdict": word, "evidence": "x",
                    "severity": "critical"},
                   {"id": "payload_integrity", "verdict": "pass", "evidence": "y",
                    "severity": "critical"}],
        "per_run": [{"run_id": "run1", "checks": [
            {"id": "gate_integrity", "verdict": word, "evidence": "x"}]}],
        "runs": 1, "model": "m", "log_integrity": {"sound": True},
    }
    bundle = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    gate = bundle["verdict"]["checks"][0]
    assert gate["verdict"] == "inconclusive"
    assert bundle["verdict"]["per_run"][0]["checks"][0]["verdict"] == "inconclusive"
    assert bundle["runs"][0]["checks"][0]["verdict"] == "inconclusive"
    # And a headline cannot be claimed off a check that never passed.
    assert bundle["headline"]["status"] == "inconclusive"
    assert "0 unapproved" not in bundle["headline"]["claim"]


def test_a_check_with_no_verdict_key_at_all_is_inconclusive():
    """Absent evidence renders as absent, never as a pass."""
    verdict = {
        "checks": [{"id": "gate_integrity", "evidence": "never ran"},
                   {"id": "payload_integrity", "verdict": "pass", "evidence": "y"}],
        "runs": 1, "model": "m", "log_integrity": {"sound": True},
    }
    bundle = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    assert bundle["verdict"]["checks"][0]["verdict"] == "inconclusive"
    assert bundle["headline"]["status"] == "inconclusive"


def test_missing_verdict_is_not_a_pass():
    bundle = export.build_bundle(_approved_run(), _integrity(5), None, commit="c")
    assert bundle["verdict"] is None
    assert bundle["headline"]["status"] == "unavailable"
    assert len(bundle["headline"]["qualifiers"]) == 3


def test_headline_is_claimed_only_with_both_checks_passing_and_a_sound_log():
    verdict = {
        "checks": [
            {"id": "gate_integrity", "verdict": "pass", "evidence": "1 mutating call",
             "severity": "critical"},
            {"id": "payload_integrity", "verdict": "pass", "evidence": "0 divergences",
             "severity": "critical"},
        ],
        "runs": 3, "model": "MiniMax-M3", "provider": "minimax",
        "log_integrity": {"sound": True, "evidence": "5 record(s), no losses"},
    }
    bundle = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    head = bundle["headline"]
    assert head["status"] == "claimed"
    assert "that the guard detected" in head["claim"]
    assert "actually exercised each check" in head["claim"]
    assert "MiniMax-M3" in head["claim"]
    assert len(head["qualifiers"]) == 3

    verdict["log_integrity"]["sound"] = False
    unsound = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    assert unsound["headline"]["status"] == "inconclusive"


def test_every_run_round_trips_in_order():
    records = _approved_run("runA") + _approved_run("runB") + _approved_run("runC")
    bundle = export.build_bundle(records, _integrity(len(records)), None, commit="c")
    assert [r["run_id"] for r in bundle["runs"]] == ["runA", "runB", "runC"]
    for run in bundle["runs"]:
        assert [e["event"] for e in run["events"]] == [
            "model_turn", "tool_call", "approval_requested", "approval_resolved", "tool_call",
        ]


def test_human_denial_and_machine_denial_are_distinguishable():
    """Same Decision, completely different evidence — see server.WebApprover.decide."""
    records = [
        _event("approval_requested", approval_id="ap_h", tool="gmail_send",
               payload_hash="a" * 64, rendered_chars=10),
        _event("approval_resolved", approval_id="ap_h", tool="gmail_send", decision="denied"),
        _event("approval_requested", approval_id="ap_m", tool="slack_post_message",
               payload_hash="b" * 64, rendered_chars=10),
        _event("approval_auto_denied", approval_id="ap_m", tool="slack_post_message",
               reason="approver_stopped"),
        _event("approval_resolved", approval_id="ap_m", tool="slack_post_message",
               decision="denied"),
    ]
    bundle = export.build_bundle(records, _integrity(5), None, commit="c")
    rows = {a["approval_id"]: a for a in bundle["runs"][0]["approvals"]}
    assert rows["ap_h"]["decision"] == "denied"
    assert rows["ap_h"]["decided_by"] == "human"
    assert rows["ap_m"]["decision"] == "denied"
    assert rows["ap_m"]["decided_by"] == "machine"
    assert rows["ap_m"]["machine_reason"] == "approver_stopped"


def test_approval_carries_the_typed_artifact_not_model_markup():
    bundle = export.build_bundle(_approved_run(), _integrity(5), None, commit="c")
    approval = bundle["runs"][0]["approvals"][0]
    assert approval["tool"] == "gmail_send"
    assert "<b>Email</b>" in approval["artifact_html"]
    assert "This leaves your account" in approval["artifact_html"]
    assert export.WITHHELD in approval["artifact_html"]


def test_tabs_are_derived_from_tool_names_not_hardcoded():
    """A fourth integration must appear the day it appears in the data."""
    records = _approved_run() + [
        _event("tool_call", tool="slack_post_message", ok=True, mutating=True,
               approval_id="ap_9", result_chars=3),
        _event("tool_call", tool="notion_create_page", ok=True, mutating=True,
               approval_id="ap_10", result_chars=3),
    ]
    bundle = export.build_bundle(records, _integrity(7), None, commit="c")
    tabs = {t["key"]: t["label"] for t in bundle["integrations"]}
    assert tabs == {"stripe": "Stripe", "gmail": "Email", "slack": "Slack", "notion": "Notion"}


def test_payload_hash_is_truncated_and_paths_are_stripped():
    verdict = {
        "checks": [{"id": "gate_integrity", "verdict": "pass",
                    "evidence": "read /home/someone/.ctrl-a/jr/activity.jsonl",
                    "severity": "critical"}],
        "runs": 1, "model": "m", "log_integrity": {"sound": True,
                                                   "evidence": "no activity log at "
                                                               "/home/someone/.ctrl-a/jr/"
                                                               "activity.jsonl"},
    }
    bundle = export.build_bundle(_approved_run(), _integrity(5), verdict, commit="c")
    serialised = json.dumps(bundle)
    assert "/home/someone" not in serialised
    assert "a" * 64 not in serialised
    assert bundle["runs"][0]["approvals"][0]["payload_hash"] == "a" * export.HASH_PREFIX


def test_secret_tripwire_raises_rather_than_scrubbing(monkeypatch):
    """If the allowlist ever has a hole, the export fails loudly."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_SENTINEL0000000000")
    with pytest.raises(export.SecretLeak):
        export.assert_no_secrets('{"oops": "sk_test_SENTINEL0000000000"}')


def test_export_evidence_writes_a_file(tmp_path, monkeypatch):
    log = tmp_path / "activity.jsonl"
    log.write_text("".join(json.dumps(r) + chr(10) for r in _approved_run()), encoding="utf-8")
    out = tmp_path / "out" / "evidence.json"
    bundle = export.export_evidence(log_path=log, out=out)
    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["schema"] == export.SCHEMA
    assert written["generated_at"]
    assert written["commit"]
    assert len(written["runs"]) == len(bundle["runs"]) == 1
    assert written["verdict"] is not None
