"""Doctor checks: independent, specific, and never leaking a secret."""

from ctrl_a_jr import doctor


def _clear(monkeypatch):
    for v in ("STRIPE_SECRET_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
              "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_every_check_reports_missing_config_rather_than_raising(monkeypatch):
    """A missing credential must be a FAIL line, not a traceback."""
    _clear(monkeypatch)
    checks = doctor.run_doctor()
    assert len(checks) == 5
    assert all(c.ok is False for c in checks)
    assert all(c.detail for c in checks), "every failure must say why"


def test_one_failure_does_not_hide_another(monkeypatch):
    """Independent checks: a single pass shows everything that needs fixing."""
    _clear(monkeypatch)
    names = [c.name for c in doctor.run_doctor()]
    assert names == ["Stripe", "Gmail SMTP", "Gmail IMAP", "Slack", "Model"]


def test_gmail_smtp_and_imap_are_separate_checks(monkeypatch):
    """IMAP is off by default on some accounts — a distinct failure from SMTP auth."""
    _clear(monkeypatch)
    names = {c.name for c in doctor.run_doctor()}
    assert "Gmail SMTP" in names and "Gmail IMAP" in names


def test_a_live_stripe_key_is_reported_not_used(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_danger")
    c = doctor.check_stripe()
    assert c.ok is False
    assert "sk_test" in c.detail


def test_no_check_ever_prints_a_secret(monkeypatch):
    _clear(monkeypatch)
    secrets = {"STRIPE_SECRET_KEY": "sk_live_SUPERSECRET",
               "SLACK_BOT_TOKEN": "xoxb-SUPERSECRET",
               "GMAIL_APP_PASSWORD": "SUPERSECRET",
               "GMAIL_ADDRESS": "me@example.com",
               "ANTHROPIC_API_KEY": "SUPERSECRET"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    report = doctor.format_report(doctor.run_doctor())
    assert "SUPERSECRET" not in report


def test_the_report_says_plainly_whether_you_can_run(monkeypatch):
    _clear(monkeypatch)
    assert "need fixing" in doctor.format_report(doctor.run_doctor())
    ok = [doctor.Check("A", True, "fine"), doctor.Check("B", True, "fine")]
    assert "all green" in doctor.format_report(ok)
