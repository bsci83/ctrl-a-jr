"""In-process driver for the api/ functions, shared by the other test_api_* files.

The handlers are exercised as real BaseHTTPRequestHandler subclasses over a
BytesIO socket, so the tests cover the same wiring Vercel loads — not a
hand-rolled shim that happens to agree with the route functions.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

# pyproject sets `pythonpath = ["src"]`, and pytest's prepend import mode inserts
# tests/ rather than the repo root, so `import api` fails without this. Derived
# from __file__ on purpose: in a git worktree an absolute or installed path would
# import the MAIN checkout's api/ and report a green suite over code this branch
# does not contain.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api._lib import routes  # noqa: E402
from api._lib.store import MemoryStore, Record  # noqa: E402

PUSH_TOKEN = "push-token-for-tests"
SIGNING_SECRET = "8f742231b10e4e3a9a0bc2f8bb0d7b3c"


def install(monkeypatch, store: MemoryStore | None = None) -> MemoryStore:
    """Point the routes at a memory store and set both secrets."""
    store = store if store is not None else MemoryStore()
    monkeypatch.setattr(routes, "STORE", store)
    monkeypatch.setenv("CTRLA_JR_PUSH_TOKEN", PUSH_TOKEN)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("CTRLA_JR_PUBLIC_BASE_URL", "https://ctrl-a-jr.vercel.app")
    return store


def seed(store: MemoryStore, approval_id: str = "ap_0123456789ab", **overrides) -> Record:
    fields = {
        "approval_id": approval_id,
        "tool": "gmail_send",
        "payload_hash": "a" * 64,
        "rendered_artifact": "<div class=\"art\">Invoice 123 is unpaid.</div>",
        "created_at": "2026-09-13T12:00:00+00:00",
    }
    fields.update(overrides)
    return store.put(Record(**fields))


def call(handler_cls, method: str, path: str, body: bytes = b"", headers: dict | None = None):
    """Drive one request through a real handler class. Returns (status, headers, body)."""
    headers = dict(headers or {})
    headers.setdefault("Host", "ctrl-a-jr.vercel.app")
    if body:
        headers.setdefault("Content-Length", str(len(body)))
    lines = [f"{method} {path} HTTP/1.0"]
    lines += [f"{key}: {value}" for key, value in headers.items()]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

    captured: dict[str, io.BytesIO] = {}

    class _Driven(handler_cls):
        def setup(self):
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            captured["wfile"] = self.wfile

        def finish(self):
            pass

    _Driven(None, ("127.0.0.1", 12345), None)
    return _parse(captured["wfile"].getvalue())


def _parse(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    parsed = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return status, parsed, body


def load_route_module(relative_path: str, name: str):
    """Import one of the `[id].py` shims, whose filename is not an identifier."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_package_is_this_worktree():
    """A green suite over the MAIN checkout's code would be worse than a red one."""
    import api._lib.routes as loaded

    assert Path(loaded.__file__).resolve().is_relative_to(ROOT)


def test_agent_package_is_this_worktree():
    import ctrl_a_jr

    assert Path(ctrl_a_jr.__file__).resolve().is_relative_to(ROOT)


def test_every_route_shim_exposes_a_handler():
    """The shims are what Vercel loads; an ImportError in one is a 500, not a test failure."""
    shims = [
        ("api/approvals/index.py", "shim_push"),
        ("api/approvals/[id].py", "shim_poll"),
        ("api/approve/[id].py", "shim_approve"),
        ("api/decide.py", "shim_decide"),
        ("api/slack/interactive.py", "shim_slack"),
    ]
    for relative_path, name in shims:
        module = load_route_module(relative_path, name)
        assert hasattr(module, "handler"), relative_path
