"""The menu and the pricing calculator, exposed as read-only tools.

Both are pure functions over `pricing`. They are registered `mutating=False`
because they touch nothing — no network, no disk, no state. The model may call
`quote_price` as often as it likes; what it may NOT do is arrive at a number
any other way, which is why the invoice tool re-derives the price from the same
function rather than accepting the one returned here.
"""

from __future__ import annotations

import json

from .. import pricing
from ..registry import Registry, ToolSpec
from ..types import ToolResult

SERVICE_KEYS = sorted(pricing.SERVICES)
SIZE_KEYS = sorted(pricing.SIZES)
ADDON_KEYS = sorted(pricing.ADDONS)


def _wrap(fn):
    def inner(**kwargs):
        try:
            return ToolResult(True, json.dumps(fn(**kwargs), default=str))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, "", str(exc))
    return inner


def register_quote_tools(registry: Registry) -> None:
    registry.register(ToolSpec(
        name="quote_menu",
        description="The shop's service menu: every service, vehicle size and add-on you "
                    "may quote, with the shop's prices. Read this before classifying a "
                    "request. You may only use the keys listed here.",
        schema={"type": "object", "properties": {}},
        mutating=False,
        run=_wrap(pricing.menu),
    ))
    registry.register(ToolSpec(
        name="quote_price",
        description="Price one job from the menu. Your job is to CLASSIFY the customer's "
                    "request — which service, which vehicle size, which add-ons — and this "
                    "tool computes the money. Never estimate, add up or invent a price "
                    "yourself; the figure a customer sees must come from here. An unknown "
                    "service, size or add-on is refused by name rather than guessed.",
        schema={"type": "object", "properties": {
            "service": {"type": "string", "enum": SERVICE_KEYS},
            "size": {"type": "string", "enum": SIZE_KEYS},
            "addons": {"type": "array", "items": {"type": "string", "enum": ADDON_KEYS}},
        }, "required": ["service", "size"]},
        mutating=False,
        run=_wrap(lambda service, size, addons=(): pricing.quote(service, size, addons)),
    ))
