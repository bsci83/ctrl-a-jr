"""Typed renderers for the things the agent proposes to do.

The canvas idea, borrowed from ctrl-a, with its mechanism deliberately inverted.

ctrl-a's `canvas_render_code` takes `html` straight from the model — the agent
authors the markup. That cannot work here. If the model writes the HTML that
shows you an email, what you see is no longer DERIVED from what gets sent, and a
model could render one message and send another with the approval record still
looking clean. It is the same attack the payload hash closes at the execution
layer, reopened at the display layer. (Model-authored HTML with inline JS is also
an XSS vector into the approval page itself.)

So renderers are code, keyed by tool name, deriving the display deterministically
from the exact arguments that will execute. Unknown tools fall back to a plain
key/value table rather than to anything clever.

Every value is escaped. Some of these fields carry text quoted from a customer's
own email.
"""

from __future__ import annotations

import html
from collections.abc import Callable

STYLE = """
.art{border:1px solid #e3e0db;border-radius:8px;overflow:hidden;margin:0 0 16px;background:#fff}
.art-h{background:#f6f5f3;padding:10px 14px;border-bottom:1px solid #e3e0db;
       font:12px ui-monospace,monospace;color:#6b6b6b;display:flex;gap:10px;align-items:center}
.art-h b{color:#1a1a1a;font-weight:600}
.art-b{padding:14px}
.art-row{display:flex;gap:10px;padding:3px 0;font:13px system-ui}
.art-row span:first-child{color:#8a8a8a;min-width:72px;flex:0 0 72px}
.art-body{white-space:pre-wrap;word-wrap:break-word;margin-top:12px;padding-top:12px;
          border-top:1px solid #eeebe6;font:14px/1.55 system-ui;color:#1a1a1a}
.art-amt{font:22px/1.2 system-ui;font-weight:650;color:#1a1a1a;margin:2px 0 8px}
.art-warn{font:12px system-ui;color:#8a6d3b;background:#fdf6e3;padding:8px 12px;
          border-top:1px solid #f0e0b8}
.slack-ch{font:13px system-ui;font-weight:600;color:#1a1a1a}
.slack-msg{font:14px/1.55 system-ui;white-space:pre-wrap;margin-top:6px}
"""


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v))


def _rows(pairs: list[tuple[str, object]]) -> str:
    return "".join(
        f'<div class="art-row"><span>{_esc(k)}</span><span>{_esc(v)}</span></div>'
        for k, v in pairs if v not in (None, "")
    )


def _email(a: dict) -> str:
    return (
        '<div class="art"><div class="art-h"><b>Email</b> will be sent as you</div>'
        f'<div class="art-b">{_rows([("To", a.get("to")), ("Subject", a.get("subject"))])}'
        f'<div class="art-body">{_esc(a.get("body"))}</div></div>'
        '<div class="art-warn">This leaves your account and reaches a real customer.</div></div>'
    )


def _slack(a: dict) -> str:
    return (
        '<div class="art"><div class="art-h"><b>Slack</b> message</div>'
        f'<div class="art-b"><div class="slack-ch">{_esc(a.get("channel"))}</div>'
        f'<div class="slack-msg">{_esc(a.get("text"))}</div></div></div>'
    )


def _invoice(a: dict) -> str:
    return (
        '<div class="art"><div class="art-h"><b>Stripe</b> will email this invoice</div>'
        f'<div class="art-b">{_rows([("Invoice", a.get("invoice_id"))])}</div>'
        '<div class="art-warn">Stripe sends this from your account, with a payment link.</div></div>'
    )


def _report(a: dict) -> str:
    return (
        '<div class="art"><div class="art-h"><b>Report</b> written to disk</div>'
        f'<div class="art-b">{_rows([("File", a.get("filename"))])}'
        f'<div class="art-body">{_esc(a.get("content"))}</div></div></div>'
    )


def _provider(a: dict) -> str:
    return (
        '<div class="art"><div class="art-h"><b>Switch model</b></div>'
        f'<div class="art-b">{_rows([("Provider", a.get("provider")), ("Model", a.get("model")),
                                     ("Reason", a.get("reason"))])}</div>'
        '<div class="art-warn">Approving changes which model produced the rest of this run.</div></div>'
    )


def _lookup(a: dict) -> str:
    """Read-only calls.

    These never reach the gate, so they had no renderer — the approval page only
    ever renders things that stop for a human. The evidence page shows a card per
    tool call, and the plain-table fallback there loses the one fact a reader
    needs about a read: nothing left the building. Adding these is inert for the
    gate, because a non-mutating tool never produces an approval record.
    """
    return (
        '<div class="art"><div class="art-h"><b>Read-only</b> lookup — '
        'nothing leaves your account</div>'
        f'<div class="art-b">{_rows(sorted(a.items()))}</div></div>'
    )


RENDERERS: dict[str, Callable[[dict], str]] = {
    "stripe_list_failed_payments": _lookup,
    "stripe_get_customer": _lookup,
    "stripe_get_invoice": _lookup,
    "gmail_search_threads": _lookup,
    "gmail_read_thread": _lookup,
    "slack_lookup_user": _lookup,
    "gmail_send": _email,
    "slack_post_message": _slack,
    "stripe_send_invoice": _invoice,
    "write_report": _report,
    "provider_switch": _provider,
}


def render_artifact(tool: str, args: dict) -> str:
    """Render what this call will actually do. Falls back to a plain table."""
    fn = RENDERERS.get(tool)
    if fn is not None:
        return fn(args)
    return (
        f'<div class="art"><div class="art-h"><b>{_esc(tool)}</b></div>'
        f'<div class="art-b">{_rows(sorted(args.items()))}</div></div>'
    )
