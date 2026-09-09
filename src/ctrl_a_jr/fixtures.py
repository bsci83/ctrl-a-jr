"""Seed and tear down Stripe test-mode fixtures for eval runs.

This is DEV TOOLING, not agent capability. Nothing here is registered as a tool:
the agent can read invoices and ask Stripe to send them, but it must never be
able to create a customer or mint an invoice. Keeping this out of the registry
is what keeps the agent's surface at 11 hand-audited tools.

Two safety rules, both load-bearing:

1. **Test mode only.** A key that is not `sk_test_*` is refused at construction,
   the same rule the agent's own StripeClient enforces.
2. **Teardown only touches what this module made.** Every object is stamped with
   `metadata[ctrl_a_jr_fixture]=true`, and teardown deletes nothing without it.
   A teardown that could reach real customer data would be worse than no teardown.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

API = "https://api.stripe.com/v1"
FIXTURE_TAG = "ctrl_a_jr_fixture"

# Three customers is enough to exercise "pick the most overdue" without making a
# run tedious to approve by hand.
DEFAULT_FIXTURES = [
    {"name": "Ada Lovelace", "amount_due": 4200, "days_overdue": 31},
    {"name": "Grace Hopper", "amount_due": 18500, "days_overdue": 12},
    {"name": "Alan Turing", "amount_due": 79900, "days_overdue": 3},
]


class _Httpx:
    def request(self, method: str, url: str, headers=None, data=None, timeout=30):
        r = httpx.request(method, url, headers=headers, data=data, timeout=timeout)
        r.raise_for_status()
        return r.json()


class FixtureSeeder:
    """Creates and removes Stripe test-mode fixtures for an eval run."""

    def __init__(self, api_key: str, email: str, http: Any | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.startswith("sk_test_"):
            raise ValueError(
                "refusing to seed against a non-test Stripe key: expected sk_test_*. "
                "Fixtures create and delete objects; that must never touch live data."
            )
        if not email or "@" not in email:
            raise ValueError(
                "a deliverable email address is required — the agent will draft mail to it, "
                "and you need to be able to read what it sent."
            )
        self.api_key = api_key
        self.email = email
        self.http = http or _Httpx()

    # ── transport ────────────────────────────────────────────────────────────

    def _post(self, path: str, data: dict) -> dict:
        return self.http.request(
            "POST", f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.http.request(
            "GET", url, headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def _delete(self, path: str) -> dict:
        return self.http.request(
            "DELETE", f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    # ── seeding ──────────────────────────────────────────────────────────────

    def seed_one(self, name: str, amount_due: int, days_overdue: int) -> dict:
        """One customer with one finalized, past-due invoice."""
        customer = self._post("customers", {
            "name": name,
            "email": self.email,
            f"metadata[{FIXTURE_TAG}]": "true",
        })
        self._post("invoiceitems", {
            "customer": customer["id"],
            "amount": amount_due,
            "currency": "usd",
            "description": f"Overdue balance for {name}",
        })
        # due_date is an absolute past timestamp — days_until_due cannot express
        # "already overdue", and an invoice that is merely open is not the state
        # this agent exists to act on.
        due = int(time.time()) - days_overdue * 86400
        invoice = self._post("invoices", {
            "customer": customer["id"],
            "collection_method": "send_invoice",
            "due_date": due,
            f"metadata[{FIXTURE_TAG}]": "true",
        })
        finalized = self._post(f"invoices/{invoice['id']}/finalize", {})
        return {
            "customer_id": customer["id"],
            "name": name,
            "invoice_id": finalized["id"],
            "amount_due": amount_due,
            "days_overdue": days_overdue,
            "hosted_invoice_url": finalized.get("hosted_invoice_url"),
        }

    def seed(self, fixtures: list[dict] | None = None) -> list[dict]:
        return [self.seed_one(**f) for f in (fixtures or DEFAULT_FIXTURES)]

    # ── listing and teardown ─────────────────────────────────────────────────

    def list_fixtures(self) -> list[dict]:
        """Every customer this module created, by tag."""
        found = []
        for c in self._get("customers", {"limit": 100}).get("data", []):
            if (c.get("metadata") or {}).get(FIXTURE_TAG) == "true":
                found.append({"customer_id": c["id"], "name": c.get("name"),
                              "email": c.get("email")})
        return found

    def teardown(self) -> list[str]:
        """Delete ONLY tagged customers. Deleting a customer voids their invoices.

        An untagged object is never touched, even in test mode — the tag is the
        only thing standing between this and someone's real customer list if a
        live key ever slipped past the constructor.
        """
        removed = []
        for row in self.list_fixtures():
            self._delete(f"customers/{row['customer_id']}")
            removed.append(row["customer_id"])
        return removed
