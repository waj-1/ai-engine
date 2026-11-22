"""core/plugins.py — how product code attaches to the generic engine."""
from __future__ import annotations

import sys
import types

import pytest

from core import plugins
from tests.scoped_build import full_build


@pytest.fixture
def fake_plugin(monkeypatch):
    pkg = types.ModuleType("fakeplug")
    pkg.__path__ = []  # a package, so submodule imports are attempted
    hook = types.ModuleType("fakeplug.app")
    monkeypatch.setitem(sys.modules, "fakeplug", pkg)
    monkeypatch.setitem(sys.modules, "fakeplug.app", hook)
    monkeypatch.setattr(plugins, "plugin_names", lambda: ("fakeplug",))
    return hook


def test_loads_the_hook_module_of_each_configured_plugin(fake_plugin):
    assert plugins.load_hook("app") == [fake_plugin]


def test_a_plugin_without_the_hook_is_skipped(fake_plugin):
    assert plugins.load_hook("voice_builders") == []


def test_no_plugins_means_the_generic_engine_alone(monkeypatch):
    monkeypatch.setattr(plugins, "plugin_names", lambda: ())
    assert plugins.load_hook("app") == []


def test_a_missing_plugin_package_is_fatal(monkeypatch):
    """A configured plugin that is not installed is a broken build. Skipping it
    would boot an engine with its product routes silently absent."""
    monkeypatch.setattr(plugins, "plugin_names", lambda: ("no_such_plugin_pkg",))
    with pytest.raises(ModuleNotFoundError):
        plugins.load_hook("app")


def test_an_error_inside_a_hook_is_not_swallowed(monkeypatch, tmp_path):
    pkg = tmp_path / "brokenplug"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "app.py").write_text("import a_dependency_that_does_not_exist\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(plugins, "plugin_names", lambda: ("brokenplug",))
    with pytest.raises(ModuleNotFoundError) as exc:
        plugins.load_hook("app")
    assert exc.value.name == "a_dependency_that_does_not_exist"


def test_the_product_plugin_ships_in_the_image():
    """`COPY . .` minus .dockerignore is the image. A rule that excluded the
    plugin package or the plugin list would break the deployed app at import."""
    from pathlib import Path

    rules = [ln.strip() for ln in Path(".dockerignore").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    for rule in rules:
        assert rule.rstrip("/*") not in ("waj", "engine_plugins.py", "core", "core/plugins.py"), rule


def test_call_hook_skips_plugins_without_the_function_and_keeps_order():
    a = types.SimpleNamespace(health_issues=lambda: ["a-problem"])
    b = types.SimpleNamespace()  # does not implement the hook
    c = types.SimpleNamespace(health_issues=lambda: ["c-problem"])
    assert plugins.call_hook([a, b, c], "health_issues") == [["a-problem"], ["c-problem"]]
    assert plugins.call_hook([], "health_issues") == []


def test_call_hook_does_not_swallow_a_plugins_error():
    def boom(_env, _client):
        raise RuntimeError("bad secret")

    with pytest.raises(RuntimeError, match="bad secret"):
        plugins.call_hook([types.SimpleNamespace(validate_env=boom)], "validate_env", {}, None)


def test_the_engine_alone_requires_only_its_own_env_and_seeds_only_its_own_rows(monkeypatch):
    """With no plugin, nothing product-specific is required or seeded: the
    shape the open-source build runs in."""
    import main

    monkeypatch.setattr(main, "_PLUGIN_APPS", [])
    full_build(monkeypatch)
    main._validate_env_or_die({"ENVIRONMENT": "development", "SUPABASE_URL": "http://x",
                               "SUPABASE_ANON_KEY": "k"})  # no model key, no product env: fine
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        main._validate_env_or_die({"ENVIRONMENT": "development", "SUPABASE_ANON_KEY": "k"})


def test_optional_import_returns_none_only_when_the_module_itself_is_absent(tmp_path, monkeypatch):
    assert plugins.optional_import("no_such_pkg_xyz.module") is None
    assert plugins.optional_import("core.no_such_module_xyz") is None
    assert plugins.optional_import("core.plugins") is plugins

    # A module that EXISTS but fails on a missing dependency is a real bug:
    # it must surface, not read as "not part of this build".
    pkg = tmp_path / "halfthere"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text("import a_dependency_that_does_not_exist\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError) as exc:
        plugins.optional_import("halfthere.feature")
    assert exc.value.name == "a_dependency_that_does_not_exist"
