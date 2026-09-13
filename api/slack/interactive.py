"""POST /api/slack/interactive — Slack Block Kit button clicks land here.

Paste this URL into api.slack.com/apps → Interactivity & Shortcuts → Request URL.
"""

from __future__ import annotations

import os
import sys

# See api/approvals/index.py: the project root is not guaranteed on sys.path.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from api._lib.http import make_handler  # noqa: E402
from api._lib.routes import slack_interactive  # noqa: E402

handler = make_handler(slack_interactive)
