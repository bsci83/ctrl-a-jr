"""GET /api/approve/<id>?k=… — the page a human decides from."""

from __future__ import annotations

import os
import sys

# See api/approvals/index.py: the project root is not guaranteed on sys.path.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from api._lib.http import make_handler  # noqa: E402
from api._lib.routes import approve_page  # noqa: E402

handler = make_handler(approve_page)
