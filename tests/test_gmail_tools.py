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
    _client(smtp=smtp).send("a@b.c", "Invoice overdue", "Please pay.")
    frm, to, raw = smtp.sent[0]
    text = raw.decode()
    assert to == "a@b.c"
    assert "Subject: Invoice overdue" in text
    assert "Please pay." in text


def test_search_threads_respects_limit():
    imap = FakeIMAP(results=[{"uid": "1"}, {"uid": "2"}, {"uid": "3"}])
    assert len(_client(imap=imap).search_threads("a@b.c", limit=2)) == 2


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
        {"to": "a@b.c", "subject": "Overdue", "body": "Please pay $42.00."}
    )
    assert "a@b.c" in rendered
    assert "Overdue" in rendered
    assert "Please pay $42.00." in rendered


def test_smtp_failure_becomes_error_result():
    class Boom:
        def send(self, *a):
            raise RuntimeError("smtp auth failed")

    reg = Registry()
    gmail_tools.register_gmail_tools(reg, _client(smtp=Boom()))
    out = reg.get("gmail_send").run(to="a@b.c", subject="s", body="b")
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
    assert _ProbeIMAP(conn).search("a@b.c", limit=5) == []


def test_imap_search_handles_a_none_payload():
    conn = _FakeConn(search_data=[None])
    assert _ProbeIMAP(conn).search("a@b.c", limit=5) == []


def test_imap_search_returns_the_most_recent_up_to_limit():
    conn = _FakeConn(search_data=[b"1 2 3 4"])
    assert _ProbeIMAP(conn).search("a@b.c", limit=2) == [{"uid": "3"}, {"uid": "4"}]


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
