"""The approval surface: one local page, stdlib only.

The approver must see the ARTIFACT, not the arguments (spec §5 P2). Approving
a JSON blob is not approval, so the page renders the finished email and
escapes it — the body is attacker-influenced content from a customer thread.
"""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from .approval import ApprovalStore
from .types import ApprovalRecord, Decision

_STYLE = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#faf9f7;color:#1a1a1a}
main{max-width:720px;margin:0 auto;padding:32px 20px}
h1{font-size:18px;margin:0 0 24px}
.card{background:#fff;border:1px solid #e3e0db;border-radius:10px;padding:20px;margin-bottom:16px}
.tool{font:12px ui-monospace,monospace;color:#8a6d3b;background:#fdf6e3;
      padding:2px 8px;border-radius:20px;display:inline-block;margin-bottom:12px}
pre{white-space:pre-wrap;word-wrap:break-word;background:#f6f5f3;padding:14px;
    border-radius:6px;margin:0 0 16px;font:13px/1.55 ui-monospace,monospace}
button{font:14px system-ui;padding:9px 20px;border-radius:6px;border:0;
       cursor:pointer;margin-right:8px}
.ok{background:#1a7f37;color:#fff}.no{background:#cf222e;color:#fff}
.empty{color:#6b6b6b;text-align:center;padding:48px}
"""


def render_page(records: list[ApprovalRecord]) -> str:
    if not records:
        body = '<p class="empty">Nothing waiting for you.</p>'
    else:
        cards = []
        for r in records:
            cards.append(
                f'<div class="card"><div class="tool">{html.escape(r.tool)}</div>'
                f"<pre>{html.escape(r.rendered)}</pre>"
                f'<form method="post" action="/resolve" style="display:inline">'
                f'<input type="hidden" name="id" value="{html.escape(r.id)}">'
                f'<button class="ok" name="decision" value="approved">Approve</button>'
                f'<button class="no" name="decision" value="denied">Deny</button>'
                f"</form></div>"
            )
        body = "".join(cards)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ctrl-a JR — approvals</title>"
        "<meta http-equiv='refresh' content='2'>"
        f"<style>{_STYLE}</style></head><body><main>"
        "<h1>Waiting for your authorization</h1>"
        f"{body}</main></body></html>"
    )


class WebApprover:
    """Blocks the agent until a human clicks. One operator, one decision at a time."""

    def __init__(self, store: ApprovalStore, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.store = store
        self.host, self.port = host, port
        self._decisions: dict[str, Decision] = {}
        self._event = threading.Event()
        self._httpd: HTTPServer | None = None
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        approver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console clean
                pass

            def do_GET(self):
                host = (self.headers.get("Host") or "").strip()
                if host not in (f"127.0.0.1:{approver.port}", f"localhost:{approver.port}"):
                    self.send_response(403)
                    self.end_headers()
                    return
                page = render_page(approver.store.pending()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self):
                host = (self.headers.get("Host") or "").strip()
                if host not in (f"127.0.0.1:{approver.port}", f"localhost:{approver.port}"):
                    # Drain the body before responding: closing the connection with
                    # unread bytes still in flight races the client's own write and
                    # intermittently resets the socket instead of delivering the 403.
                    length = int(self.headers.get("Content-Length", 0))
                    if length:
                        self.rfile.read(length)
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                if self.path.split("?")[0] != "/resolve":
                    self.send_response(404)
                    self.end_headers()
                    return
                approval_id = form.get("id", [""])[0]
                decision = form.get("decision", ["denied"])[0]
                approver._decisions[approval_id] = (
                    Decision.APPROVED if decision == "approved" else Decision.DENIED
                )
                approver._event.set()
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()

        self._httpd = HTTPServer((self.host, self.port), Handler)
        # When port=0 the OS picks one; record it so .url is correct.
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        # Release anyone blocked in decide() BEFORE tearing the server down, so a
        # waiting agent thread cannot be stranded on a decision that can no longer
        # arrive.
        self._stopped = True
        self._event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def decide(self, record: ApprovalRecord) -> Decision:
        while record.id not in self._decisions:
            if self._stopped:
                # The approval surface is gone. Nobody can say yes, so the answer
                # is no — an unanswerable request must never become an approval.
                return Decision.DENIED
            self._event.wait(timeout=0.25)
            self._event.clear()
        return self._decisions.pop(record.id)
