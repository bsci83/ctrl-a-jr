"""Tests for the fixture seeder.

The lesson from this build's five review findings: a fake that records requests
and is never read proves nothing. Every test here asserts what actually goes on
the wire — method, path, body, and for mail the raw bytes handed to SMTP — not
merely that a call returned.
"""


import pytest

from ctrl_a_jr import fixtures, pricing


class FakeHTTP:
    """Records every request and replays canned responses by path."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        path = url.split("/v1/")[-1].split("?")[0]
        self.requests.append({"method": method, "path": path, "data": data,
                              "headers": headers})
        return self.responses.get(path, {})


class FakeSMTP:
    """Records the envelope and the raw message bytes, which is the wire."""

    def __init__(self):
        self.sent = []

    def send(self, from_addr, to_addr, message_bytes):
        self.sent.append({"from": from_addr, "to": to_addr, "raw": message_bytes})


def _seeder(responses=None, smtp=None):
    return fixtures.FixtureSeeder("sk_test_x", "shop@example.com", "app pass word here",
                                  http=FakeHTTP(responses or {}), smtp=smtp or FakeSMTP())


# ── construction guards ──────────────────────────────────────────────────────

def test_a_live_key_is_refused():
    with pytest.raises(ValueError, match="sk_test"):
        fixtures.FixtureSeeder("sk_live_danger", "me@example.com", smtp=FakeSMTP())


def test_a_non_string_key_is_refused_with_value_error():
    with pytest.raises(ValueError):
        fixtures.FixtureSeeder(None, "me@example.com", smtp=FakeSMTP())


def test_a_key_without_the_trailing_underscore_is_refused():
    with pytest.raises(ValueError, match="sk_test"):
        fixtures.FixtureSeeder("sk_testXYZ", "me@example.com", smtp=FakeSMTP())


def test_a_missing_or_malformed_email_is_refused():
    with pytest.raises(ValueError, match="email"):
        fixtures.FixtureSeeder("sk_test_x", "", smtp=FakeSMTP())
    with pytest.raises(ValueError, match="email"):
        fixtures.FixtureSeeder("sk_test_x", "not-an-address", smtp=FakeSMTP())


def test_seeding_without_an_app_password_refuses_before_touching_stripe():
    """Half a fixture set — customers in Stripe, no mail in the inbox — is worse
    than none: the agent finds nothing to read and the run looks like a model
    failure."""
    s = fixtures.FixtureSeeder("sk_test_x", "shop@example.com", "", http=FakeHTTP())
    with pytest.raises(ValueError, match="GMAIL_APP_PASSWORD"):
        s.seed()
    assert s.http.requests == []


# ── seeding: assert the wire, not the return value ───────────────────────────

def _seed_responses():
    return {"customers": {"id": "cus_1"}}


def test_seed_one_creates_a_tagged_customer_and_sends_the_request_email():
    smtp = FakeSMTP()
    s = _seeder(_seed_responses(), smtp=smtp)
    s.seed_one(**fixtures.DEFAULT_FIXTURES[0])
    assert [r["path"] for r in s.http.requests] == ["customers"]
    assert s.http.requests[0]["data"][f"metadata[{fixtures.FIXTURE_TAG}]"] == "true"
    assert len(smtp.sent) == 1


def test_the_request_email_is_really_sent_with_the_customer_text_on_the_wire():
    """The agent must parse a human message. If the body never left this process
    there is nothing in the inbox and the demo is the agent being told the answer."""
    smtp = FakeSMTP()
    s = _seeder(_seed_responses(), smtp=smtp)
    spec = fixtures.DEFAULT_FIXTURES[0]
    s.seed_one(**spec)
    wire = smtp.sent[0]
    raw = wire["raw"].decode()
    assert wire["from"] == "shop@example.com"
    assert wire["to"] == "shop@example.com"
    assert f"Subject: {spec['subject']}" in raw
    assert "2019 Tahoe" in raw
    assert "Marcus Webb" in raw


def test_the_customer_name_rides_in_the_from_display_name():
    """Gmail rewrites a From address that is not the authenticated account, so
    the display name and the sign-off are the only identity the agent gets."""
    smtp = FakeSMTP()
    _seeder(_seed_responses(), smtp=smtp).seed_one(**fixtures.DEFAULT_FIXTURES[1])
    raw = smtp.sent[0]["raw"].decode()
    assert "From: Denise Okafor <shop@example.com>" in raw


def test_every_seeded_message_carries_the_fixture_header():
    """Mail teardown is manual — the agent's IMAP path is readonly. This header
    is how the seeded messages are found again."""
    smtp = FakeSMTP()
    _seeder(_seed_responses(), smtp=smtp).seed()
    for wire in smtp.sent:
        assert f"{fixtures.FIXTURE_HEADER}: true" in wire["raw"].decode()


def test_seed_creates_three_requests_and_three_customers():
    smtp = FakeSMTP()
    s = _seeder(_seed_responses(), smtp=smtp)
    rows = s.seed()
    assert len(rows) == 3
    assert len(smtp.sent) == 3
    assert [r["path"] for r in s.http.requests] == ["customers"] * 3


def test_the_request_bodies_contain_no_structured_fields():
    """The point of the demo is that the agent reads prose. A body carrying
    'service: interior_detail' would be a form, and parsing a form proves
    nothing about parsing a customer."""
    for spec in fixtures.DEFAULT_FIXTURES:
        body = spec["body"].lower()
        for key in (*pricing.SERVICES, *pricing.SIZES, *pricing.ADDONS):
            assert key not in body, f"{key} appears verbatim in {spec['name']}'s request"


def test_exactly_one_request_crosses_the_slack_threshold():
    """One above and two below is what makes the escalation step meaningful:
    an agent that posts to Slack every time, or never, both look correct when
    every fixture sits on the same side of the line."""
    totals = [pricing.quote(**spec["expect"])["total_cents"]
              for spec in fixtures.DEFAULT_FIXTURES]
    over = [t for t in totals if t > pricing.SLACK_THRESHOLD_CENTS]
    assert len(over) == 1


def test_the_biggest_job_is_the_one_that_crosses_the_threshold():
    """The task says handle the biggest job and the prompt says escalate above
    the threshold. If the biggest job sat under it the agent's natural path would
    never touch Slack and the submission would silently be a two-app demo."""
    priced = [(pricing.quote(**s["expect"])["total_cents"], s["name"])
              for s in fixtures.DEFAULT_FIXTURES]
    biggest = max(priced)
    assert biggest[0] > pricing.SLACK_THRESHOLD_CENTS
    assert biggest[1] == "Denise Okafor"


def test_every_expected_classification_is_priceable_from_the_menu():
    """A fixture describing a job the menu cannot price would look like the agent
    failing to classify."""
    for spec in fixtures.DEFAULT_FIXTURES:
        assert pricing.quote(**spec["expect"])["total_cents"] > 0


def test_seed_reports_the_expected_total_and_the_threshold_flag():
    s = _seeder(_seed_responses())
    rows = s.seed()
    by_name = {r["name"]: r for r in rows}
    assert by_name["Denise Okafor"]["over_slack_threshold"] is True
    assert by_name["Ray Alvarez"]["over_slack_threshold"] is False
    assert by_name["Ray Alvarez"]["expected_total"] == "$129.00"


def test_no_invoice_is_ever_seeded():
    """Pricing the job is the agent's work. A seeded invoice would pre-compute
    the one number the whole design exists to keep out of the model's hands."""
    s = _seeder(_seed_responses())
    s.seed()
    assert not [r for r in s.http.requests if "invoice" in r["path"]]


def test_the_bearer_token_is_sent_as_a_header_and_never_in_a_body():
    s = _seeder(_seed_responses())
    s.seed()
    for r in s.http.requests:
        assert r["headers"]["Authorization"] == "Bearer sk_test_x"
        assert "sk_test_x" not in str(r["data"])


def test_the_app_password_never_reaches_stripe_or_the_message_body():
    smtp = FakeSMTP()
    s = _seeder(_seed_responses(), smtp=smtp)
    s.seed()
    assert all("app pass" not in str(r["data"]) for r in s.http.requests)
    assert all(b"app pass" not in w["raw"] for w in smtp.sent)


# ── teardown safety: the important tests ─────────────────────────────────────

def test_teardown_deletes_only_tagged_customers():
    """The tag is the only thing between this and someone's real customer list."""
    s = _seeder({"customers": {"data": [
        {"id": "cus_mine", "name": "Ada", "email": "shop@example.com",
         "metadata": {fixtures.FIXTURE_TAG: "true"}},
        {"id": "cus_REAL", "name": "A Real Customer", "email": "real@client.com",
         "metadata": {}},
    ]}})
    removed = s.teardown()
    assert removed == ["cus_mine"]
    deletes = [r["path"] for r in s.http.requests if r["method"] == "DELETE"]
    assert deletes == ["customers/cus_mine"]
    assert "customers/cus_REAL" not in deletes


def test_teardown_ignores_a_customer_whose_tag_is_not_exactly_true():
    s = _seeder({"customers": {"data": [
        {"id": "cus_x", "metadata": {fixtures.FIXTURE_TAG: "false"}},
        {"id": "cus_y", "metadata": {"something_else": "true"}},
    ]}})
    assert s.teardown() == []
    assert not [r for r in s.http.requests if r["method"] == "DELETE"]


def test_teardown_on_an_empty_account_deletes_nothing():
    s = _seeder({"customers": {"data": []}})
    assert s.teardown() == []


def test_teardown_sends_no_mail():
    smtp = FakeSMTP()
    s = _seeder({"customers": {"data": []}}, smtp=smtp)
    s.teardown()
    assert smtp.sent == []


def test_list_fixtures_returns_only_tagged_customers():
    s = _seeder({"customers": {"data": [
        {"id": "cus_mine", "name": "Ada", "email": "shop@example.com",
         "metadata": {fixtures.FIXTURE_TAG: "true"}},
        {"id": "cus_REAL", "name": "Real", "email": "real@client.com", "metadata": {}},
    ]}})
    rows = s.list_fixtures()
    assert [r["customer_id"] for r in rows] == ["cus_mine"]


def test_list_handles_a_customer_with_no_metadata_key_at_all():
    s = _seeder({"customers": {"data": [{"id": "cus_x"}]}})
    assert s.list_fixtures() == []
