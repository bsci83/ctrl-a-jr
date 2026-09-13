"""Gmail over stdlib smtplib/imaplib with a Google App Password.

No OAuth, no broker, no third-party grant — which is what makes the
local-first claim in the README unqualified. A pre-flight against Composio on
2026-09-09 found 7 of 9 grants expired, including the only Slack one; see
docs/design §8.
"""

from __future__ import annotations

import email
import imaplib
import json
import re
import smtplib
from email.message import EmailMessage
from typing import Any

from ..registry import Registry, ToolSpec
from ..types import ToolResult

# imaplib concatenates command arguments raw — `data = data + b' ' + arg` in
# IMAP4._command, with no CRLF filtering — then sends the line terminated by
# CRLF. So a line break inside an argument is not escaped: it terminates
# the current command and begins a new one. That matters more here than
# anywhere else in this codebase: the
# search/read tools are classified read-only and therefore BYPASS the approval
# gate, and their arguments come from a model that has just been fed up to 4000
# characters of customer-authored email. Injected text -> new IMAP commands ->
# STORE +FLAGS (\Deleted) / EXPUNGE / APPEND, ungated and unlogged.
#
# Validate before imaplib sees it. Refuse rather than escape: these two fields
# have narrow, checkable shapes, and a refusal is a tool error the model can
# read, not a silent mangling.
_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}$")
_UID_RE = re.compile(r"^[0-9]{1,20}$")


def _safe_address(value: object) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value):
        raise ValueError(
            "refusing to search: from_address must be a plain email address. "
            "Control characters and IMAP syntax are not accepted."
        )
    return value


def normalize_app_password(value: str) -> str:
    """Strip whitespace from a Google App Password.

    Google displays it as four groups of four ("abcd efgh ijkl mnop") and users
    paste what they see. The credential is 16 characters; the spaces are
    presentation. smtplib/imaplib send the string verbatim, so an unstripped
    paste is a confusing "Username and Password not accepted" that looks like a
    wrong password rather than a formatting problem.
    """
    return "".join(value.split())


def _safe_uid(value: object) -> str:
    if not isinstance(value, str) or not _UID_RE.match(value):
        raise ValueError("refusing to fetch: uid must be digits only.")
    return value


class _SMTP:
    def __init__(self, address: str, app_password: str) -> None:
        self.address = address
        self.app_password = normalize_app_password(app_password)

    def send(self, from_addr: str, to_addr: str, message_bytes: bytes) -> None:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(self.address, self.app_password)
            s.sendmail(from_addr, [to_addr], message_bytes)


class _IMAP:
    def __init__(self, address: str, app_password: str) -> None:
        self.address = address
        self.app_password = normalize_app_password(app_password)

    def _conn(self):
        c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            c.login(self.address, self.app_password)
            # readonly=True is load-bearing, not tidiness: this tool is registered
            # mutating=False and therefore never reaches the approval gate, so it must
            # genuinely not change state. A read-write SELECT lets FETCH set \Seen and
            # silently marks the customer's mail as read.
            c.select("INBOX", readonly=True)
        except Exception:
            try:
                c.logout()
            # Deliberate: we are already unwinding a real failure. A logout error
            # here must not mask the original exception, which is re-raised below.
            except Exception:  # noqa: BLE001, S110
                pass
            raise
        return c

    def search(self, from_address: str, limit: int) -> list[dict]:
        c = self._conn()
        try:
            _typ, data = c.search(None, "FROM", _safe_address(from_address))
            uids = data[0].split()[-limit:] if data and data[0] else []
            return [{"uid": u.decode()} for u in uids]
        finally:
            c.logout()

    def recent(self, limit: int) -> list[dict]:
        """The most recent messages in the inbox, newest last.

        `search` needs a sender, which is right for "what did THIS customer
        already say" and useless for "what is waiting for me". A deployed agent
        you can talk to has to be able to look at its own inbox; without this it
        answers "I do not have an inbox address to search against", which is
        what the first live cloud run actually did.

        No caller-supplied value reaches the IMAP command line here — the search
        key is the literal ALL — so there is nothing to inject. `limit` is
        clamped to an int and applied in Python.
        """
        c = self._conn()
        try:
            _typ, data = c.search(None, "ALL")
            uids = data[0].split()[-max(1, min(int(limit), 25)):] if data and data[0] else []
            out = []
            for u in uids:
                # Headers only: the body is a separate, deliberate read.
                _t, d = c.fetch(u, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                head = email.message_from_bytes(d[0][1]) if d and d[0] else None
                out.append({
                    "uid": u.decode(),
                    "from": (head.get("From", "") if head else ""),
                    "subject": (head.get("Subject", "") if head else ""),
                    "date": (head.get("Date", "") if head else ""),
                })
            return out
        finally:
            c.logout()

    def fetch(self, uid: str) -> dict:
        c = self._conn()
        try:
            # BODY.PEEK[] never sets \Seen, belt-and-braces with readonly above.
            _typ, data = c.fetch(_safe_uid(uid).encode(), "(BODY.PEEK[])")
            msg = email.message_from_bytes(data[0][1])
            if msg.is_multipart():
                body = "".join(
                    p.get_payload(decode=True).decode(errors="replace")
                    for p in msg.walk() if p.get_content_type() == "text/plain"
                )
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")
            return {"uid": uid, "subject": msg.get("Subject", ""), "from": msg.get("From", ""),
                    "body": body[:4000]}
        finally:
            c.logout()


class GmailClient:
    def __init__(self, address: str, app_password: str,
                 smtp: Any | None = None, imap: Any | None = None) -> None:
        self.address = address
        self.smtp = smtp or _SMTP(address, app_password)
        self.imap = imap or _IMAP(address, app_password)

    def search_threads(self, from_address: str, limit: int = 5) -> list[dict]:
        # Validated HERE, not only inside _IMAP: a fake or alternate transport
        # would otherwise skip the guard entirely. This is the layer the tool
        # calls, so it is the layer that must hold.
        return self.imap.search(_safe_address(from_address), limit)

    def list_recent(self, limit: int = 10) -> list[dict]:
        return self.imap.recent(limit)

    def read_thread(self, uid: str) -> dict:
        return self.imap.fetch(_safe_uid(uid))

    def send(self, to: str, subject: str, body: str) -> dict:
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        self.smtp.send(self.address, to, msg.as_bytes())
        # Body is never returned or logged verbatim — length only.
        return {"sent": True, "to": to, "subject": subject, "body_chars": len(body)}


def register_gmail_tools(registry: Registry, client: GmailClient) -> None:
    def _wrap(fn):
        def inner(**kwargs):
            try:
                return ToolResult(True, json.dumps(fn(**kwargs), default=str))
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, "", str(exc))
        return inner

    registry.register(ToolSpec(
        name="gmail_search_threads",
        description="Find recent email from a customer's address. Use this before drafting so you "
                    "know what they have already said. Pass a bare email address, not a search query. "
                    "Do not guess their history.",
        schema={"type": "object",
                "properties": {"from_address": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["from_address"]},
        mutating=False,
        run=_wrap(lambda from_address, limit=5: client.search_threads(from_address, limit)),
    ))
    registry.register(ToolSpec(
        name="gmail_list_inbox",
        description="List what is waiting in the shop inbox: sender, subject and date for the "
                    "most recent messages. Start here when you are asked what needs handling. "
                    "Read-only.",
        schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        mutating=False,
        run=_wrap(lambda limit=10: client.list_recent(limit)),
    ))
    registry.register(ToolSpec(
        name="gmail_read_thread",
        description="Read one message by uid, returned by gmail_search_threads.",
        schema={"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]},
        mutating=False,
        run=_wrap(lambda uid: client.read_thread(uid)),
    ))
    registry.register(ToolSpec(
        name="gmail_send",
        description="Send an email to the customer. Every send is reviewed by a human "
                    "before it leaves, so write the finished message, not a draft note.",
        schema={"type": "object",
                "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                               "body": {"type": "string"}},
                "required": ["to", "subject", "body"]},
        mutating=True,
        run=_wrap(lambda to, subject, body: client.send(to, subject, body)),
        render=lambda to, subject, body: f"To: {to}\nSubject: {subject}\n\n{body}",
    ))
