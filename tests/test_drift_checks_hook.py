"""scripts/check_profile_drift reads its checks from the plugins (hook
"drift_checks"); the engine ships none."""
from __future__ import annotations

from types import SimpleNamespace


def test_check_lists_come_from_the_plugins(monkeypatch):
    import scripts.check_profile_drift as drift
    from core import plugins

    sample = drift.PromptCheck.__new__(drift.PromptCheck)
    module = SimpleNamespace(prompt_checks=lambda: [sample])
    monkeypatch.setattr(plugins, "load_hook", lambda hook: [module] if hook == "drift_checks" else [])
    assert drift._plugin_checks("prompt_checks") == [sample]
    assert drift._plugin_checks("config_checks") == []
    monkeypatch.setattr(plugins, "load_hook", lambda hook: [])
    assert all(drift._plugin_checks(fn) == [] for fn in drift._CHECK_LISTS.values())
