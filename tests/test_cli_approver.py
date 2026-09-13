"""Which surface answers approvals — and what happens when it is misconfigured."""

from __future__ import annotations

import pytest

from ctrl_a_jr import cli, loop
from ctrl_a_jr.approval import ApprovalStore
from ctrl_a_jr.approver_remote import RemoteApprover
from ctrl_a_jr.server import WebApprover


@pytest.fixture
def _remote_env(monkeypatch):
    monkeypatch.setenv("CTRLA_JR_APPROVAL_API", "https://approvals.example.test")
    monkeypatch.setenv("CTRLA_JR_PUSH_TOKEN", "push-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("CTRLA_JR_SLACK_CHANNEL", "#ar")


def test_local_is_the_default_surface():
    """The path that has actually gated live sends stays the default."""
    approver, banner, open_url = cli._make_approver(ApprovalStore(), "local", 0)
    assert isinstance(approver, WebApprover)
    assert open_url and open_url.startswith("http://127.0.0.1")
    assert any("Approvals:" in line for line in banner)


def test_remote_is_selected_when_asked_and_configured(_remote_env):
    approver, banner, open_url = cli._make_approver(ApprovalStore(), "remote", 0)
    assert isinstance(approver, RemoteApprover)
    # Nothing to open locally, and no local browser tab to mislead the operator.
    assert open_url is None
    assert "https://approvals.example.test" in banner[0]


@pytest.mark.parametrize("missing", ["CTRLA_JR_APPROVAL_API", "CTRLA_JR_PUSH_TOKEN"])
def test_remote_without_its_config_fails_naming_the_variable(_remote_env, monkeypatch, missing):
    """Falling back to local because a variable was unset would move the
    authorization boundary without telling anyone."""
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli._make_approver(ApprovalStore(), "remote", 0)
    assert missing in str(exc.value)


def test_a_typo_in_the_approver_env_is_refused_not_silently_local(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        cli._make_approver(ApprovalStore(), "remot", 0)
    assert "CTRLA_JR_APPROVER" in str(exc.value)


def test_the_round_budget_covers_the_full_task():
    """5 was one round short: an extra gmail_read_thread pushed the report past
    the limit on a live run. The boundary behaviour itself is unchanged —
    test_loop.py still pins the tools-off final turn."""
    assert loop.MAX_ROUNDS == 8
