import pytest
from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import stripe_tools


class FakeHTTP:
    """Records requests and replays canned JSON."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.requests.append((method, url, data))
        key = url.split("/v1/")[-1].split("?")[0]
        return self.responses[key]


def _client(responses):
    return stripe_tools.StripeClient("sk_test_x", http=FakeHTTP(responses))


def test_list_failed_payments_returns_rows():
    c = _client({"invoices": {"data": [
        {"id": "in_1", "customer": "cus_1", "amount_due": 4200, "currency": "usd",
         "status": "open", "due_date": 1757000000},
    ]}})
    out = c.list_failed_payments(limit=5)
    assert out[0]["id"] == "in_1"
    assert out[0]["amount_due"] == 4200


def test_get_customer_returns_email_and_name():
    c = _client({"customers/cus_1": {"id": "cus_1", "email": "a@b.c", "name": "Ada"}})
    assert c.get_customer("cus_1")["email"] == "a@b.c"


def test_test_mode_key_is_required():
    with pytest.raises(ValueError, match="sk_test"):
        stripe_tools.StripeClient("sk_live_danger", http=FakeHTTP({}))


def test_registers_three_read_tools_and_one_mutating():
    reg = Registry()
    stripe_tools.register_stripe_tools(reg, _client({}))
    assert reg.mutating_names() == ["stripe_create_payment_link"]
    assert "stripe_list_failed_payments" in reg.names()
    assert "stripe_get_customer" in reg.names()
    assert "stripe_get_invoice" in reg.names()


def test_read_tool_returns_tool_result():
    reg = Registry()
    c = _client({"customers/cus_1": {"id": "cus_1", "email": "a@b.c", "name": "Ada"}})
    stripe_tools.register_stripe_tools(reg, c)
    out = reg.get("stripe_get_customer").run(customer_id="cus_1")
    assert out.ok and "a@b.c" in out.content


def test_upstream_failure_becomes_error_result():
    class Boom:
        def request(self, *a, **kw):
            raise RuntimeError("stripe 503")

    reg = Registry()
    stripe_tools.register_stripe_tools(reg, stripe_tools.StripeClient("sk_test_x", http=Boom()))
    out = reg.get("stripe_get_customer").run(customer_id="cus_1")
    assert out.ok is False and "503" in (out.error or "")
