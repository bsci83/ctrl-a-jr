"""Approval over a deployed surface, without opening a port.

The local agent holds every credential in the product — the Stripe key, the
Gmail app password, the Slack bot token. So it never accepts an inbound
connection. It PUSHES the pending approval to the deployed API and POLLS for
the decision. Everything is outbound; there is no tunnel and no listening
socket to find.

The one rule this file exists to enforce: `decide` returns APPROVED only when
the API has told us, in a well-formed response, that a human said yes. Every
other outcome — unreachable server, timeout, 500, a status string we do not
recognise, a shutdown mid-wait — is DENIED. An approver that says yes when it
cannot reach the operator is the worst bug this codebase could have, because
it fails OPEN at the exact moment nobody is watching.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .activity import log_action
from .artifacts import render_artifact
from .types import ApprovalRecord, Decision

# Ten minutes: long enough for an operator to reach a phone, short enough that a
# forgotten run does not hold a thread open all night. Bounded either way — an
# unbounded wait is a hang, and a hung agent is not a safe agent.
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 2.0
MAX_POLL_S = 15.0
PUSH_TIMEOUT_S = 20.0
POLL_HTTP_TIMEOUT_S = 20.0

# Accepted decision strings. Anything else is not "probably approved"; it is a
# response we do not understand, and we do not act on responses we do not
# understand.
_APPROVED = "approved"
_DENIED = "denied"
_PENDING = "pending"


class _Httpx:
    """The real transport. Returns (status, parsed_body) rather than raising on
    a non-2xx, because a 500 and a 200 are both answers `decide` must classify
    itself — raise_for_status here would collapse them into one opaque error."""

    def request(self, method: str, url: str, headers: dict | None = None,
                json: dict | None = None, timeout: float = PUSH_TIMEOUT_S) -> tuple[int, Any]:
        r = httpx.request(method, url, headers=headers, json=json, timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            # A body that is not JSON (an HTML error page from the edge, say) is
            # not a decision. Hand back None and let the caller deny.
            return r.status_code, None


class RemoteApprover:
    """Satisfies the same `Approver` protocol as `WebApprover`: one method,
    `decide`, blocking until a human answers or the wait is over."""

    def __init__(
        self,
        api_base: str,
        push_token: str,
        http: Any | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_S,
        max_poll_interval_s: float = MAX_POLL_S,
        slack: Any | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        if not isinstance(api_base, str) or not api_base.strip():
            raise ValueError(
                "a deployed approval API base URL is required: set "
                "CTRLA_JR_APPROVAL_API (e.g. https://your-app.vercel.app)"
            )
        if not isinstance(push_token, str) or not push_token.strip():
            raise ValueError(
                "a push token is required: set CTRLA_JR_PUSH_TOKEN to the same "
                "secret the deployed API holds. Without it the API cannot tell "
                "your agent from anyone else's."
            )
        self.api_base = api_base.strip().rstrip("/")
        self._token = push_token.strip()
        self.http = http or _Httpx()
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.slack = slack
        self._monotonic = monotonic
        # An Event, not a flag plus sleep(): stop() has to interrupt a thread
        # that is mid-wait, otherwise Ctrl-C parks for a whole poll interval.
        self._stop = threading.Event()

    # Parity with WebApprover so cli.py can hold either without special-casing.
    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._stop.set()

    @property
    def url(self) -> str:
        return f"{self.api_base}/"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _deny(self, record: ApprovalRecord, reason: str, **fields: object) -> Decision:
        """Every refusal path lands here, so no refusal can be silent.

        The event name for a machine denial matches WebApprover's
        `approval_auto_denied` on purpose: the eval harness distinguishes a human
        saying no from the surface being unable to ask, and a second event name
        would make remote runs read as human denials nobody performed.
        """
        log_action("approval_auto_denied", approval_id=record.id, tool=record.tool,
                   reason=reason, approver="remote", **fields)
        return Decision.DENIED

    def decide(self, record: ApprovalRecord) -> Decision:
        if self._stop.is_set():
            return self._deny(record, "approver_stopped")

        try:
            self._push(record)
        except Exception as exc:  # noqa: BLE001 - unreachable is a denial, not a crash
            return self._deny(record, "push_failed", error=repr(exc))

        # Posting the Slack card is convenience, not authorization: the decision
        # is read from the API either way. A Slack outage must not deny a request
        # the operator can still answer on the web surface.
        self._notify_slack(record)

        deadline = self._monotonic() + self.timeout_s
        interval = self.poll_interval_s
        while True:
            if self._stop.is_set():
                # The run is ending with a request still open. Nobody can say
                # yes, so the answer is no.
                return self._deny(record, "approver_stopped")
            if self._monotonic() >= deadline:
                return self._deny(record, "approval_timeout",
                                  waited_s=round(self.timeout_s, 3))

            try:
                status, body, decided_by = self._poll(record)
            except Exception as exc:  # noqa: BLE001
                return self._deny(record, "poll_failed", error=repr(exc))

            if status == _APPROVED:
                echoed = body.get("payload_hash") if isinstance(body, dict) else None
                if isinstance(echoed, str) and echoed != record.payload_hash:
                    # An approval for a DIFFERENT payload is not an approval for
                    # this one. Checked only when the API echoes the hash, so this
                    # can tighten but never loosen; the guard's own re-verification
                    # at execution time is the second line, not the only one.
                    return self._deny(record, "payload_hash_mismatch",
                                      echoed_hash=echoed)
                log_action("approval_remote_decided", approval_id=record.id,
                           tool=record.tool, decision=_APPROVED,
                           decided_by=decided_by, approver="remote",
                           payload_hash=record.payload_hash)
                return Decision.APPROVED
            if status == _DENIED:
                log_action("approval_remote_decided", approval_id=record.id,
                           tool=record.tool, decision=_DENIED,
                           decided_by=decided_by, approver="remote",
                           payload_hash=record.payload_hash)
                return Decision.DENIED
            if status != _PENDING:
                # Not "keep waiting and hope". A status we cannot name may mean
                # expired, revoked, or a different API entirely on this URL.
                return self._deny(record, "unrecognised_status",
                                  status=repr(status), body_type=type(body).__name__)

            # Interruptible sleep, with backoff so a ten-minute wait is not ~300
            # requests against a serverless function.
            self._stop.wait(interval)
            interval = min(interval * 1.5, self.max_poll_interval_s)

    def _push(self, record: ApprovalRecord) -> None:
        payload = {
            "approval_id": record.id,
            "tool": record.tool,
            "payload_hash": record.payload_hash,
            # The operator must approve the ARTIFACT, not the arguments — the same
            # rule as the local page. render_artifact derives the display from the
            # exact args that will execute and escapes every value in them.
            "artifact": render_artifact(record.tool, record.args),
            "rendered": record.rendered,
            "created_at": datetime.now(UTC).isoformat(),
        }
        # The token travels in the header only. In the body it would be echoed
        # back by any surface that renders the pending approval.
        status, body = self.http.request(
            "POST", f"{self.api_base}/api/approvals",
            headers=self._headers(), json=payload, timeout=PUSH_TIMEOUT_S,
        )
        if not (200 <= int(status) < 300):
            raise RuntimeError(f"push rejected with HTTP {status}: {str(body)[:200]}")
        log_action("approval_pushed", approval_id=record.id, tool=record.tool,
                   payload_hash=record.payload_hash, approver="remote",
                   api_base=self.api_base)

    def _poll(self, record: ApprovalRecord) -> tuple[str | None, Any, str | None]:
        status_code, body = self.http.request(
            "GET", f"{self.api_base}/api/approvals/{record.id}",
            headers=self._headers(), timeout=POLL_HTTP_TIMEOUT_S,
        )
        if not (200 <= int(status_code) < 300):
            raise RuntimeError(f"HTTP {status_code} polling approval {record.id}")
        if not isinstance(body, dict):
            # A list, a string or a missing body is not a decision document.
            return None, body, None
        decision = body.get("status", body.get("decision"))
        decided_by = body.get("decided_by")
        if not isinstance(decision, str):
            return None, body, None
        return decision.strip().lower(), body, (decided_by if isinstance(decided_by, str) else None)

    def _notify_slack(self, record: ApprovalRecord) -> None:
        if self.slack is None:
            return
        try:
            self.slack.post_approval_request(record.id, record.tool, record.rendered)
        except Exception as exc:  # noqa: BLE001 - a card that did not post is not a denial
            log_action("approval_card_failed", approval_id=record.id, tool=record.tool,
                       error=repr(exc), approver="remote")
