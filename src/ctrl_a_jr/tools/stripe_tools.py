"""Stripe, hand-rolled over the REST API.

Test mode is enforced, not assumed: a live key raises at construction. This
agent drafts emails about money to real customers, and the eval design
depends on replayable fixtures — neither is compatible with live keys.

`create_quote_invoice` takes a CLASSIFICATION (service, vehicle size, add-ons),
never an amount. The amount is re-derived from `pricing.quote` inside this
module, so the number that reaches Stripe cannot be a number the model chose,
even if the model puts one in its arguments — there is no parameter for it to
land in.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import pricing
from ..registry import Registry, ToolSpec
from ..types import ToolResult

API = "https://api.stripe.com/v1"


class _Httpx:
    def request(self, method: str, url: str, headers=None, data=None, timeout=30):
        r = httpx.request(method, url, headers=headers, data=data, timeout=timeout)
        r.raise_for_status()
        return r.json()


class StripeClient:
    def __init__(self, api_key: str, http: Any | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.startswith("sk_test_"):
            raise ValueError(
                "refusing to run against a non-test Stripe key: expected sk_test_*. "
                "This agent sends real email about real invoices."
            )
        self.api_key = api_key
        self.http = http or _Httpx()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.http.request("GET", url, headers={"Authorization": f"Bearer {self.api_key}"})

    def _post(self, path: str, data: dict) -> dict:
        return self.http.request(
            "POST", f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
        )

    def find_customers(self, email: str, limit: int = 10) -> list[dict]:
        """Customers with this email address. The shop's inbox is one address, so
        several fixtures share it; the agent picks by the name on the request."""
        body = self._get("customers", {"email": email, "limit": limit})
        return [{"id": c.get("id"), "name": c.get("name"), "email": c.get("email")}
                for c in body.get("data", [])]

    def create_quote_invoice(self, customer_id: str, service: str, size: str,
                             addons: object = (), customer_name: str = "") -> dict:
        """Invoice a quoted job. The amount is computed here, from the menu.

        There is deliberately no amount parameter. `pricing.quote` raises on an
        unknown service, size or add-on, so an unclassifiable request fails
        before anything is created rather than being invoiced at a guessed price.
        """
        q = pricing.quote(service, size, addons)
        # Fetch first: an invented or deleted customer id must fail before any
        # object is created, not leave an orphan invoice item behind.
        customer = self._get(f"customers/{customer_id}")
        if customer.get("deleted"):
            raise RuntimeError(f"customer {customer_id} is deleted; refusing to invoice it")

        for item in q["line_items"]:
            self._post("invoiceitems", {
                "customer": customer_id,
                "amount": item["amount_cents"],
                "currency": q["currency"],
                "description": item["label"],
            })
        invoice = self._post("invoices", {
            "customer": customer_id,
            "collection_method": "send_invoice",
            "days_until_due": 7,
            # Stripe's default is `exclude`. Without this the pending items never
            # attach, the invoice totals 0, and a $0 invoice auto-pays on
            # finalize — the customer gets a paid-in-full receipt for a job they
            # have not paid for.
            "pending_invoice_items_behavior": "include",
            "metadata[quote_id]": q["quote_id"],
            "metadata[quote_for]": customer_name or (customer.get("name") or ""),
        })
        finalized = self._post(f"invoices/{invoice['id']}/finalize", {})
        stripe_total = finalized.get("total")
        # Stripe is the last word on what the customer will be charged. If it
        # disagrees with the menu, something attached that we did not put there;
        # surface it rather than emailing a link to an amount we never priced.
        if stripe_total is not None and int(stripe_total) != q["total_cents"]:
            raise RuntimeError(
                f"invoice {finalized.get('id')} totals {stripe_total}, "
                f"expected {q['total_cents']} from the menu"
            )
        return {
            "invoice_id": finalized.get("id"),
            "customer_id": customer_id,
            "quote_id": q["quote_id"],
            "status": finalized.get("status"),
            "hosted_invoice_url": finalized.get("hosted_invoice_url"),
            "total_cents": q["total_cents"],
            "total": q["total"],
            "line_items": q["line_items"],
            "over_slack_threshold": q["over_slack_threshold"],
        }

    def list_failed_payments(self, limit: int = 5) -> list[dict]:
        body = self._get("invoices", {"status": "open", "limit": limit})
        return body.get("data", [])

    def get_customer(self, customer_id: str) -> dict:
        return self._get(f"customers/{customer_id}")

    def get_invoice(self, invoice_id: str) -> dict:
        return self._get(f"invoices/{invoice_id}")

    def send_invoice(self, invoice_id: str) -> dict:
        """Ask Stripe to email the customer this invoice, with its hosted pay link."""
        return self.http.request(
            "POST", f"{API}/invoices/{invoice_id}/send_invoice",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={},
        )


def _quote_text(customer_id: str, customer_name: str, service: str, size: str,
                addons: object) -> str:
    """Plain-text itemisation for the approval record."""
    q = pricing.quote(service, size, addons)
    nl = chr(10)
    lines = [f"Invoice {customer_name or customer_id} ({customer_id})",
             f"{q['service_label']} — {q['size_label']}", ""]
    lines += [f"  {item['label']:<34} {item['amount']:>10}" for item in q["line_items"]]
    lines += ["", f"  {'TOTAL':<34} {q['total']:>10}"]
    return nl.join(lines)


def register_stripe_tools(registry: Registry, client: StripeClient) -> None:
    def _wrap(fn):
        def inner(**kwargs):
            try:
                return ToolResult(True, json.dumps(fn(**kwargs), default=str))
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, "", str(exc))
        return inner

    registry.register(ToolSpec(
        name="stripe_list_failed_payments",
        description="List open (unpaid) Stripe invoices. Use it to check whether a customer "
                    "already has an open invoice before raising another one.",
        schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        mutating=False,
        run=_wrap(lambda limit=5: client.list_failed_payments(limit)),
    ))
    registry.register(ToolSpec(
        name="stripe_get_customer",
        description="Fetch a customer's name and email by Stripe customer id.",
        schema={"type": "object", "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"]},
        mutating=False,
        run=_wrap(lambda customer_id: client.get_customer(customer_id)),
    ))
    registry.register(ToolSpec(
        name="stripe_find_customers",
        description="Find the shop's customers by email address. Requests arrive at one shop "
                    "inbox, so several customers can share it — match the name on the request "
                    "to pick the right one. Never invent a customer id.",
        schema={"type": "object",
                "properties": {"email": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["email"]},
        mutating=False,
        run=_wrap(lambda email, limit=10: client.find_customers(email, limit)),
    ))
    registry.register(ToolSpec(
        name="stripe_create_quote_invoice",
        description="Create and finalize the Stripe invoice for a quoted job, and return its "
                    "hosted payment link. Pass the CLASSIFICATION — service, vehicle size, "
                    "add-ons — exactly as you passed it to quote_price. There is no amount "
                    "parameter: the price is computed from the shop's menu, not by you.",
        schema={"type": "object", "properties": {
            "customer_id": {"type": "string"},
            "customer_name": {"type": "string"},
            "service": {"type": "string", "enum": sorted(pricing.SERVICES)},
            "size": {"type": "string", "enum": sorted(pricing.SIZES)},
            "addons": {"type": "array",
                       "items": {"type": "string", "enum": sorted(pricing.ADDONS)}},
        }, "required": ["customer_id", "service", "size"]},
        mutating=True,
        run=_wrap(lambda customer_id, service, size, addons=(), customer_name="":
                  client.create_quote_invoice(customer_id, service, size, addons,
                                              customer_name)),
        # Derived from the menu, like the charge itself — the approver reads the
        # same breakdown the invoice is built from, not a summary the model wrote.
        render=lambda customer_id, service, size, addons=(), customer_name="": (
            _quote_text(customer_id, customer_name, service, size, addons)
        ),
    ))
    registry.register(ToolSpec(
        name="stripe_get_invoice",
        description="Fetch one invoice: amount due, currency, due date, status. "
                    "Use the real figures from here — never estimate an amount.",
        schema={"type": "object", "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"]},
        mutating=False,
        run=_wrap(lambda invoice_id: client.get_invoice(invoice_id)),
    ))
    registry.register(ToolSpec(
        name="stripe_send_invoice",
        description="Ask Stripe to email the customer their invoice, which includes a hosted "
                    "payment link. Use this only after the recovery email has been approved. "
                    "The pay URL is already on the invoice as hosted_invoice_url if you only "
                    "need to reference it in your own email.",
        schema={"type": "object", "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"]},
        mutating=True,
        run=_wrap(lambda invoice_id: client.send_invoice(invoice_id)),
        render=lambda invoice_id: (
            f"Stripe will EMAIL the customer their invoice {invoice_id} from your Stripe "
            f"account, including a payment link."
        ),
    ))
