import pytest

from ctrl_a_jr import pricing
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


def test_registers_the_read_tools_and_exactly_two_mutating_ones():
    reg = Registry()
    stripe_tools.register_stripe_tools(reg, _client({}))
    assert reg.mutating_names() == ["stripe_create_quote_invoice", "stripe_send_invoice"]
    assert "stripe_list_failed_payments" in reg.names()
    assert "stripe_get_customer" in reg.names()
    assert "stripe_get_invoice" in reg.names()
    assert "stripe_find_customers" in reg.names()


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


# ── the quote invoice: the model must not be able to choose the amount ───────

def _invoice_responses(total=None):
    finalized = {"id": "in_9", "status": "open",
                 "hosted_invoice_url": "https://pay.stripe.com/i/9"}
    if total is not None:
        finalized["total"] = total
    return {
        "customers/cus_1": {"id": "cus_1", "name": "Marcus Webb", "email": "shop@example.com"},
        "invoiceitems": {"id": "ii_1"},
        "invoices": {"id": "in_9"},
        "invoices/in_9/finalize": finalized,
    }


def _amounts_on_the_wire(http):
    return [r["data"]["amount"] for r in http.requests if r["url"].endswith("/invoiceitems")]


def test_the_invoiced_amounts_are_the_menu_amounts():
    """Assert what reaches Stripe, not what the return value claims."""
    http = FakeHTTP(_invoice_responses())
    c = stripe_tools.StripeClient("sk_test_x", http=http)
    out = c.create_quote_invoice("cus_1", "interior_detail", "suv", ["pet_hair"])
    expected = [i["amount_cents"]
                for i in pricing.quote("interior_detail", "suv", ["pet_hair"])["line_items"]]
    assert _amounts_on_the_wire(http) == expected
    assert out["total_cents"] == sum(expected) == 26_125


def test_there_is_no_amount_parameter_for_a_model_to_fill_in():
    """The structural guarantee: a model-chosen figure has nowhere to land.
    A call carrying one is refused, and nothing is created."""
    reg = Registry()
    http = FakeHTTP(_invoice_responses())
    stripe_tools.register_stripe_tools(reg, stripe_tools.StripeClient("sk_test_x", http=http))
    out = reg.get("stripe_create_quote_invoice").run(
        customer_id="cus_1", service="interior_detail", size="suv",
        addons=["pet_hair"], amount=1,
    )
    assert out.ok is False
    assert http.requests == []


def test_a_service_the_model_invented_creates_nothing():
    """pricing.quote runs BEFORE any Stripe object exists, so an unclassifiable
    request cannot leave an orphan invoice item behind."""
    http = FakeHTTP(_invoice_responses())
    c = stripe_tools.StripeClient("sk_test_x", http=http)
    with pytest.raises(ValueError, match="showroom_package"):
        c.create_quote_invoice("cus_1", "showroom_package", "suv")
    assert http.requests == []


def test_a_stripe_total_that_disagrees_with_the_menu_is_an_error():
    """Stripe is the last word on what the customer is charged. If an item we did
    not price attached itself, we must not email a link to that amount."""
    http = FakeHTTP(_invoice_responses(total=99_999))
    c = stripe_tools.StripeClient("sk_test_x", http=http)
    with pytest.raises(RuntimeError, match="expected"):
        c.create_quote_invoice("cus_1", "exterior_detail", "sedan")


def test_pending_invoice_items_are_explicitly_included():
    """Stripe's default is `exclude`: the items never attach, the invoice totals
    0, and a $0 invoice auto-pays on finalize — a paid receipt for unpaid work."""
    http = FakeHTTP(_invoice_responses())
    stripe_tools.StripeClient("sk_test_x", http=http).create_quote_invoice(
        "cus_1", "full_detail", "truck")
    body = next(r["data"] for r in http.requests if r["url"].endswith("/invoices"))
    assert body["pending_invoice_items_behavior"] == "include"
    assert body["collection_method"] == "send_invoice"


def test_the_customer_is_fetched_before_anything_is_created():
    http = FakeHTTP(_invoice_responses())
    stripe_tools.StripeClient("sk_test_x", http=http).create_quote_invoice(
        "cus_1", "exterior_detail", "sedan")
    assert http.requests[0]["method"] == "GET"
    assert http.requests[0]["url"].endswith("/customers/cus_1")


def test_a_deleted_customer_is_refused_before_any_object_is_created():
    responses = _invoice_responses()
    responses["customers/cus_1"] = {"id": "cus_1", "deleted": True}
    http = FakeHTTP(responses)
    c = stripe_tools.StripeClient("sk_test_x", http=http)
    with pytest.raises(RuntimeError, match="deleted"):
        c.create_quote_invoice("cus_1", "exterior_detail", "sedan")
    assert [r["method"] for r in http.requests] == ["GET"]


def test_the_hosted_pay_url_is_returned_so_the_reply_can_carry_a_real_link():
    http = FakeHTTP(_invoice_responses())
    out = stripe_tools.StripeClient("sk_test_x", http=http).create_quote_invoice(
        "cus_1", "ceramic_coating", "truck", ["ozone"])
    assert out["hosted_invoice_url"] == "https://pay.stripe.com/i/9"
    assert out["over_slack_threshold"] is True


def test_find_customers_queries_by_email():
    http = FakeHTTP({"customers": {"data": [
        {"id": "cus_1", "name": "Marcus Webb", "email": "shop@example.com"}]}})
    rows = stripe_tools.StripeClient("sk_test_x", http=http).find_customers("shop@example.com")
    assert "email=shop%40example.com" in http.requests[0]["url"]
    assert rows == [{"id": "cus_1", "name": "Marcus Webb", "email": "shop@example.com"}]


def test_the_approval_render_itemises_the_quote_from_the_menu():
    reg = Registry()
    stripe_tools.register_stripe_tools(reg, _client({}))
    rendered = reg.get("stripe_create_quote_invoice").render_for_approval({
        "customer_id": "cus_1", "customer_name": "Marcus Webb",
        "service": "interior_detail", "size": "suv", "addons": ["pet_hair"],
    })
    assert "Interior detail" in rendered
    assert "Pet hair removal" in rendered
    assert "$261.25" in rendered
