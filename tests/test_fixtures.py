"""Tests for the fixture seeder.

The lesson from this build's five review findings: a fake that records requests
and is never read proves nothing. Every test here asserts what actually goes on
the wire — method, path, and body — not merely that a call returned.
"""

import time

import pytest

from ctrl_a_jr import fixtures


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


def _seeder(responses=None):
    return fixtures.FixtureSeeder("sk_test_x", "me@example.com",
                                  http=FakeHTTP(responses or {}))


# ── construction guards ──────────────────────────────────────────────────────

def test_a_live_key_is_refused():
    with pytest.raises(ValueError, match="sk_test"):
        fixtures.FixtureSeeder("sk_live_danger", "me@example.com", http=FakeHTTP())


def test_a_non_string_key_is_refused_with_value_error():
    with pytest.raises(ValueError):
        fixtures.FixtureSeeder(None, "me@example.com", http=FakeHTTP())


def test_a_key_without_the_trailing_underscore_is_refused():
    with pytest.raises(ValueError, match="sk_test"):
        fixtures.FixtureSeeder("sk_testXYZ", "me@example.com", http=FakeHTTP())


def test_a_missing_or_malformed_email_is_refused():
    with pytest.raises(ValueError, match="email"):
        fixtures.FixtureSeeder("sk_test_x", "", http=FakeHTTP())
    with pytest.raises(ValueError, match="email"):
        fixtures.FixtureSeeder("sk_test_x", "not-an-address", http=FakeHTTP())


# ── seeding: assert the wire, not the return value ───────────────────────────

def _seed_responses():
    return {
        "customers": {"id": "cus_1"},
        "invoiceitems": {"id": "ii_1"},
        "invoices": {"id": "in_1"},
        "invoices/in_1/finalize": {"id": "in_1", "status": "open",
                                   "hosted_invoice_url": "https://pay.stripe.com/x"},
    }


def test_seed_one_creates_customer_item_invoice_and_finalizes_in_order():
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    assert [r["path"] for r in s.http.requests] == [
        "customers", "invoiceitems", "invoices", "invoices/in_1/finalize",
    ]
    assert all(r["method"] == "POST" for r in s.http.requests)


def test_every_created_object_carries_the_fixture_tag():
    """Teardown deletes by tag. An untagged object could never be cleaned up —
    and worse, an untagged object is indistinguishable from real data."""
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    by_path = {r["path"]: r["data"] for r in s.http.requests}
    tag_key = f"metadata[{fixtures.FIXTURE_TAG}]"
    assert by_path["customers"][tag_key] == "true"
    assert by_path["invoices"][tag_key] == "true"


def test_the_invoice_due_date_is_actually_in_the_past():
    """An invoice that is merely open is not the state this agent acts on."""
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    invoice_body = next(r["data"] for r in s.http.requests if r["path"] == "invoices")
    due = int(invoice_body["due_date"])
    assert due < int(time.time())
    # 31 days back, within a minute of tolerance for test runtime
    assert abs((int(time.time()) - due) - 31 * 86400) < 60


def test_the_invoice_uses_send_invoice_collection():
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    invoice_body = next(r["data"] for r in s.http.requests if r["path"] == "invoices")
    assert invoice_body["collection_method"] == "send_invoice"


def test_the_amount_and_customer_reach_the_invoice_item():
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    item = next(r["data"] for r in s.http.requests if r["path"] == "invoiceitems")
    assert item["amount"] == 4200
    assert item["customer"] == "cus_1"
    assert item["currency"] == "usd"


def test_seed_one_returns_the_ids_a_run_needs():
    s = _seeder(_seed_responses())
    out = s.seed_one("Ada Lovelace", 4200, 31)
    assert out["customer_id"] == "cus_1"
    assert out["invoice_id"] == "in_1"
    assert out["hosted_invoice_url"] == "https://pay.stripe.com/x"


def test_seed_defaults_to_three_fixtures_with_differing_overdue_ages():
    s = _seeder(_seed_responses())
    out = s.seed()
    assert len(out) == 3
    ages = [f["days_overdue"] for f in fixtures.DEFAULT_FIXTURES]
    assert len(set(ages)) == 3, "the agent must have a most-overdue one to pick"


def test_the_bearer_token_is_sent_as_a_header_and_never_in_a_body():
    s = _seeder(_seed_responses())
    s.seed_one("Ada Lovelace", 4200, 31)
    for r in s.http.requests:
        assert r["headers"]["Authorization"] == "Bearer sk_test_x"
        assert "sk_test_x" not in str(r["data"])


# ── teardown safety: the important tests ─────────────────────────────────────

def test_teardown_deletes_only_tagged_customers():
    """The tag is the only thing between this and someone's real customer list."""
    s = _seeder({"customers": {"data": [
        {"id": "cus_mine", "name": "Ada", "email": "me@example.com",
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


def test_list_fixtures_returns_only_tagged_customers():
    s = _seeder({"customers": {"data": [
        {"id": "cus_mine", "name": "Ada", "email": "me@example.com",
         "metadata": {fixtures.FIXTURE_TAG: "true"}},
        {"id": "cus_REAL", "name": "Real", "email": "real@client.com", "metadata": {}},
    ]}})
    rows = s.list_fixtures()
    assert [r["customer_id"] for r in rows] == ["cus_mine"]


def test_list_handles_a_customer_with_no_metadata_key_at_all():
    s = _seeder({"customers": {"data": [{"id": "cus_x"}]}})
    assert s.list_fixtures() == []
