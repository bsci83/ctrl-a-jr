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


def _build(out_dir: Path) -> tuple[Registry, ProviderState]:
    reg = Registry()
    register_stripe_tools(reg, StripeClient(os.environ["STRIPE_SECRET_KEY"]))
    register_gmail_tools(reg, GmailClient(os.environ["GMAIL_ADDRESS"],
                                          os.environ["GMAIL_APP_PASSWORD"]))
    register_slack_tools(reg, SlackClient(os.environ["SLACK_BOT_TOKEN"]))
    register_report_tools(reg, out_dir)
    state = ProviderState(os.environ.get("CTRLA_JR_PROVIDER", "minimax"),
                          os.environ.get("CTRLA_JR_MODEL", "MiniMax-M3"))
    register_provider_tools(reg, state)
    return reg, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctrl-a-jr")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run one recovery pass")
    run_p.add_argument("--out", default="./out", type=Path)
    run_p.add_argument("--port", default=8765, type=int)
    eval_p = sub.add_parser("eval", help="score the activity log")
    eval_p.add_argument("--out", default="verdict.json", type=Path)

    args = parser.parse_args(argv)

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
