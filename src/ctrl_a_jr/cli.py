"""Entry points: `ctrl-a-jr run` and `ctrl-a-jr eval`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from .activity import log_action
from .approval import ApprovalStore
from .evals.runner import run_evals
from .guard import Guard
from .loop import run_loop
from .providers import ProviderState, client_from_env, register_provider_tools
from .registry import Registry
from .server import WebApprover
from .tools.gmail_tools import GmailClient, register_gmail_tools
from .tools.report_tools import register_report_tools
from .tools.slack_tools import SlackClient, register_slack_tools
from .tools.stripe_tools import StripeClient, register_stripe_tools

SYSTEM = """You recover failed payments for a small business.

Work in this order:
1. List open invoices that need recovery.
2. For the most overdue one, fetch the invoice and the customer.
3. Search email from that customer so you know what they have already said.
4. Draft ONE recovery email. Use the real amount and due date from Stripe —
   never estimate or invent a figure.
5. Send it. A human reviews every send before it leaves.
6. If the amount is over $500, post a short note to Slack.
7. Write a short report of what you did.

If a human denies an action, report the denial and stop. Do not retry it and do
not attempt the same thing through a different tool."""

USER_TASK = "Recover the most overdue open invoice."


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env support. A real dependency is not worth it for KEY=value."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}. See .env.example.")
    return value


def _build(out_dir: Path) -> tuple[Registry, ProviderState]:
    reg = Registry()
    register_stripe_tools(reg, StripeClient(_require("STRIPE_SECRET_KEY")))
    register_gmail_tools(reg, GmailClient(_require("GMAIL_ADDRESS"),
                                          _require("GMAIL_APP_PASSWORD")))
    register_slack_tools(reg, SlackClient(_require("SLACK_BOT_TOKEN")))
    register_report_tools(reg, out_dir)
    state = ProviderState(os.environ.get("CTRLA_JR_PROVIDER", "minimax"),
                          os.environ.get("CTRLA_JR_MODEL", "MiniMax-M3"))
    register_provider_tools(reg, state)
    return reg, state


def _fixtures(action: str) -> int:
    """Dev tooling. Deliberately NOT registered as agent tools — the agent may read
    invoices and ask Stripe to send them, never create or delete a customer."""
    from .fixtures import FixtureSeeder

    seeder = FixtureSeeder(_require("STRIPE_SECRET_KEY"), _require("GMAIL_ADDRESS"))
    if action == "seed":
        rows = seeder.seed()
        for r in rows:
            print(f"  {r['name']:<16} {r['invoice_id']}  "
                  f"${r['amount_due'] / 100:,.2f}  {r['days_overdue']}d overdue")
        print(f"\nSeeded {len(rows)} overdue invoice(s) to {seeder.email}.")
        return 0
    if action == "list":
        rows = seeder.list_fixtures()
        for r in rows:
            print(f"  {r['customer_id']}  {r['name']}  {r['email']}")
        print(f"\n{len(rows)} fixture customer(s).")
        return 0
    removed = seeder.teardown()
    print(f"Removed {len(removed)} fixture customer(s). Untagged customers untouched.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="ctrl-a-jr")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run one recovery pass")
    run_p.add_argument("--out", default="./out", type=Path)
    run_p.add_argument("--port", default=8765, type=int)
    eval_p = sub.add_parser("eval", help="score the activity log")
    eval_p.add_argument("--out", default="verdict.json", type=Path)
    fx_p = sub.add_parser("fixtures", help="seed / list / tear down Stripe test fixtures")
    fx_p.add_argument("action", choices=["seed", "list", "teardown"])

    args = parser.parse_args(argv)

    if args.cmd == "fixtures":
        return _fixtures(args.action)

    if args.cmd == "eval":
        state = ProviderState(os.environ.get("CTRLA_JR_PROVIDER", "minimax"),
                              os.environ.get("CTRLA_JR_MODEL", "MiniMax-M3"))
        verdict = run_evals(model=state.model, provider=state.provider, out=args.out)
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["exit"] else 1

    args.out.mkdir(parents=True, exist_ok=True)
    registry, state = _build(args.out)
    store = ApprovalStore()
    approver = WebApprover(store, port=args.port)
    approver.start()
    print(f"Approvals: {approver.url}")
    webbrowser.open(approver.url)
    try:
        guard = Guard(registry, store, approver)
        try:
            result = run_loop(client_from_env(state), guard, SYSTEM, USER_TASK)
        except Exception as exc:  # noqa: BLE001 - transport failure, not a tool failure
            # Spec §7a: a transport failure pauses the run and asks a human whether to
            # continue on the fallback. The model cannot request this itself — being
            # unreachable is precisely the condition we are handling.
            log_action("provider_transport_failure", provider=state.provider, error=repr(exc))
            fallback_model = os.environ.get("CTRLA_JR_FALLBACK_MODEL", "anthropic/claude-sonnet-4")
            outcome = guard.dispatch("provider_switch", {
                "provider": "openrouter",
                "model": fallback_model,
                "reason": f"{state.provider} unreachable: {exc}",
            })
            if not outcome.ok:
                print(f"\n{state.provider} is unreachable and the switch was not approved. "
                      "Stopping.")
                return 1
            result = run_loop(client_from_env(state), guard, SYSTEM, USER_TASK)
        print("\n" + result.text)
    finally:
        approver.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
