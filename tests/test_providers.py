import pytest

from ctrl_a_jr import providers


def test_provider_switch_is_registered_as_mutating():
    from ctrl_a_jr.registry import Registry
    reg = Registry()
    providers.register_provider_tools(reg, providers.ProviderState("minimax", "MiniMax-M3"))
    assert "provider_switch" in reg.mutating_names()


def test_provider_switch_is_not_offered_to_the_model():
    """Spec 7a: failover triggers on transport failure, never on a model-produced
    response — so the model must not be able to call this tool at all. It stays
    dispatchable by the CLI (and therefore still gated), just not advertised."""
    from ctrl_a_jr.registry import Registry
    reg = Registry()
    providers.register_provider_tools(reg, providers.ProviderState("minimax", "MiniMax-M3"))
    assert "provider_switch" not in [s["name"] for s in reg.schemas()]
    assert "provider_switch" in reg.mutating_names()


def test_switch_records_the_new_provider():
    state = providers.ProviderState("minimax", "MiniMax-M3")
    from ctrl_a_jr.registry import Registry
    reg = Registry()
    providers.register_provider_tools(reg, state)
    out = reg.get("provider_switch").run(provider="openrouter", model="claude-sonnet-5",
                                         reason="minimax 503")
    assert out.ok
    assert state.provider == "openrouter"
    assert state.model == "claude-sonnet-5"


def test_switch_renders_the_reason_for_approval():
    from ctrl_a_jr.registry import Registry
    reg = Registry()
    providers.register_provider_tools(reg, providers.ProviderState("minimax", "MiniMax-M3"))
    rendered = reg.get("provider_switch").render_for_approval(
        {"provider": "openrouter", "model": "claude-sonnet-5", "reason": "minimax 503"}
    )
    assert "openrouter" in rendered and "minimax 503" in rendered


def test_a_missing_model_key_names_the_variable(monkeypatch):
    """A KeyError traceback mid-run is the worst place for an ugly failure."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        providers.client_from_env(providers.ProviderState("minimax", "MiniMax-M3"))


def test_a_missing_fallback_key_names_the_variable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
        providers.client_from_env(providers.ProviderState("openrouter", "some-model"))
