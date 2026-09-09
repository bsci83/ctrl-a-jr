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

    def search(self, query, limit):
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
