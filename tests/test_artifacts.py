"""The approver reads the artifact, not the arguments (spec §5 P2).

So the quote card has to show the whole breakdown — every line item and the
total — and it has to be safe to show, because the customer name on it came out
of an email a stranger wrote.
"""

from ctrl_a_jr import pricing
from ctrl_a_jr.artifacts import render_artifact

ARGS = {"customer_id": "cus_1", "customer_name": "Marcus Webb",
        "service": "interior_detail", "size": "suv", "addons": ["pet_hair"]}


def test_the_quote_card_shows_every_line_item_and_the_total():
    html = render_artifact("stripe_create_quote_invoice", ARGS)
    q = pricing.quote("interior_detail", "suv", ["pet_hair"])
    for item in q["line_items"]:
        assert item["label"] in html
        assert item["amount"] in html
    assert q["total"] in html


def test_the_card_prices_from_the_menu_rather_than_from_the_arguments():
    """There is no amount in the arguments to display; if one appeared, it would
    be a figure the model wrote and the card would stop being derived."""
    assert "amount" not in ARGS
    html = render_artifact("stripe_create_quote_invoice", ARGS)
    assert "$261.25" in html


def test_a_customer_name_containing_markup_is_escaped():
    """The name is quoted from a stranger's email. Model- or customer-authored
    markup reaching the approval page is XSS into the page that authorises
    sending money requests."""
    html = render_artifact("stripe_create_quote_invoice",
                           dict(ARGS, customer_name='<script>alert("x")</script>'))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_card_names_what_approving_actually_does():
    html = render_artifact("stripe_create_quote_invoice", ARGS)
    assert "payment link" in html


def test_the_read_only_quote_card_does_not_claim_anything_is_sent():
    html = render_artifact("quote_price", {"service": "full_detail", "size": "truck"})
    assert "nothing sent" in html
    assert pricing.quote("full_detail", "truck")["total"] in html


def test_an_unpriceable_card_falls_back_instead_of_raising():
    """export.py replays these renderers with withheld placeholder values. A card
    that raised there would take the whole public evidence page down."""
    html = render_artifact("stripe_create_quote_invoice", {"arguments": "(withheld)"})
    assert "art" in html and "(withheld)" in html


def test_an_unknown_tool_still_renders_a_table():
    html = render_artifact("some_future_tool", {"k": "v"})
    assert "some_future_tool" in html and "v" in html
