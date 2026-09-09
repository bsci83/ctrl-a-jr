"""Stripe, hand-rolled over the REST API.

Test mode is enforced, not assumed: a live key raises at construction. This
agent drafts emails about money to real customers, and the eval design
depends on replayable fixtures — neither is compatible with live keys.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

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
        if not api_key.startswith("sk_test"):
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

    def list_failed_payments(self, limit: int = 5) -> list[dict]:
        body = self._get("invoices", {"status": "open", "limit": limit})
        return body.get("data", [])

    def get_customer(self, customer_id: str) -> dict:
        return self._get(f"customers/{customer_id}")

    def get_invoice(self, invoice_id: str) -> dict:
        return self._get(f"invoices/{invoice_id}")

    def create_payment_link(self, invoice_id: str) -> dict:
        return self.http.request(
            "POST", f"{API}/payment_links",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"line_items[0][quantity]": 1, "metadata[invoice_id]": invoice_id},
        )


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
        description="List open/unpaid Stripe invoices that need recovery. Start here.",
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
        name="stripe_get_invoice",
        description="Fetch one invoice: amount due, currency, due date, status. "
                    "Use the real figures from here — never estimate an amount.",
        schema={"type": "object", "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"]},
        mutating=False,
        run=_wrap(lambda invoice_id: client.get_invoice(invoice_id)),
    ))
    registry.register(ToolSpec(
        name="stripe_create_payment_link",
        description="Create a payment link for an invoice so the customer can pay.",
        schema={"type": "object", "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"]},
        mutating=True,
        run=_wrap(lambda invoice_id: client.create_payment_link(invoice_id)),
        render=lambda invoice_id: f"Create a Stripe payment link for invoice {invoice_id}",
    ))
