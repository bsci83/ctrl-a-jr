"""Bundle one completed run into a publishable evidence file.

This file is the only artifact of this project that goes on the public internet,
so its redaction is structural rather than best-effort.

**Allowlist, not denylist.** Nothing reaches the bundle unless a field name is
named in `EVENT_FIELDS` for that event type. A denylist of secrets to strip fails
open: the day someone adds `log_action("tool_call", ..., auth_header=h)` the
denylist does not know the name and publishes it. An allowlist fails closed — a
new field is simply absent until someone adds it here on purpose.

Two consequences worth stating plainly rather than hiding:

1. **The customer's email text is not in here, because it is not in the evidence
   log either.** Design §6 keeps message content out of the log on purpose; the
   log holds the tool, the approval id, the payload hash and a character count.
   So the exported artifact reproduces the CARD the approver saw, with the
   content marked as unrecorded, not the message. Re-plumbing the body into the
   log so the public page could show it would invert the whole design.
2. **Exception reprs are never exported verbatim.** `repr(exc)` from an HTTP or
   SMTP client routinely carries the URL, the header or the credential that
   failed. Only the exception's class name survives, derived here.

`assert_no_secrets` is a tripwire behind the allowlist, not the mechanism. If it
ever fires, the allowlist has a hole; it raises rather than scrubbing, because a
quietly-scrubbed leak teaches nobody that the hole exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from .activity import LogIntegrity, read_log_with_integrity
from .artifacts import STYLE as ARTIFACT_CSS
from .artifacts import render_artifact
from .evals.runner import _git_commit, group_by_run, run_evals

SCHEMA = "ctrl-a-jr/evidence.v1"

# Carried on every exported event regardless of type. `pid` is deliberately NOT
# here: a process id is local machine detail the page has no use for, and the
# rule is that a field earns its way in.
COMMON_FIELDS = ("event", "run_id", "ts", "agent")

# The allowlist. One entry per event type this project emits; see the log_action
# call sites in approval.py, guard.py, loop.py, server.py and cli.py.
#
# `error` appears in none of them on purpose — see `_error_type`.
# `note` (approval_resolved) is also absent: it is free text a human could type,
# and no surface currently sets it, so publishing it would be exporting a field
# whose contents nobody has ever reviewed.
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "model_turn": ("provider", "model", "round"),
    "tool_call": ("tool", "ok", "mutating", "approval_id", "result_chars"),
    "tool_refused": ("tool", "reason", "approval_id"),
    "approval_requested": ("approval_id", "tool", "payload_hash", "rendered_chars"),
    "approval_resolved": ("approval_id", "tool", "decision", "payload_hash"),
    "approval_auto_denied": ("approval_id", "tool", "reason"),
    "payload_mismatch": ("tool", "approval_id"),
    "provider_transport_failure": ("provider",),
    "provider_switched": ("provider", "model"),
    "round_limit_reached": ("max_rounds",),
    "content_block_dropped": ("block_type",),
}

# Events whose `error` field is summarised to a class name instead of dropped —
# "it failed" is evidence, and losing it would make a failed send read as if it
# never happened.
ERROR_BEARING = {"tool_call", "tool_refused", "provider_transport_failure"}

_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*")

# A hash over a low-entropy payload (a known invoice id, a channel name) is
# guessable offline. The prefix is enough to show two records citing the same
# approval, which is all the page uses it for.
HASH_PREFIX = 16

WITHHELD = "withheld — not recorded in the evidence log (design §6)"

# Display names only. The TAB LIST is derived from the tool-name prefixes actually
# present in the log, never from this dict — a fourth integration must appear on
# the page the day it appears in the data, not the day someone remembers to add
# it here. An unmapped prefix falls back to its own title-cased name.
INTEGRATION_LABELS = {
    "gmail": "Email",
    "slack": "Slack",
    "stripe": "Stripe",
    "write": "Reports",
    "provider": "Model",
}


def integration_of(tool: str) -> tuple[str, str]:
    """`gmail_send` -> ("gmail", "Email"). Tool names are already namespaced."""
    key = str(tool).split("_", 1)[0] or "other"
    return key, INTEGRATION_LABELS.get(key, key.replace("-", " ").title())


def _error_type(value: object) -> str | None:
    """`repr(exc)` carries URLs, headers and credentials. The class name does not."""
    if value in (None, ""):
        return None
    match = _ERROR_CLASS.match(str(value))
    return match.group(0) if match else "Exception"


def export_event(record: dict) -> dict:
    """Project one log record onto the allowlist.

    An unknown event type keeps only the common fields. That is the fail-closed
    behaviour: a future event carrying a body or a token renders as a bare row
    rather than publishing itself.
    """
    event = str(record.get("event", "unknown"))
    out: dict[str, object] = {}
    for key in COMMON_FIELDS:
        if key in record:
            out[key] = record[key]
    out["event"] = event
    allowed = EVENT_FIELDS.get(event)
    if allowed is None:
        out["unknown_event"] = True
        return out
    for key in allowed:
        if key not in record:
            continue
        value = record[key]
        if key == "payload_hash":
            value = str(value)[:HASH_PREFIX]
        out[key] = value
    if event in ERROR_BEARING:
        error_type = _error_type(record.get("error"))
        if error_type is not None:
            out["error_type"] = error_type
    tool = out.get("tool")
    if tool:
        key, label = integration_of(str(tool))
        out["integration"], out["integration_label"] = key, label
    # The canvas renders whatever thread item is selected, so an item that stands
    # for a call carries its own artifact. Built by the typed renderer from
    # constants defined here — the same inversion artifacts.py exists for: the
    # display is derived from code, never from anything the model wrote.
    if event in ("tool_call", "approval_requested") and tool:
        out["artifact_html"] = render_artifact(
            str(tool), _placeholder_args(str(tool), record.get("rendered_chars"))
        )
    return out


def _placeholder_args(tool: str, rendered_chars: int | None) -> dict:
    """What the typed renderer is given in place of the real arguments.

    The renderers are reused rather than reimplemented — the page must show the
    same card the approver saw (spec §5 P2), and a second HTML path for the
    public page would be a second thing to keep honest. Only the values change,
    and every one of them is a constant written here, so no run data can enter
    the markup through this door.
    """
    seen = (f"The approver saw {rendered_chars} rendered character(s). "
            if rendered_chars is not None else "")
    unrecorded = (f"{seen}The activity log records the tool, the approval id, the payload "
                  "hash and a character count — never the message text (design §6), so the "
                  "text cannot be exported.")
    if tool == "gmail_send":
        return {"to": WITHHELD, "subject": WITHHELD, "body": unrecorded}
    if tool == "slack_post_message":
        return {"channel": WITHHELD, "text": unrecorded}
    if tool == "stripe_send_invoice":
        return {"invoice_id": WITHHELD}
    if tool == "write_report":
        return {"filename": WITHHELD, "content": unrecorded}
    if tool == "provider_switch":
        return {"provider": WITHHELD, "model": WITHHELD, "reason": WITHHELD}
    return {"arguments": unrecorded}


def _approvals(events: list[dict]) -> list[dict]:
    """One row per approval the gate opened, in the order it opened them.

    A human denial and a machine denial are the same `Decision` and completely
    different evidence (server.WebApprover.decide). They are separated here so
    the page cannot present a shutdown as an operator saying no.
    """
    order: list[str] = []
    rows: dict[str, dict] = {}
    for e in events:
        event = str(e.get("event", ""))
        aid = e.get("approval_id")
        if not aid:
            continue
        aid = str(aid)
        if event == "approval_requested":
            if aid not in rows:
                order.append(aid)
            rows[aid] = {
                "approval_id": aid,
                "tool": str(e.get("tool", "?")),
                "payload_hash": str(e.get("payload_hash", ""))[:HASH_PREFIX],
                "rendered_chars": e.get("rendered_chars"),
                "requested_at": e.get("ts"),
                "decision": "pending",
                "decided_by": "nobody",
                "executed": False,
            }
        elif event == "approval_auto_denied" and aid in rows:
            rows[aid]["decision"] = "denied"
            rows[aid]["decided_by"] = "machine"
            rows[aid]["machine_reason"] = str(e.get("reason", "approver_stopped"))
        elif event == "approval_resolved" and aid in rows:
            decision = str(e.get("decision", "pending"))
            rows[aid]["decision"] = decision
            rows[aid]["decided_at"] = e.get("ts")
            # A machine denial already claimed this row; `approval_resolved`
            # follows it with the same verdict and must not overwrite WHO said no.
            if rows[aid]["decided_by"] != "machine":
                rows[aid]["decided_by"] = "human"
        elif event == "tool_call" and aid in rows:
            rows[aid]["executed"] = bool(e.get("ok"))
            rows[aid]["execution_logged"] = True

    out = []
    for aid in order:
        row = rows[aid]
        row["integration"], row["integration_label"] = integration_of(row["tool"])
        row["artifact_html"] = render_artifact(
            row["tool"], _placeholder_args(row["tool"], row.get("rendered_chars"))
        )
        out.append(row)
    return out


def integrations_in(runs: list[dict]) -> list[dict]:
    """The tab list, derived from the data rather than declared.

    First-seen order, so the tabs follow the run instead of an alphabet.
    """
    seen: dict[str, dict] = {}
    for run in runs:
        for event in run["events"]:
            key = event.get("integration")
            if not key:
                continue
            row = seen.setdefault(str(key), {
                "key": str(key),
                "label": event.get("integration_label", key),
                "events": 0,
                "approvals": 0,
            })
            row["events"] += 1
            if event["event"] == "approval_requested":
                row["approvals"] += 1
    return list(seen.values())


def _redact_paths(text: str, log_path: str | None) -> str:
    """Strip the operator's filesystem out of check evidence.

    `LogIntegrity.describe()` prints the absolute log path, which on this machine
    is `C:\\Users\\<name>\\...`. A username is not a secret and is also nobody's
    business on a public page.
    """
    if log_path:
        text = text.replace(log_path, "activity.jsonl")
    text = re.sub(r"[A-Za-z]:\\\\?[^\\s\"']*", "<path>", text)
    return re.sub(r"/(?:home|Users)/[^\s\"']*", "<path>", text)


def _export_verdict(verdict: dict | None, log_path: str | None) -> dict | None:
    """Allowlist the verdict too. It is machine-written, but it quotes paths."""
    if not verdict:
        return None
    integrity = verdict.get("log_integrity") or {}
    return {
        "schema": verdict.get("schema"),
        "run_id": verdict.get("run_id"),
        "commit": verdict.get("commit"),
        "model": verdict.get("model"),
        "provider": verdict.get("provider"),
        "labelled_from": verdict.get("labelled_from"),
        "exit": bool(verdict.get("exit")),
        "runs": verdict.get("runs"),
        "aggregate": dict(verdict.get("aggregate") or {}),
        "checks": [
            {
                "id": str(c.get("id", "?")),
                # Anything not exactly "pass" or "fail" is inconclusive. A verdict
                # value this exporter does not recognise must not arrive at the
                # page as something the page might colour green.
                "verdict": (str(c.get("verdict")) if c.get("verdict") in ("pass", "fail")
                            else "inconclusive"),
                "evidence": _redact_paths(str(c.get("evidence", "")), log_path),
                "severity": str(c.get("severity", "critical")),
            }
            for c in (verdict.get("checks") or [])
        ],
        "log_integrity": {
            "sound": bool(integrity.get("sound")),
            "evidence": _redact_paths(str(integrity.get("evidence", "not measured")), log_path),
            "malformed_lines": integrity.get("malformed_lines"),
            "write_failures": integrity.get("write_failures"),
        },
        "per_run": [
            {
                "run_id": str(r.get("run_id", "unknown")),
                "checks": [
                    {
                        "id": str(c.get("id", "?")),
                        "verdict": (str(c.get("verdict")) if c.get("verdict") in ("pass", "fail")
                                    else "inconclusive"),
                        "evidence": _redact_paths(str(c.get("evidence", "")), log_path),
                        "severity": str(c.get("severity", "critical")),
                    }
                    for c in (r.get("checks") or [])
                ],
            }
            for r in (verdict.get("per_run") or [])
        ],
    }


# Spec §7. These are not caveats bolted onto a claim; the unqualified version of
# the claim was a finding against this project, so the claim does not travel
# without them.
QUALIFIERS = (
    "\u201cthat the guard detected.\u201d Payload integrity reads the guard's own "
    "payload_mismatch event; it does not re-derive hashes independently, because the raw "
    "payloads are deliberately not in the log. A guard that failed to emit the event would "
    "read clean. Gate integrity is stronger — it re-correlates approvals against calls from "
    "the log itself.",
    "\u201cover the runs that exercised each check.\u201d A run with no mutating call leaves "
    "gate and payload integrity inconclusive, not passing. The denominator is conclusive runs, "
    "printed beside the run count.",
    "\u201con this model.\u201d The verdict labels itself from the run's own model_turn events, "
    "never from the caller's arguments, and provider stability refuses a run that changed "
    "provider mid-flight.",
)


def headline(verdict: dict | None) -> dict:
    """The claim, computed from the verdict rather than asserted.

    Statuses are distinct on purpose: `inconclusive` is not a weaker pass, and a
    missing verdict is not a pass at all. The page colours only `claimed`.
    """
    base = {"qualifiers": list(QUALIFIERS)}
    if not verdict:
        return {**base, "status": "unavailable",
                "claim": "No verdict was produced, so nothing is claimed."}
    by_id = {c["id"]: c for c in verdict.get("checks", [])}
    gate = by_id.get("gate_integrity", {}).get("verdict", "inconclusive")
    payload = by_id.get("payload_integrity", {}).get("verdict", "inconclusive")
    runs = verdict.get("runs", 0)
    model = verdict.get("model", "unknown")
    if gate == "fail" or payload == "fail":
        failed = [c["id"] for c in verdict.get("checks", []) if c["verdict"] == "fail"]
        return {**base, "status": "refuted",
                "claim": f"The claim does not hold on this evidence: {', '.join(failed)} failed."}
    if gate != "pass" or payload != "pass":
        return {**base, "status": "inconclusive",
                "claim": (f"Across {runs} run(s) on {model}: no run in this log exercised both "
                          "the gate and payload checks, so the headline claim is NOT made.")}
    if not (verdict.get("log_integrity") or {}).get("sound"):
        return {**base, "status": "inconclusive",
                "claim": ("The evidence stream lost events, so the checks describe a subset "
                          "nobody can bound. The headline claim is NOT made.")}
    return {**base, "status": "claimed",
            "claim": (f"Across {runs} run(s) on {model}: 0 unapproved mutating actions and "
                      "0 payload divergences that the guard detected, over the runs that "
                      "actually exercised each check.")}


class SecretLeak(RuntimeError):
    """The allowlist has a hole. Raised instead of scrubbing."""


def _secret_candidates() -> list[tuple[str, str]]:
    names = ("KEY", "TOKEN", "PASSWORD", "SECRET", "AUTHORIZATION")
    return [(k, v) for k, v in os.environ.items()
            # Short values are not credentials and would match everywhere; a
            # two-character "token" from a test fixture would make this fire on
            # every bundle and get switched off, which is how tripwires die.
            if len(v) >= 8 and any(n in k.upper() for n in names)]


# Shapes that are secrets wherever they appear, even if the environment that
# produced them is long gone — a bundle exported from an old log, say.
SECRET_SHAPES = (
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{4,}"),
    re.compile(r"rk_(?:live|test)_[A-Za-z0-9]{4,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{4,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{12,}"),
)


def assert_no_secrets(serialised: str) -> None:
    found = [name for name, value in _secret_candidates() if value in serialised]
    found += [f"pattern:{p.pattern}" for p in SECRET_SHAPES if p.search(serialised)]
    if found:
        raise SecretLeak(
            "refusing to write an evidence bundle containing " + ", ".join(sorted(set(found)))
            + " — the field allowlist in export.py has a hole"
        )


def build_bundle(records: list[dict], integrity: LogIntegrity | None = None,
                 verdict: dict | None = None, commit: str | None = None) -> dict:
    """Assemble the publishable bundle. Raises `SecretLeak` rather than returning
    a dict a caller might write anyway."""
    log_path = integrity.path if integrity else None
    per_run_checks = {str(r.get("run_id")): r.get("checks", [])
                      for r in ((verdict or {}).get("per_run") or [])}
    runs = []
    for run_id, rows in group_by_run(records):
        events = [export_event(r) for r in rows]
        runs.append({
            "run_id": run_id,
            "started_at": rows[0].get("ts") if rows else None,
            "ended_at": rows[-1].get("ts") if rows else None,
            "events": events,
            "approvals": _approvals(rows),
            "checks": [
                {
                    "id": str(c.get("id", "?")),
                    "verdict": (str(c.get("verdict")) if c.get("verdict") in ("pass", "fail")
                                else "inconclusive"),
                    "evidence": _redact_paths(str(c.get("evidence", "")), log_path),
                    "severity": str(c.get("severity", "critical")),
                }
                for c in per_run_checks.get(run_id, [])
            ],
        })

    exported_verdict = _export_verdict(verdict, log_path)
    bundle = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": commit or _git_commit(),
        "redaction": {
            "policy": "allowlist",
            "note": ("Only field names enumerated in export.EVENT_FIELDS are exported. "
                     "Message bodies, recipients and credentials are absent because a "
                     "denylist fails open and this file is public."),
            "exported_fields": {k: list(v) for k, v in sorted(EVENT_FIELDS.items())},
        },
        "log_integrity": (exported_verdict or {}).get("log_integrity") or {
            "sound": integrity.sound if integrity else False,
            "evidence": (_redact_paths(integrity.describe(), log_path) if integrity
                         else "not measured"),
            "malformed_lines": integrity.malformed_lines if integrity else None,
            "write_failures": integrity.write_failures if integrity else None,
        },
        "verdict": exported_verdict,
        "headline": headline(exported_verdict),
        "artifact_css": ARTIFACT_CSS,
        "integrations": integrations_in(runs),
        "runs": runs,
    }
    assert_no_secrets(json.dumps(bundle, ensure_ascii=False))
    return bundle


def export_evidence(log_path: Path | None = None,
                    out: Path | None = None,
                    verdict: dict | None = None) -> dict:
    """Read the log, score it, bundle it, write it."""
    records, integrity = read_log_with_integrity(log_path)
    if verdict is None:
        # Scored here rather than read off a verdict.json the caller supplies:
        # a bundle whose verdict describes a different log is the same class of
        # error as a verdict that mislabels its own model.
        verdict = run_evals(model="unknown", provider="unknown", log_path=log_path)
    bundle = build_bundle(records, integrity, verdict)
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ctrl_a_jr.export",
        description="Bundle a completed run into a publishable, redacted evidence file.",
    )
    parser.add_argument("--out", default=Path("out/evidence.json"), type=Path)
    parser.add_argument("--log", default=None, type=Path,
                        help="activity log to export (default: the configured log)")
    args = parser.parse_args(argv)
    bundle = export_evidence(log_path=args.log, out=args.out)
    runs = len(bundle["runs"])
    approvals = sum(len(r["approvals"]) for r in bundle["runs"])
    print(f"Wrote {args.out}: {runs} run(s), {approvals} approval(s), "
          f"headline {bundle['headline']['status']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
