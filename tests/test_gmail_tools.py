import pytest

from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import gmail_tools


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send(self, from_addr, to_addr, message_bytes):
        self.sent.append((from_addr, to_addr, message_bytes))


class FakeIMAP:
    def __init__(self, results=None, bodies=None):
        self.results = results or []
        self.bodies = bodies or {}

    def search(self, from_address, limit):
        return self.results[:limit]

    def fetch(self, uid):
        return self.bodies[uid]


def _client(smtp=None, imap=None):
    return gmail_tools.GmailClient("me@example.com", "app-pw",
                                   smtp=smtp or FakeSMTP(), imap=imap or FakeIMAP())


def test_send_builds_a_well_formed_message():
    smtp = FakeSMTP()
    _client(smtp=smtp).send("a@b.co", "Invoice overdue", "Please pay.")
    _frm, to, raw = smtp.sent[0]
    text = raw.decode()
    assert to == "a@b.co"
    assert "Subject: Invoice overdue" in text
    assert "Please pay." in text


def test_search_threads_respects_limit():
    imap = FakeIMAP(results=[{"uid": "1"}, {"uid": "2"}, {"uid": "3"}])
    assert len(_client(imap=imap).search_threads("a@b.co", limit=2)) == 2


def test_read_thread_returns_body():
    imap = FakeIMAP(bodies={"1": {"uid": "1", "subject": "Re: invoice", "body": "will pay friday"}})
    assert "friday" in _client(imap=imap).read_thread("1")["body"]


def test_registers_two_read_tools_and_one_mutating():
    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client())
    assert reg.mutating_names() == ["gmail_send"]
    assert "gmail_search_threads" in reg.names()
    assert "gmail_read_thread" in reg.names()


def test_send_renders_the_actual_message_for_approval():
    """P2: the approver sees the artifact, not the arguments."""
    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client())
    rendered = reg.get("gmail_send").render_for_approval(
        {"to": "a@b.co", "subject": "Overdue", "body": "Please pay $42.00."}
    )
    assert "a@b.co" in rendered
    assert "Overdue" in rendered
    assert "Please pay $42.00." in rendered


def test_smtp_failure_becomes_error_result():
    class Boom:
        def send(self, *a):
            raise RuntimeError("smtp auth failed")

    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client(smtp=Boom()))
    out = reg.get("gmail_send").run(to="a@b.co", subject="s", body="b")
    assert out.ok is False and "auth failed" in (out.error or "")


class _FakeConn:
    """Stands in for an imaplib connection so the real _IMAP logic is exercised."""

    def __init__(self, search_data=None, fetch_data=None):
        self.search_data = search_data
        self.fetch_data = fetch_data
        self.fetch_spec = None
        self.logged_out = False

    def search(self, charset, key, value):
        return "OK", self.search_data

    def fetch(self, uid, spec):
        self.fetch_spec = spec
        return "OK", self.fetch_data

    def logout(self):
        self.logged_out = True


class _ProbeIMAP(gmail_tools._IMAP):
    def __init__(self, conn):
        super().__init__("me@example.com", "pw")
        self._injected = conn

    def _conn(self):
        return self._injected


def test_imap_search_on_an_empty_mailbox_returns_no_uids():
    conn = _FakeConn(search_data=[b""])
    assert _ProbeIMAP(conn).search("a@b.co", limit=5) == []


def test_imap_search_handles_a_none_payload():
    conn = _FakeConn(search_data=[None])
    assert _ProbeIMAP(conn).search("a@b.co", limit=5) == []


def test_imap_search_returns_the_most_recent_up_to_limit():
    conn = _FakeConn(search_data=[b"1 2 3 4"])
    assert _ProbeIMAP(conn).search("a@b.co", limit=2) == [{"uid": "3"}, {"uid": "4"}]


def test_imap_fetch_uses_body_peek_so_mail_is_not_marked_read():
    """The whole reason gmail_read_thread may skip the approval gate."""
    raw = b"Subject: Re: invoice\r\nFrom: a@b.c\r\n\r\nwill pay friday\r\n"
    conn = _FakeConn(fetch_data=[(b"1", raw)])
    out = _ProbeIMAP(conn).fetch("1")
    assert conn.fetch_spec == "(BODY.PEEK[])"
    assert "friday" in out["body"]
    assert out["subject"] == "Re: invoice"
    assert conn.logged_out is True


def test_imap_selects_the_mailbox_readonly(monkeypatch):
    """A read-write SELECT lets FETCH set \\Seen — an ungated tool must not do that."""
    calls = {}

    class FakeIMAP4SSL:
        def __init__(self, host, port):
            pass

        def login(self, addr, pw):
            calls["login"] = True

        def select(self, mailbox, readonly=False):
            calls["mailbox"] = mailbox
            calls["readonly"] = readonly

        def logout(self):
            calls["logout"] = True

    monkeypatch.setattr(gmail_tools.imaplib, "IMAP4_SSL", FakeIMAP4SSL)
    gmail_tools._IMAP("me@example.com", "pw")._conn()
    assert calls["mailbox"] == "INBOX"
    assert calls["readonly"] is True


def test_imap_conn_closes_the_socket_if_login_fails(monkeypatch):
    closed = {"logout": False}

    class FailingIMAP4SSL:
        def __init__(self, host, port):
            pass

        def login(self, addr, pw):
            raise RuntimeError("bad app password")

        def logout(self):
            closed["logout"] = True

    monkeypatch.setattr(gmail_tools.imaplib, "IMAP4_SSL", FailingIMAP4SSL)
    with pytest.raises(RuntimeError):
        gmail_tools._IMAP("me@example.com", "pw")._conn()
    assert closed["logout"] is True


# ── IMAP argument validation ──────────────────────────────────────────────────
# imaplib concatenates command arguments raw and terminates the line with CRLF,
# so a line break inside an argument starts a new IMAP command. These two tools
# are classified read-only and therefore never reach the approval gate, and
# their arguments come from a model that was just fed customer-authored email.

def test_a_crlf_payload_in_from_address_is_refused():
    """The injection: attacker email text -> model -> new IMAP commands."""
    with pytest.raises(ValueError, match="plain email address"):
        gmail_tools._safe_address("a@b.c\r\nZ1 SELECT INBOX\r\nZ2 STORE 1:* +FLAGS (\\Deleted)")


def test_a_bare_newline_in_from_address_is_refused():
    with pytest.raises(ValueError):
        gmail_tools._safe_address("a@b.c\nZ1 EXPUNGE")


def test_imap_quoting_and_literal_syntax_are_refused():
    for payload in ('a@b.c" "x', "a@b.c{10}", "a@b.c\\z"):
        with pytest.raises(ValueError):
            gmail_tools._safe_address(payload)


def test_a_non_string_address_is_refused_not_crashed():
    for payload in (None, 123, b"a@b.co"):
        with pytest.raises(ValueError):
            gmail_tools._safe_address(payload)


def test_a_legitimate_address_still_passes():
    assert gmail_tools._safe_address("ada@example.com") == "ada@example.com"


def test_uid_accepts_only_digits():
    assert gmail_tools._safe_uid("42") == "42"
    for payload in ("1\r\nZ1 EXPUNGE", "1:*", "abc", "", None):
        with pytest.raises(ValueError):
            gmail_tools._safe_uid(payload)


def test_the_search_tool_surfaces_a_refusal_as_a_tool_error():
    """A refusal must reach the model as an error result, not raise out."""
    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client())
    out = reg.get("gmail_search_threads").run(from_address="a@b.c\r\nZ1 EXPUNGE")
    assert out.ok is False
    assert "email address" in (out.error or "")


def test_an_app_password_pasted_with_spaces_is_normalised():
    """Google shows the 16 characters as four groups of four, and that is what
    people paste. smtplib sends the string verbatim, so the spaces would come
    back as 'Username and Password not accepted' — a formatting problem wearing
    a wrong-password error message."""
    assert gmail_tools.normalize_app_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert gmail_tools.normalize_app_password("  abcd efgh ijkl mnop  ") == "abcdefghijklmnop"
    assert gmail_tools.normalize_app_password("abcdefghijklmnop") == "abcdefghijklmnop"


def test_the_transports_normalise_what_they_are_constructed_with():
    assert gmail_tools._SMTP("me@example.com", "abcd efgh ijkl mnop").app_password == (
        "abcdefghijklmnop")
    assert gmail_tools._IMAP("me@example.com", "abcd efgh ijkl mnop").app_password == (
        "abcdefghijklmnop")


# ── listing the inbox ─────────────────────────────────────────────────────────
# search() needs a sender, which is right for "what did this customer say" and
# useless for "what is waiting for me". The first live cloud run answered
# "I don't have a known shop inbox address to search against" and stopped.

def test_list_inbox_is_registered_read_only():
    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client())
    assert "gmail_list_inbox" in reg.names()
    assert reg.mutating_names() == ["gmail_send"]


def test_list_recent_returns_sender_subject_and_date():
    class FakeIMAP2:
        def recent(self, limit):
            return [{"uid": "7", "from": "Marcus Webb <m@example.com>",
                     "subject": "Tahoe detail?", "date": "Sun, 13 Sep 2026"}]
    rows = gmail_tools.GmailClient("me@example.com", "pw", imap=FakeIMAP2()).list_recent(5)
    assert rows[0]["subject"] == "Tahoe detail?"
    assert "Marcus Webb" in rows[0]["from"]


class _ListConn(_FakeConn):
    def __init__(self, uids, headers):
        super().__init__(search_data=[uids])
        self._headers = headers
        self.fetch_specs = []

    def search(self, charset, key, value=None):
        self.key = key
        return "OK", self.search_data

    def fetch(self, uid, spec):
        self.fetch_specs.append(spec)
        return "OK", [(uid, self._headers)]


def test_listing_uses_a_literal_ALL_key_and_peeks_only_headers():
    """Nothing caller-supplied reaches the IMAP command line, and listing must
    not mark mail read — gmail_list_inbox is ungated like the other reads."""
    conn = _ListConn(b"1 2 3", b"From: a@b.co\r\nSubject: hi\r\nDate: today\r\n\r\n")
    rows = _ProbeIMAP(conn).recent(limit=2)
    assert conn.key == "ALL"
    assert all("PEEK" in s for s in conn.fetch_specs)
    assert len(rows) == 2


def test_the_limit_is_clamped_rather_than_trusted():
    conn = _ListConn(b"1 2 3 4 5", b"From: a@b.co\r\nSubject: hi\r\n\r\n")
    assert len(_ProbeIMAP(conn).recent(limit=9999)) == 5   # capped by what exists
    conn2 = _ListConn(b"1 2 3 4 5", b"From: a@b.co\r\nSubject: hi\r\n\r\n")
    assert len(_ProbeIMAP(conn2).recent(limit=0)) == 1     # never zero or negative
