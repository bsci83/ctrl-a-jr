"""The shop's service menu, and the ONLY place a price is computed.

The model classifies; the code prices. That split is the whole point of this
module. A tool that accepted an `amount` from the model would let the model
author the number a customer is charged, and the approval card would then be
showing a figure with no provenance — exactly the failure this project exists
to prevent. So the menu is Python data, `quote()` is a pure function of it, and
every path to Stripe re-derives the amount here rather than trusting an
argument.

Money is integer cents throughout. Multipliers are integer percents and applied
with `//`, not floats: `14900 * 1.25` is representable but `x * 1.15` is not,
and a price that differs in the last cent between the approval card and the
invoice is a payload-integrity failure, not a rounding curiosity.
"""

from __future__ import annotations

import hashlib

CURRENCY = "usd"

# Above this the shop wants a human to see the job in Slack before it becomes a
# routine auto-reply. The SYSTEM prompt in cli.py quotes this figure; the
# fixtures are sized around it so exactly one seeded request crosses it.
SLACK_THRESHOLD_CENTS = 50_000

SERVICES: dict[str, dict] = {
    "interior_detail": {"label": "Interior detail", "base_cents": 14_900},
    "exterior_detail": {"label": "Exterior detail", "base_cents": 12_900},
    "full_detail": {"label": "Full detail (interior + exterior)", "base_cents": 24_900},
    "ceramic_coating": {"label": "Ceramic coating (9H, 2 year)", "base_cents": 69_900},
}

SIZES: dict[str, dict] = {
    "sedan": {"label": "Sedan / coupe", "percent": 100},
    "suv": {"label": "SUV / crossover", "percent": 125},
    "truck": {"label": "Truck / van", "percent": 140},
}

ADDONS: dict[str, dict] = {
    "pet_hair": {"label": "Pet hair removal", "cents": 7_500},
    "ozone": {"label": "Ozone odour treatment", "cents": 4_500},
    "engine_bay": {"label": "Engine bay cleaning", "cents": 6_000},
}


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def menu() -> dict:
    """The menu as data, for the model to classify against."""
    return {
        "currency": CURRENCY,
        "services": [{"key": k, "label": v["label"], "base": dollars(v["base_cents"])}
                     for k, v in SERVICES.items()],
        "sizes": [{"key": k, "label": v["label"], "multiplier": f"{v['percent']}%"}
                  for k, v in SIZES.items()],
        "addons": [{"key": k, "label": v["label"], "price": dollars(v["cents"])}
                   for k, v in ADDONS.items()],
        "slack_threshold": dollars(SLACK_THRESHOLD_CENTS),
    }


def _normalize_addons(addons: object) -> list[str]:
    """Refuse an unknown or repeated add-on BY NAME.

    Defaulting an unrecognised word to "no add-on" would quietly undercharge,
    and guessing the nearest match would price a job the customer did not ask
    for. Both are silent; a refusal is a tool error the model can read and fix.
    """
    if addons is None:
        return []
    if isinstance(addons, str):
        raise ValueError("addons must be a list of add-on keys, not a string")
    try:
        items = list(addons)
    except TypeError as exc:
        raise ValueError("addons must be a list of add-on keys") from exc
    seen: list[str] = []
    for a in items:
        if not isinstance(a, str) or a not in ADDONS:
            raise ValueError(
                f"unknown add-on {a!r}. Valid add-ons: {', '.join(sorted(ADDONS))}"
            )
        if a in seen:
            raise ValueError(f"add-on {a!r} is listed twice; each add-on is priced once")
        seen.append(a)
    return seen


def quote(service: str, size: str, addons: object = ()) -> dict:
    """Price one job from the menu. Pure: same inputs, same output, no I/O.

    The returned `line_items` always sum to `total_cents` — the total is their
    sum, never a separately computed figure that could drift from the breakdown
    shown on the approval card.
    """
    if not isinstance(service, str) or service not in SERVICES:
        raise ValueError(
            f"unknown service {service!r}. Valid services: {', '.join(sorted(SERVICES))}"
        )
    if not isinstance(size, str) or size not in SIZES:
        raise ValueError(
            f"unknown vehicle size {size!r}. Valid sizes: {', '.join(sorted(SIZES))}"
        )
    keys = _normalize_addons(addons)

    svc = SERVICES[service]
    sz = SIZES[size]
    base = svc["base_cents"]
    adjusted = base * sz["percent"] // 100

    line_items = [{"label": svc["label"], "amount_cents": base}]
    if adjusted != base:
        line_items.append({
            "label": f"{sz['label']} size ({sz['percent']}%)",
            "amount_cents": adjusted - base,
        })
    for key in keys:
        line_items.append({"label": ADDONS[key]["label"],
                           "amount_cents": ADDONS[key]["cents"]})

    total = sum(item["amount_cents"] for item in line_items)
    return {
        "quote_id": quote_id(service, size, keys),
        "service": service,
        "service_label": svc["label"],
        "size": size,
        "size_label": sz["label"],
        "addons": keys,
        "currency": CURRENCY,
        "line_items": [dict(item, amount=dollars(item["amount_cents"])) for item in line_items],
        "total_cents": total,
        "total": dollars(total),
        "over_slack_threshold": total > SLACK_THRESHOLD_CENTS,
    }


def quote_id(service: str, size: str, addons: list[str]) -> str:
    """Stable id for one priced configuration, so the invoice, the approval card
    and the report can be shown to refer to the same quote."""
    canonical = f"{service}|{size}|{','.join(addons)}"
    return "q_" + hashlib.sha256(canonical.encode()).hexdigest()[:10]
