"""Entry points: `ctrl-a-jr run` and `ctrl-a-jr eval`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path
from uuid import uuid4

from . import activity
from .activity import log_action
from .approval import ApprovalStore
from .approver_remote import RemoteApprover
from .evals.runner import run_evals
from .guard import Guard
from .loop import run_loop
from .providers import ProviderState, client_from_env, register_provider_tools
from .registry import Registry
from .server import TOKEN_ENV, WebApprover
from .tools.gmail_tools import GmailClient, register_gmail_tools
from .tools.quote_tools import register_quote_tools
from .tools.report_tools import register_report_tools
from .tools.slack_tools import SlackClient, register_slack_tools
from .tools.stripe_tools import StripeClient, register_stripe_tools

SYSTEM = """You handle inbound quote requests for an auto detailing shop.

Work in this order:
1. Read the shop's service menu so you know what can be quoted.
2. Search the shop inbox and read the quote requests waiting there. Work from
   what the customer actually wrote — the vehicle, its condition and what they
   asked for are in their own words, not in any structured field.
3. Classify each request: ONE service, ONE vehicle size, and any add-ons, using
   only keys from the menu. If a request does not map onto the menu, say so and
   stop rather than picking the nearest thing.
4. Price it with quote_price. The price comes from the menu. Never estimate,
   never add figures up yourself, never invent a price.
5. For the job the task names, find the customer in Stripe by the shop's email
   address and match them by the name on the request. Then create the invoice
   with stripe_create_quote_invoice, passing the same classification you priced.
   You do not choose the amount — the invoice is priced from the menu.
6. Reply to the customer with the itemised quote and the hosted payment link the
   invoice returned. Use the real figures and the real link from the tools; do
   not retype a price from memory or compose a payment URL yourself. A human
   reviews every send before it leaves.
7. If the total is over $500, post a short note to Slack so the shop sees the
   big job. You do not choose the channel.
8. Write a short report of what you did.

If a human denies an action, report the denial and stop. Do not retry it and do
not attempt the same thing through a different tool."""

USER_TASK = "Quote the biggest job waiting in the shop inbox."


ENV_FILES = (Path(".env.local"), Path(".env"))


def load_dotenv(paths: tuple[Path, ...] = ENV_FILES) -> None:
    """Minimal .env support. A real dependency is not worth it for KEY=value.

    `.env.local` is read first and wins, matching the convention used across the
    rest of this operator's projects; `.env` is the committed-adjacent fallback.
    Values already in the real environment beat both — `setdefault` never
    overwrites what the shell set.
    """
    for path in paths:
        if not path.exists():
            continue
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
    register_slack_tools(reg, SlackClient(_require("SLACK_BOT_TOKEN"),
                                          _require("CTRLA_JR_SLACK_CHANNEL")))
    register_quote_tools(reg)
    register_report_tools(reg, out_dir)
    state = ProviderState(os.environ.get("CTRLA_JR_PROVIDER", "minimax"),
                          os.environ.get("CTRLA_JR_MODEL", "MiniMax-M3"))
    register_provider_tools(reg, state)
    return reg, state


APPROVER_ENV = "CTRLA_JR_APPROVER"
APPROVAL_API_ENV = "CTRLA_JR_APPROVAL_API"
PUSH_TOKEN_ENV = "CTRLA_JR_PUSH_TOKEN"


def _make_approver(store: ApprovalStore, mode: str, port: int):
    """Pick the approval surface. Local is the default and stays the default.

    The local page is the path that has actually gated live sends, so it is what
    you get unless someone asks for the deployed one by name. Selecting remote
    requires its configuration to be PRESENT, checked here: falling back to local
    because a variable was unset would silently move the authorization boundary
    without telling anyone.
    """
    if mode == "remote":
        approver = RemoteApprover(
            api_base=_require(APPROVAL_API_ENV),
            push_token=_require(PUSH_TOKEN_ENV),
            # The card is posted from inside the gate, never registered as a tool.
            slack=SlackClient(_require("SLACK_BOT_TOKEN"),
                              _require("CTRLA_JR_SLACK_CHANNEL")),
        )
        banner = [f"Approvals: {approver.url} (deployed surface; this machine "
                  "accepts no inbound connection)"]
        return approver, banner, None

    if mode != "local":
        # argparse only validates the flag, not the env default behind it. A typo
        # in CTRLA_JR_APPROVER must not quietly choose a surface for you.
        raise SystemExit(
            f"Unknown approver {mode!r}. Set {APPROVER_ENV} (or --approver) to "
            "'local' or 'remote'."
        )
    approver = WebApprover(store, port=port)
    banner = [f"Approvals: {approver.authed_url}"]
    if approver.token_is_generated:
        banner.append(f"  (generated approval token; set {TOKEN_ENV} to pin your own)")
    return approver, banner, approver.authed_url


def _fixtures(action: str) -> int:
    """Dev tooling. Deliberately NOT registered as agent tools — the agent may look
    a customer up and invoice a quoted job, never create or delete a customer."""
    from .fixtures import FixtureSeeder

    seeder = FixtureSeeder(_require("STRIPE_SECRET_KEY"), _require("GMAIL_ADDRESS"),
                           _require("GMAIL_APP_PASSWORD"))
    if action == "seed":
        rows = seeder.seed()
        for r in rows:
            flag = "  → Slack" if r.get("over_slack_threshold") else ""
            print(f"  {r['name']:<16} {r['customer_id']:<20} "
                  f"{r.get('expected_total', '?'):>10}{flag}")
        print(f"\nSeeded {len(rows)} quote request(s) to {seeder.email}. "
              "Expected totals are what the MENU prices, not a promise about the run.")
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
    run_p = sub.add_parser("run", help="run one quote-handling pass")
    run_p.add_argument("--out", default="./out", type=Path)
    run_p.add_argument("--port", default=8765, type=int)
    run_p.add_argument(
        "--approver", choices=["local", "remote"],
        default=(os.environ.get(APPROVER_ENV, "local").strip().lower() or "local"),
        help="where approvals are answered: the local page (default) or the "
             "deployed API this agent pushes to and polls.",
    )
    eval_p = sub.add_parser("eval", help="score the activity log")
    eval_p.add_argument("--out", default="verdict.json", type=Path)
    fx_p = sub.add_parser("fixtures", help="seed / list / tear down Stripe test fixtures")
    fx_p.add_argument("action", choices=["seed", "list", "teardown"])
    sub.add_parser("doctor", help="check every credential and connection before a run")

    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        from .doctor import format_report, run_doctor
        checks = run_doctor()
        print(format_report(checks))
        return 0 if all(c.ok for c in checks) else 1

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
    approver, banner, open_url = _make_approver(store, args.approver, args.port)
    approver.start()
    # Printed ONCE, and only here. The bare local URL 404s without the token, so
    # the operator needs the whole link — including when the surface is tunnelled
    # and the host part has to be swapped for the public one (scripts/tunnel.md).
    for line in banner:
        print(line)
    if open_url:
        webbrowser.open(open_url)
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
            # A fresh run id for the retry. The two passes ran on different
            # providers, so they are different configurations; sharing one id
            # makes a denial in pass 1 followed by an approval in pass 2 read
            # as the agent retrying after a refusal.
            activity.RUN_ID = uuid4().hex[:12]
            log_action("provider_switched", provider=state.provider, model=state.model)
            try:
                result = run_loop(client_from_env(state), guard, SYSTEM, USER_TASK)
            except Exception as retry_exc:  # noqa: BLE001 - the fallback failed too
                # Previously this raised outside the handler and crashed with a
                # traceback after the operator had already approved the switch.
                log_action("provider_transport_failure", provider=state.provider,
                           error=repr(retry_exc))
                print(f"\nThe fallback provider also failed: {retry_exc}")
                return 1
        print("\n" + result.text)
    finally:
        approver.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
