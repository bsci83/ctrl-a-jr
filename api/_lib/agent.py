"""Server-side construction of the agent: registry, guard, model client.

`cli._build` does this for a terminal. A conversational HTTP entrypoint needs
the SAME construction, and the failure this file exists to prevent is the two
drifting: a tool registered in one and not the other means a run started from
Slack has a different capability set from a run started from the terminal, and
nothing in the type system would say so. The prompt and the tool registrations
are therefore imported from the agent package, never copied.

Credential handling differs from the CLI's in one deliberate way. `cli._require`
raises SystemExit, which on a serverless function is an opaque 500 with no
message. Here a missing variable raises `MissingCredential` naming EVERY
variable that is unset, and it is raised BEFORE any client is constructed — a
half-built registry is worse than no registry, because the agent would then
quietly lack a tool and report that it could not do the job rather than that the
deploy is misconfigured.

No value of any environment variable is ever put in an exception message. The
names are the diagnostic; the values are secrets.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Vercel's Python builder puts the project root on sys.path (api/index.py makes
# sure of it) but not `src/`. Without this, `import ctrl_a_jr` is an ImportError
# at cold start, surfacing as a 500 with no clue which import failed.
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ctrl_a_jr.approval import ApprovalStore  # noqa: E402
from ctrl_a_jr.cli import SYSTEM  # noqa: E402
from ctrl_a_jr.guard import Guard  # noqa: E402
from ctrl_a_jr.loop import MAX_ROUNDS  # noqa: E402
from ctrl_a_jr.providers import (  # noqa: E402
    ProviderState,
    client_from_env,
    register_provider_tools,
)
from ctrl_a_jr.registry import Registry  # noqa: E402
from ctrl_a_jr.resume import SuspendingApprover  # noqa: E402
from ctrl_a_jr.tools.gmail_tools import GmailClient, register_gmail_tools  # noqa: E402
from ctrl_a_jr.tools.quote_tools import register_quote_tools  # noqa: E402
from ctrl_a_jr.tools.report_tools import register_report_tools  # noqa: E402
from ctrl_a_jr.tools.slack_tools import SlackClient, register_slack_tools  # noqa: E402
from ctrl_a_jr.tools.stripe_tools import StripeClient, register_stripe_tools  # noqa: E402

OUT_DIR_ENV = "CTRLA_JR_OUT_DIR"

# Everything a run needs before it may exist at all. `write_report` writes to
# disk and `provider_switch` needs a model key, so there is no useful subset.
REQUIRED = (
    "STRIPE_SECRET_KEY",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "SLACK_BOT_TOKEN",
    "CTRLA_JR_SLACK_CHANNEL",
)


class MissingCredential(RuntimeError):
    """One or more required environment variables are unset.

    Carries the NAMES only. A message containing a value would end up in a
    response body and in the function log.
    """


@dataclass(frozen=True)
class AgentBuild:
    """Everything an invocation needs to drive one run forward."""

    registry: Registry
    guard: Guard
    client: Any
    slack: SlackClient
    system: str
    max_rounds: int = MAX_ROUNDS


def _value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def model_key_env(provider: str) -> str:
    """Which key this provider reads. Mirrors `providers.client_from_env`."""
    return "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"


def _provider_state() -> ProviderState:
    return ProviderState(
        _value("CTRLA_JR_PROVIDER") or "minimax",
        _value("CTRLA_JR_MODEL") or "MiniMax-M3",
    )


def _out_dir() -> Path:
    """Where `write_report` may write.

    A serverless filesystem is read-only apart from the temp directory, so a
    default of ./out would make an APPROVED report write fail — the human would
    have authorised something that then could not happen.
    """
    configured = _value(OUT_DIR_ENV)
    root = Path(configured) if configured else Path(tempfile.gettempdir()) / "ctrl-a-jr-out"
    root.mkdir(parents=True, exist_ok=True)
    return root


def missing_credentials() -> list[str]:
    """Every required variable that is unset, in a stable order."""
    state = _provider_state()
    names = [*REQUIRED, model_key_env(state.provider)]
    return [name for name in names if not _value(name)]


def build_agent() -> AgentBuild:
    """Registry + guard + model client, or a MissingCredential naming the gaps.

    The credential sweep happens FIRST and covers every variable, so this
    function either returns a complete agent or constructs nothing at all. The
    alternative — construct until something throws — leaves a registry holding
    some tools and not others, and a run on it reports "I could not find the
    invoice" when the truth is "STRIPE_SECRET_KEY is unset".
    """
    gaps = missing_credentials()
    if gaps:
        raise MissingCredential(
            "missing required environment variable" + ("s" if len(gaps) > 1 else "")
            + ": " + ", ".join(gaps)
        )

    state = _provider_state()
    registry = Registry()
    register_stripe_tools(registry, StripeClient(_value("STRIPE_SECRET_KEY")))
    register_gmail_tools(
        registry, GmailClient(_value("GMAIL_ADDRESS"), _value("GMAIL_APP_PASSWORD"))
    )
    slack = SlackClient(_value("SLACK_BOT_TOKEN"), _value("CTRLA_JR_SLACK_CHANNEL"))
    register_slack_tools(registry, slack)
    register_quote_tools(registry)
    register_report_tools(registry, _out_dir())
    register_provider_tools(registry, state)

    try:
        client = client_from_env(state)
    except SystemExit as exc:  # noqa: BLE001 - providers.py signals a missing key this way
        # Unreachable while `missing_credentials` covers the same variable, and
        # kept anyway: a SystemExit escaping a WSGI handler kills the worker
        # rather than returning a response.
        raise MissingCredential(str(exc)) from None

    # The approver is the suspending one on purpose. `resume.advance` ignores
    # whatever approver it is handed and builds its own, but if any future code
    # dispatched through THIS guard directly, a blocking approver would hang the
    # function until the platform timeout — and whether the tool ran would then
    # depend on where that timeout landed. This one fails closed instantly.
    guard = Guard(registry, ApprovalStore(), SuspendingApprover())
    return AgentBuild(
        registry=registry, guard=guard, client=client, slack=slack,
        system=SYSTEM, max_rounds=MAX_ROUNDS,
    )
