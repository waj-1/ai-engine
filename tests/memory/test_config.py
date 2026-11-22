from core.agent_profile import Profile


def test_profile_carries_memory_config():
    p = Profile(
        profile_id="p1",
        agent_kind="business",
        slug="business_default",
        environment="staging",
        memory_config={"enabled": True},
    )
    assert p.memory_config == {"enabled": True}


def test_profile_memory_config_defaults_empty():
    p = Profile(
        profile_id="p1",
        agent_kind="business",
        slug="business_default",
        environment="staging",
    )
    assert p.memory_config == {}


from core.memory.config import (
    DEFAULT_MEMORY_CONFIG,
    resolve_memory_config,
    resolve_window_config,
)


def test_resolve_empty_returns_defaults_disabled():
    cfg = resolve_memory_config({})
    assert cfg["enabled"] is False
    assert cfg["retrieval"]["top_k"] == 6
    assert cfg["extraction"]["importance_threshold"] == 0.5


def test_window_defaults_present_and_independent_of_enabled():
    # A profile with no memory_config still gets a usable conversation window,
    # even though long-term memory (`enabled`) is off.
    cfg = resolve_memory_config({})
    assert cfg["enabled"] is False
    assert cfg["window"]["max_messages"] == 10
    assert cfg["window"]["max_message_chars"] == 4000
    assert cfg["window"]["suggestion_context_messages"] == 6


def test_resolve_window_config_partial_override_keeps_siblings():
    win = resolve_window_config({"window": {"max_messages": 30}})
    assert win["max_messages"] == 30            # overridden
    assert win["max_message_chars"] == 4000     # default preserved
    assert win["suggestion_context_messages"] == 6


def test_resolve_window_config_none_is_defaults():
    assert resolve_window_config(None)["max_messages"] == 10


def test_resolve_overrides_merge_deeply():
    cfg = resolve_memory_config({"enabled": True, "retrieval": {"top_k": 3}})
    assert cfg["enabled"] is True
    assert cfg["retrieval"]["top_k"] == 3          # overridden
    assert cfg["retrieval"]["min_similarity"] == 0.7  # default preserved


def test_defaults_are_not_mutated():
    resolve_memory_config({"retrieval": {"top_k": 99}})
    assert DEFAULT_MEMORY_CONFIG["retrieval"]["top_k"] == 6
