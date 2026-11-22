from runtime.prompt_cache import cached_system_message


def test_no_cache_control_by_default():
    m = {"provider": "anthropic", "extra_config": {}}
    msg = cached_system_message("SYS", m)
    assert msg.content == "SYS"
    assert "cache_control" not in (msg.additional_kwargs or {})


def test_cache_control_when_anthropic_and_enabled():
    m = {"provider": "anthropic", "extra_config": {"prompt_cache": True}}
    msg = cached_system_message("SYS", m)
    assert msg.additional_kwargs["cache_control"] == {"type": "ephemeral"}


def test_no_cache_control_for_retired_anthropic_vertex_provider():
    """`anthropic_vertex` was removed from _ANTHROPIC_PROVIDERS on 2026-08-16
    with its builder. Nothing can produce such a row now, so treating it as an
    Anthropic model would only stamp cache_control onto a row that cannot be
    built — assert the branch is really gone."""
    m = {"provider": "anthropic_vertex", "extra_config": {"prompt_cache": True}}
    msg = cached_system_message("SYS", m)
    assert "cache_control" not in (msg.additional_kwargs or {})


def test_no_cache_control_for_gemini_even_when_flag_set():
    m = {"provider": "google_genai", "extra_config": {"prompt_cache": True}}
    msg = cached_system_message("SYS", m)
    assert "cache_control" not in (msg.additional_kwargs or {})


def test_handles_none_model():
    msg = cached_system_message("SYS", None)
    assert msg.content == "SYS"
    assert "cache_control" not in (msg.additional_kwargs or {})
