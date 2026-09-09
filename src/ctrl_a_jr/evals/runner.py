"""Run the checks and emit a verdict.

The verdict names the model and provider. A reliability number that spans an
unrecorded configuration change looks rigorous and is not true of either
system it averaged — see docs/design §7a.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from ..activity import read_log
from . import checks
from .checks import CheckResult

DETERMINISTIC = (
    checks.check_gate_integrity,
    checks.check_payload_integrity,
    checks.check_denial_handling,
    checks.check_provider_stability,
)


def labels_from_log(records: list[dict]) -> tuple[str, str]:
    """Read provider/model off the run itself. A verdict must not be able to
    mislabel its own subject."""
    seen = {(str(r.get("provider", "?")), str(r.get("model", "?")))
            for r in records if r.get("event") == "model_turn"}
    if not seen:
        return ("unknown", "unknown")
    if len(seen) > 1:
        providers = "+".join(sorted(p for p, _ in seen))
        models = "+".join(sorted(m for _, m in seen))
        return (providers, models)
    return seen.pop()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - never let commit lookup break an eval run
        return "unknown"


def build_verdict(results: list[CheckResult], model: str, provider: str,
                  records: list[dict] | None = None) -> dict:
    counts = {"pass": 0, "fail": 0, "inconclusive": 0}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    labelled_provider, labelled_model = labels_from_log(records or [])
    return {
        "schema": "ctrl-a-jr/verdict.v1",
        "run_id": uuid.uuid4().hex,
        "commit": _git_commit(),
        "model": labelled_model,
        "provider": labelled_provider,
        "labelled_from": "activity log",
        "exit": counts["fail"] == 0 and counts["pass"] > 0,
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
    """`model`/`provider` are accepted for backward compatibility but ignored for the
    verdict body — see `labels_from_log`. A caller cannot mislabel a run it did not
    produce."""
    records = read_log(log_path)
    results = [fn(records) for fn in DETERMINISTIC]
    verdict = build_verdict(results, model=model, provider=provider, records=records)
    if out is not None:
        Path(out).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict
