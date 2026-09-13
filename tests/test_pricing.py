"""The menu is the only source of a price, so these are the tests that matter.

Every assertion here is about a number the customer would be charged.
"""

from pathlib import Path

import pytest

from ctrl_a_jr import pricing
from ctrl_a_jr.registry import Registry
from ctrl_a_jr.tools import quote_tools


def test_the_package_under_test_is_this_worktree():
    """An editable install points `ctrl_a_jr` at ONE checkout, so a pytest run
    started inside a worktree can report green over the MAIN checkout's source.
    pyproject sets pythonpath=["src"]; this proves it took effect."""
    import ctrl_a_jr
    assert Path(ctrl_a_jr.__file__).resolve().is_relative_to(Path.cwd().resolve())


# ── purity and arithmetic ────────────────────────────────────────────────────

def test_quote_is_deterministic_for_the_same_inputs():
    a = pricing.quote("full_detail", "suv", ["pet_hair", "ozone"])
    b = pricing.quote("full_detail", "suv", ["pet_hair", "ozone"])
    assert a == b


def test_the_breakdown_sums_to_the_total():
    """The approval card shows the line items and the customer pays the total.
    If those two are computed separately they can drift apart."""
    for service in pricing.SERVICES:
        for size in pricing.SIZES:
            q = pricing.quote(service, size, list(pricing.ADDONS))
            assert sum(i["amount_cents"] for i in q["line_items"]) == q["total_cents"]


def test_the_size_multiplier_is_applied_to_the_base_only():
    sedan = pricing.quote("interior_detail", "sedan", ["pet_hair"])
    suv = pricing.quote("interior_detail", "suv", ["pet_hair"])
    base = pricing.SERVICES["interior_detail"]["base_cents"]
    addon = pricing.ADDONS["pet_hair"]["cents"]
    assert sedan["total_cents"] == base + addon
    assert suv["total_cents"] == base * 125 // 100 + addon


def test_totals_are_whole_cents():
    """Integer percents, integer floor division — never a float cent that renders
    one way on the card and another on the invoice."""
    for size in pricing.SIZES:
        q = pricing.quote("ceramic_coating", size)
        assert isinstance(q["total_cents"], int)


def test_no_addons_is_not_an_error():
    assert pricing.quote("exterior_detail", "sedan")["addons"] == []


# ── refusals: the price must never be guessed ────────────────────────────────

def test_an_unknown_service_is_refused_by_name():
    with pytest.raises(ValueError, match="headlight_polish"):
        pricing.quote("headlight_polish", "sedan")


def test_an_unknown_size_is_refused_by_name():
    with pytest.raises(ValueError, match="motorcycle"):
        pricing.quote("full_detail", "motorcycle")


def test_an_unknown_addon_is_refused_by_name():
    with pytest.raises(ValueError, match="undercoating"):
        pricing.quote("full_detail", "suv", ["pet_hair", "undercoating"])


def test_an_unknown_addon_is_not_silently_dropped():
    """The dangerous failure is not the exception — it is pricing the job as if
    the add-on had not been asked for, and undercharging silently."""
    with pytest.raises(ValueError):
        pricing.quote("interior_detail", "suv", ["ozone", "clay_bar"])


def test_a_duplicated_addon_is_refused_rather_than_double_charged():
    with pytest.raises(ValueError, match="twice"):
        pricing.quote("interior_detail", "suv", ["pet_hair", "pet_hair"])


def test_a_string_of_addons_is_refused_not_iterated_as_characters():
    with pytest.raises(ValueError, match="list"):
        pricing.quote("interior_detail", "suv", "pet_hair")


def test_a_non_string_service_raises_value_error_not_attribute_error():
    with pytest.raises(ValueError):
        pricing.quote(None, "sedan")


# ── the tool surface ─────────────────────────────────────────────────────────

def test_quote_tools_are_read_only():
    reg = Registry()
    quote_tools.register_quote_tools(reg)
    assert reg.mutating_names() == []
    assert "quote_price" in reg.names()
    assert "quote_menu" in reg.names()


def test_the_quote_tool_returns_the_total_as_a_tool_result():
    reg = Registry()
    quote_tools.register_quote_tools(reg)
    out = reg.get("quote_price").run(service="interior_detail", size="suv",
                                     addons=["pet_hair"])
    assert out.ok and '"total": "$261.25"' in out.content


def test_the_quote_tool_surfaces_the_refusal_as_a_tool_error():
    """The model must be able to read why it was refused and fix its
    classification — a crash in the loop teaches it nothing."""
    reg = Registry()
    quote_tools.register_quote_tools(reg)
    out = reg.get("quote_price").run(service="engine_swap", size="suv")
    assert out.ok is False and "engine_swap" in (out.error or "")


def test_the_schema_enumerates_only_menu_keys():
    """The enum is the model's first line of defence against inventing a service."""
    reg = Registry()
    quote_tools.register_quote_tools(reg)
    props = reg.get("quote_price").schema["properties"]
    assert props["service"]["enum"] == sorted(pricing.SERVICES)
    assert props["size"]["enum"] == sorted(pricing.SIZES)
    assert props["addons"]["items"]["enum"] == sorted(pricing.ADDONS)
