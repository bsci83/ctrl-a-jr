import pytest

from ctrl_a_jr.registry import Registry, ToolSpec
from ctrl_a_jr.types import ToolResult


def _spec(name="ping"):
    return ToolSpec(name, "p", {"type": "object", "properties": {}}, False,
                    lambda **kw: ToolResult(True, "pong"))


def test_register_raises_on_a_duplicate_name():
    reg = Registry()
    reg.register(_spec("ping"))
    with pytest.raises(ValueError):
        reg.register(_spec("ping"))


def test_schemas_omit_tools_that_are_not_model_callable():
    reg = Registry()
    reg.register(ToolSpec("hidden", "h", {"type": "object", "properties": {}}, True,
                          lambda **kw: ToolResult(True, "ok"), model_callable=False))
    reg.register(_spec("visible"))
    names = [s["name"] for s in reg.schemas()]
    assert "hidden" not in names
    assert "visible" in names
    assert "hidden" in reg.names()
