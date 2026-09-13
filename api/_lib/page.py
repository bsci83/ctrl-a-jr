"""The human-facing approval page.

Visual language copied from the local server rather than imported: functions in
api/ ship without `src/` on the path. The duplication is CSS only — the markup
that carries a customer's words through this page comes out of
`sanitize.sanitize_artifact`, never out of a template string.
"""

from __future__ import annotations

import html
import os

from .sanitize import sanitize_artifact
from .store import Record

_STYLE = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#faf9f7;color:#1a1a1a}
main{max-width:720px;margin:0 auto;padding:32px 20px}
h1{font-size:18px;margin:0 0 24px}
.card{background:#fff;border:1px solid #e3e0db;border-radius:10px;padding:20px;margin-bottom:16px}
.tool{font:12px ui-monospace,monospace;color:#8a6d3b;background:#fdf6e3;
      padding:2px 8px;border-radius:20px;display:inline-block;margin-bottom:12px}
button{font:14px system-ui;padding:9px 20px;border-radius:6px;border:0;
       cursor:pointer;margin-right:8px}
.ok{background:#1a7f37;color:#fff}.no{background:#cf222e;color:#fff}
.meta{font:12px ui-monospace,monospace;color:#9a9a9a;margin:14px 0 0}
.done{font:14px system-ui;padding:12px 14px;border-radius:8px;margin:0 0 4px}
.done.approved{background:#eaf5ec;color:#1a7f37;border:1px solid #c6e3cd}
.done.denied{background:#fff1f0;color:#cf222e;border:1px solid #f5c2c0}
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


def _shell(title: str, inner: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style></head><body><main>{inner}</main></body></html>"
    )


def _console_link() -> str:
    """A way back.

    The approval page lives on a different origin from the console, so deciding
    something navigated the operator off the product with no route back — a dead
    end found while rehearsing the demo. The console keeps polling and picks the
    decision up on its own; this link just returns you to it.
    """
    url = (os.environ.get("CTRLA_JR_CONSOLE_URL") or "").strip()
    if not url.startswith("https://"):
        return ""
    return (
        f'<p class="meta"><a href="{html.escape(url, quote=True)}" '
        'style="color:#1a7f37;font-weight:600;text-decoration:none">'
        "← Back to the console</a> — it has already picked this up.</p>"
    )


def _decided_note(rec: Record) -> str:
    who = html.escape(rec.decided_by or "unknown")
    when = html.escape(rec.decided_at or "")
    return (
        f'<div class="done {html.escape(rec.decision)}">'
        f"This was already <b>{html.escape(rec.decision)}</b> by {who}"
        f"{f' at {when}' if when else ''}.</div>"
        "<p class=\"meta\">A decision is final. Re-opening this link cannot change it.</p>"
    )


def render_approval(rec: Record, nonce: str) -> str:
    """The pending artifact with its two buttons, or the decision already made."""
    artifact = sanitize_artifact(rec.rendered_artifact)
    meta = (
        f'<p class="meta">{html.escape(rec.approval_id)} · '
        f"payload {html.escape(rec.payload_hash[:16])}… · "
        f"created {html.escape(rec.created_at)}</p>"
    )
    if rec.decision != "pending":
        inner = (
            "<h1>ctrl-a JR</h1>"
            f'<div class="card"><div class="tool">{html.escape(rec.tool)}</div>'
            f"{_decided_note(rec)}{artifact}{meta}{_console_link()}</div>"
        )
        return _shell("ctrl-a JR — decided", inner)

    form = (
        '<form method="post" action="/api/decide">'
        f'<input type="hidden" name="id" value="{html.escape(rec.approval_id, quote=True)}">'
        f'<input type="hidden" name="k" value="{html.escape(nonce, quote=True)}">'
        '<button class="ok" name="decision" value="approved">Approve</button>'
        '<button class="no" name="decision" value="denied">Deny</button>'
        "</form>"
    )
    inner = (
        "<h1>Waiting for your authorization</h1>"
        f'<div class="card"><div class="tool">{html.escape(rec.tool)}</div>'
        f"{artifact}{form}{meta}</div>"
    )
    return _shell("ctrl-a JR — approve", inner)
