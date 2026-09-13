"""Pre-flight credential and connectivity checks.

A bad token otherwise surfaces as a failed tool call in the middle of a run,
which on demo day is ten minutes of confusion at the worst possible moment. This
turns "the run broke" into "this one credential is wrong."

Every check is independent: one failure never hides another, so a single pass
tells you everything that needs fixing rather than the first thing.

No check ever prints a secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def check_stripe() -> Check:
    if not _env("STRIPE_SECRET_KEY"):
        return Check("Stripe", False, "STRIPE_SECRET_KEY not set")
    try:
        from .tools.stripe_tools import StripeClient
        client = StripeClient(_env("STRIPE_SECRET_KEY"))
        invoices = client.list_failed_payments(limit=3)
        return Check("Stripe", True, f"test mode, {len(invoices)} open invoice(s) visible")
    except ValueError as exc:
        return Check("Stripe", False, str(exc)[:70])
    except Exception as exc:  # noqa: BLE001
        return Check("Stripe", False, f"{type(exc).__name__}: {str(exc)[:60]}")


def check_gmail_smtp() -> Check:
    addr, pw = _env("GMAIL_ADDRESS"), _env("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        return Check("Gmail SMTP", False, "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    try:
        import smtplib
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(addr, pw)
        return Check("Gmail SMTP", True, f"auth ok as {addr}")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "Username and Password not accepted" in str(exc):
            hint = " — app password wrong, or 2-Step Verification is off"
        return Check("Gmail SMTP", False, f"{type(exc).__name__}{hint}")


def check_gmail_imap() -> Check:
    """IMAP is OFF by default on some accounts — a distinct failure from SMTP."""
    addr, pw = _env("GMAIL_ADDRESS"), _env("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        return Check("Gmail IMAP", False, "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    try:
        import imaplib
        c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            c.login(addr, pw)
            typ, _ = c.select("INBOX", readonly=True)
            if typ != "OK":
                return Check("Gmail IMAP", False, "cannot select INBOX")
            return Check("Gmail IMAP", True, "INBOX readable (readonly)")
        finally:
            try:
                c.logout()
            except Exception:  # noqa: BLE001, S110
                pass
    except Exception as exc:  # noqa: BLE001
        hint = " — enable IMAP in Gmail settings" if "IMAP" in str(exc).upper() else ""
        return Check("Gmail IMAP", False, f"{type(exc).__name__}{hint}")


def check_slack() -> Check:
    token = _env("SLACK_BOT_TOKEN")
    if not token:
        return Check("Slack", False, "SLACK_BOT_TOKEN not set")
    try:
        import httpx
        r = httpx.post("https://slack.com/api/auth.test",
                       headers={"Authorization": f"Bearer {token}"}, timeout=20)
        body = r.json()
        if not body.get("ok"):
            return Check("Slack", False, f"{body.get('error', 'unknown')}")
        return Check("Slack", True, f"{body.get('team', '?')} as {body.get('user', '?')}")
    except Exception as exc:  # noqa: BLE001
        return Check("Slack", False, f"{type(exc).__name__}: {str(exc)[:60]}")


def check_model() -> Check:
    provider = _env("CTRLA_JR_PROVIDER") or "minimax"
    model = _env("CTRLA_JR_MODEL") or "MiniMax-M3"
    key = "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"
    if not _env(key):
        return Check("Model", False, f"{key} not set")
    try:
        from .providers import ProviderState, client_from_env
        client = client_from_env(ProviderState(provider, model))
        client.create(system="reply with ok", messages=[{"role": "user", "content": "ok"}], tools=[])
        return Check("Model", True, f"{provider} / {model} responded")
    except Exception as exc:  # noqa: BLE001
        return Check("Model", False, f"{type(exc).__name__}: {str(exc)[:60]}")


CHECKS = (check_stripe, check_gmail_smtp, check_gmail_imap, check_slack, check_model)


def run_doctor() -> list[Check]:
    return [fn() for fn in CHECKS]


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        status = "ok  " if c.ok else "FAIL"
        lines.append(f"  {c.name:<12} {status}  {c.detail}")
    failed = [c for c in checks if not c.ok]
    lines.append("")
    lines.append("  all green — you are clear to run" if not failed
                 else f"  {len(failed)} of {len(checks)} need fixing before a run")
    return "\n".join(lines)
