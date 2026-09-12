"""Test-wide guarantees.

The activity log is not a debug convenience — it is the evidence stream the eval
harness reads, and the basis of this project's reliability claim. So a test run
must never write into it.

That was not true until 2026-09-12: eight of fourteen test files redirected the
log and six did not, and there was no conftest. 312 phantom `approval_requested`
records accumulated in the operator's real log at ~8 per `pytest` invocation.
The checks ignored them, so nothing failed — which is exactly why it went
unnoticed for three days.

Redirecting per-file was the wrong shape: it protects the files someone
remembered to change. This fixture is autouse and session-wide, so a new test
file is protected by default rather than by diligence.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_activity_log(tmp_path, monkeypatch):
    """Point every test's activity log at a per-test temp file.

    autouse: a test that forgets to ask for isolation still gets it.
    """
    monkeypatch.setenv("CTRLA_JR_ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))
