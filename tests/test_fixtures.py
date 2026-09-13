"""Tests for the fixture seeder.

The lesson from this build's five review findings: a fake that records requests
and is never read proves nothing. Every test here asserts what actually goes on
the wire — method, path, and body — not merely that a call returned.
"""


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

BASE = 1_900_000_000


def _seed_responses():
    return {
        "customers": {"id": "cus_1"},
        "invoiceitems": {"id": "ii_1"},
        "invoices": {"id": "in_1"},
        "invoices/in_1/finalize": {"id": "in_1", "status": "open",
                                   "hosted_invoice_url": "https://pay.stripe.com/x"},
    }


def _seed_one(s, name="Ada Lovelace", amount=4200, rank=0):
    return s.seed_one(BASE, name, amount, rank)


def test_seed_one_creates_customer_item_invoice_and_finalizes_in_order():
    s = _seeder(_seed_responses())
    _seed_one(s)
    assert [r["path"] for r in s.http.requests] == [
        "customers", "invoiceitems", "invoices", "invoices/in_1/finalize",
    ]
    assert all(r["method"] == "POST" for r in s.http.requests)


def test_every_created_object_carries_the_fixture_tag():
    """Teardown deletes by tag. An untagged object could never be cleaned up —
    and worse, an untagged object is indistinguishable from real data."""
    s = _seeder(_seed_responses())
    _seed_one(s)
    by_path = {r["path"]: r["data"] for r in s.http.requests}
    tag_key = f"metadata[{fixtures.FIXTURE_TAG}]"
    assert by_path["customers"][tag_key] == "true"
    assert by_path["invoices"][tag_key] == "true"


def test_the_due_date_is_in_the_future_at_creation():
    """Stripe rejects a due_date that is not strictly future, on create AND on
    update. The fixture becomes overdue by lapsing, not by backdating."""
    s = _seeder(_seed_responses())
    _seed_one(s, rank=0)
    body = next(r["data"] for r in s.http.requests if r["path"] == "invoices")
    assert int(body["due_date"]) == BASE


def test_rank_orders_the_due_dates_so_rank_zero_is_most_overdue():
    due = [_seed_one(_seeder(_seed_responses()), rank=r)["due_date"] for r in (0, 1, 2)]
    assert due == sorted(due), "rank 0 must fall due first"
    assert due[1] - due[0] == fixtures.STAGGER_SECONDS


def test_no_test_clock_is_used():
    """Objects on a test clock are excluded from Stripe's list endpoints, so
    stripe_list_failed_payments returned 0 and the agent could not find the
    invoices it existed to recover. Verified against live Stripe, 2026-09-13."""
    s = _seeder(_seed_responses())
    s.seed(wait=False)
    assert not any("test_clock" in r["path"] for r in s.http.requests)
    assert all("test_clock" not in (r["data"] or {}) for r in s.http.requests)


def test_the_most_overdue_fixture_also_crosses_the_slack_threshold():
    """The prompt says post to Slack above $500 and the agent works the MOST
    overdue invoice. These used to disagree — most overdue was $42 — so the
    agent's natural path never touched Slack and the submission was silently a
    two-app demo. Nothing else in the suite can catch that."""
    most_overdue = min(fixtures.DEFAULT_FIXTURES, key=lambda f: f["rank"])
    assert most_overdue["amount_due"] > 50000


def test_seed_waits_for_every_due_date_to_lapse(monkeypatch):
    """Returning early hands back invoices that are merely open — the one state
    this agent does not exist to act on."""
    waited = {}
    s = _seeder(_seed_responses())
    monkeypatch.setattr(s, "_wait_until", lambda ts: waited.setdefault("until", ts))
    rows = s.seed()
    assert waited["until"] > max(r["due_date"] for r in rows)


def test_pending_invoice_items_are_explicitly_included():
    """Stripe's default is `exclude`. Without this the item never attaches, the
    invoice totals 0, and a $0 invoice auto-pays on finalize — a fixture that
    looks seeded and gives the agent nothing to recover."""
    s = _seeder(_seed_responses())
    _seed_one(s)
    body = next(r["data"] for r in s.http.requests if r["path"] == "invoices")
    assert body["pending_invoice_items_behavior"] == "include"


def test_the_invoice_uses_send_invoice_collection():
    s = _seeder(_seed_responses())
    _seed_one(s)
    invoice_body = next(r["data"] for r in s.http.requests if r["path"] == "invoices")
    assert invoice_body["collection_method"] == "send_invoice"


def test_the_amount_and_customer_reach_the_invoice_item():
    s = _seeder(_seed_responses())
    _seed_one(s)
    item = next(r["data"] for r in s.http.requests if r["path"] == "invoiceitems")
    assert item["amount"] == 4200
    assert item["customer"] == "cus_1"
    assert item["currency"] == "usd"


def test_seed_one_returns_the_ids_a_run_needs():
    s = _seeder(_seed_responses())
    out = _seed_one(s)
    assert out["customer_id"] == "cus_1"
    assert out["invoice_id"] == "in_1"
    assert out["hosted_invoice_url"] == "https://pay.stripe.com/x"


def test_seed_defaults_to_three_fixtures_with_distinct_ranks():
    s = _seeder(_seed_responses())
    out = s.seed(wait=False)
    assert len(out) == 3
    ranks = [f["rank"] for f in fixtures.DEFAULT_FIXTURES]
    assert len(set(ranks)) == 3, "the agent must have a most-overdue one to pick"


def test_the_bearer_token_is_sent_as_a_header_and_never_in_a_body():
    s = _seeder(_seed_responses())
    _seed_one(s)
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
