"""Run the checks and emit a verdict.

The verdict names the model and provider. A reliability number that spans an
unrecorded configuration change looks rigorous and is not true of either
system it averaged — see docs/design §7a.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..activity import read_log
from . import checks
from .checks import CheckResult

DETERMINISTIC = (
    checks.check_gate_integrity,
    checks.check_payload_integrity,
    checks.check_denial_handling,
)


def build_verdict(results: list[CheckResult], model: str, provider: str) -> dict:
    counts = {"pass": 0, "fail": 0, "inconclusive": 0}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return {
        "schema": "ctrl-a-jr/verdict.v1",
        "model": model,
        "provider": provider,
        "exit": counts["fail"] == 0,
        "checks": [
            {"id": r.id, "verdict": r.verdict, "evidence": r.evidence, "severity": r.severity}
            for r in results
        ],
        "aggregate": counts,
        "regressed_this_cycle": [],
        "disputed": [],
        "next_actions": [r.evidence for r in results if r.verdict == "fail"],
    }


def run_evals(model: str, provider: str, log_path: Path | None = None,
              out: Path | None = None) -> dict:
    records = read_log(log_path)
    results = [fn(records) for fn in DETERMINISTIC]
    verdict = build_verdict(results, model=model, provider=provider)
    if out is not None:
        Path(out).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict
