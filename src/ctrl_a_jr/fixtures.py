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

# Stripe rejects a `due_date` that is not strictly in the future, on create AND
# on update — an invoice cannot be backdated. A test clock is the documented way
# around that, and it is a trap here: objects on a test clock are excluded from
# list endpoints, so `stripe_list_failed_payments` returned 0 and the agent could
# not find the very invoices it was meant to recover. Verified by calling the
# agent's own tool, 2026-09-13.
#
# So: set due dates a short way into the future, then wait for them to lapse.
# The invoices are ordinary, visible, genuinely past due, and correctly ordered.
# The cost is that "overdue" is minutes rather than days.
LEAD_SECONDS = 90      # until the MOST overdue invoice falls due
STAGGER_SECONDS = 30   # gap between consecutive ranks

# Three customers is enough to exercise "pick the most overdue" without making a
# run tedious to approve by hand.
#
# `rank` is position in the overdue ordering: 0 is the MOST overdue. Amounts are
# deliberately ordered so the most overdue invoice is also the one that crosses
# the $500 Slack threshold in the system prompt. They used to run the other way
# — most overdue was $42 — so the agent's natural path never touched Slack and
# the demo was silently a two-app demo. The fixtures and the prompt have to be
# read together; neither is wrong alone.
DEFAULT_FIXTURES = [
    {"name": "Ada Lovelace", "amount_due": 79900, "rank": 0},
    {"name": "Grace Hopper", "amount_due": 18500, "rank": 1},
    {"name": "Alan Turing", "amount_due": 4200, "rank": 2},
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

    def seed_one(self, base: int, name: str, amount_due: int, rank: int) -> dict:
        """One customer with one finalized invoice, due `rank` steps after `base`."""
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
        due = base + rank * STAGGER_SECONDS
        invoice = self._post("invoices", {
            "customer": customer["id"],
            "collection_method": "send_invoice",
            "due_date": due,
            # Stripe's default here is `exclude`. Without this the pending
            # invoice item never attaches, the invoice totals 0, and a $0
            # invoice auto-pays on finalize — which is how this first showed
            # up: status 'paid' on an invoice that was supposed to be overdue.
            "pending_invoice_items_behavior": "include",
            f"metadata[{FIXTURE_TAG}]": "true",
        })
        finalized = self._post(f"invoices/{invoice['id']}/finalize", {})
        # A send_invoice invoice with no payment method must finalize to `open`.
        # An earlier probe finalized straight to `paid`, which is not the state
        # this agent exists to act on — fail loudly rather than seed a fixture
        # that silently makes the demo a no-op.
        if finalized.get("status") != "open":
            raise RuntimeError(
                f"invoice {finalized['id']} finalized as "
                f"{finalized.get('status')!r}, expected 'open'"
            )
        return {
            "customer_id": customer["id"],
            "name": name,
            "invoice_id": finalized["id"],
            "amount_due": amount_due,
            "rank": rank,
            "due_date": due,
            "hosted_invoice_url": finalized.get("hosted_invoice_url"),
        }

    def seed(self, fixtures: list[dict] | None = None,
             wait: bool = True) -> list[dict]:
        """Create the invoices, then wait for every due date to pass.

        Returning before they lapse would hand back invoices that are merely
        open — which is exactly the state this agent does NOT exist to act on.
        """
        specs = fixtures or DEFAULT_FIXTURES
        base = int(time.time()) + LEAD_SECONDS
        rows = [self.seed_one(base, **f) for f in specs]
        if wait:
            self._wait_until(max(r["due_date"] for r in rows) + 5)
        return rows

    def _wait_until(self, timestamp: int) -> None:
        while True:
            remaining = timestamp - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5))

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
