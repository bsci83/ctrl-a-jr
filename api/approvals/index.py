"""POST /api/approvals — the local agent pushes a pending approval."""

from __future__ import annotations

import os
import sys

# Vercel's Python runtime does not guarantee the project root on sys.path, and a
# bare `from api._lib ...` then fails at cold start with an ImportError that
# surfaces only as a 500.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from api._lib.http import make_handler  # noqa: E402
from api._lib.routes import push  # noqa: E402

handler = make_handler(push)
