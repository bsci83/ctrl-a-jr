import pytest

from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import stripe_tools


class FakeHTTP:
    """Records requests and replays canned JSON."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.requests.append({"method": method, "url": url, "data": data})
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
    assert reg.mutating_names() == ["stripe_send_invoice"]
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


def test_list_failed_payments_sends_the_expected_query():
    """FakeHTTP used to discard the querystring, so nothing checked what we asked Stripe for."""
    http = FakeHTTP({"invoices": {"data": []}})
    stripe_tools.StripeClient("sk_test_x", http=http).list_failed_payments(limit=3)
    url = http.requests[0]["url"]
    assert "status=open" in url
    assert "limit=3" in url


def test_send_invoice_posts_to_the_right_endpoint():
    http = FakeHTTP({"invoices/in_1/send_invoice": {"id": "in_1", "status": "open"}})
    stripe_tools.StripeClient("sk_test_x", http=http).send_invoice("in_1")
    req = http.requests[0]
    assert req["method"] == "POST"
    assert req["url"].endswith("/v1/invoices/in_1/send_invoice")


def test_get_invoice_surfaces_the_hosted_pay_url():
    """hosted_invoice_url is how the customer actually pays — it must reach the model."""
    http = FakeHTTP({"invoices/in_1": {"id": "in_1", "amount_due": 4200,
                                       "hosted_invoice_url": "https://pay.stripe.com/x"}})
    out = stripe_tools.StripeClient("sk_test_x", http=http).get_invoice("in_1")
    assert out["hosted_invoice_url"] == "https://pay.stripe.com/x"


def test_non_string_key_raises_value_error_not_attribute_error():
    with pytest.raises(ValueError):
        stripe_tools.StripeClient(None, http=FakeHTTP({}))


def test_key_without_trailing_underscore_is_refused():
    with pytest.raises(ValueError):
        stripe_tools.StripeClient("sk_testXYZ", http=FakeHTTP({}))
