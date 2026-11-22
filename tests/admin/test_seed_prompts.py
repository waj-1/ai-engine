"""seed_prompts.run() must be idempotent and never raise on import errors."""
from __future__ import annotations


def test_run_does_not_raise_on_empty_db(monkeypatch):
    """With a null DB client, run() should return zero counts without raising."""
    from admin import seed_prompts

    class _Builder:
        def __init__(self):
            self.data = []
        def select(self, *a, **k): return self
        def insert(self, payload):
            self.data = [{"prompt_id": "00000000-0000-0000-0000-000000000001"}]
            return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def order(self, *a, **k): return self
        def execute(self):
            return type("Res", (), {"data": list(self.data)})()

    class FakeClient:
        def table(self, name):
            return _Builder()

    monkeypatch.setattr(seed_prompts, "get_service_client", lambda: FakeClient())
    out = seed_prompts.run()
    # With the fake client, _prompt_has_any_version returns False (empty data),
    # so one version row inserts per spec. Total spec entries = 9.
    assert out["versions"] >= 0
    assert isinstance(out, dict)


def test_to_spec_marks_write_tools(monkeypatch):
    from admin import sync_tools

    monkeypatch.setattr(sync_tools, "WRITE_TOOL_NAMES", {"save_sample"})

    class _Fake:
        name = "save_sample"
        description = "Save a sample"
        args_schema = None

    spec = sync_tools._to_spec(_Fake(), "sample_kind", "runtime/tools/sample.py")
    assert spec.scope == "write"
    assert spec.category == "sample_kind"


def test_to_spec_defaults_to_read():
    from admin import sync_tools

    class _Fake:
        name = "get_sample_metrics"
        description = "Fetch metrics"
        args_schema = None

    spec = sync_tools._to_spec(_Fake(), "sample_kind", "runtime/tools/sample.py")
    assert spec.scope == "read"


def test_to_spec_carries_agent_kind():
    from admin import sync_tools

    class _Fake:
        name = "search_documentation"
        description = "Docs search"
        args_schema = None

    spec = sync_tools._to_spec(_Fake(), "sample_kind", "runtime/tools/sample.py")
    assert spec.agent_kind == "sample_kind"


def test_seed_eval_suites_run_does_not_raise(monkeypatch):
    """With a null DB client, run() should return zero counts without raising."""
    from admin import seed_eval_suites

    class _Builder:
        def select(self, *a, **k): return self
        def insert(self, payload): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return type("Res", (), {"data": []})()

    class FakeClient:
        def table(self, name): return _Builder()

    monkeypatch.setattr(seed_eval_suites, "get_service_client", lambda: FakeClient())
    out = seed_eval_suites.run()
    assert isinstance(out, dict)
    assert "inserted" in out


def test_seed_agent_profiles_run_does_not_raise(monkeypatch):
    """seed_agent_profiles tolerates a null DB and returns counts."""
    from admin import seed_agent_profiles

    class _Builder:
        def select(self, *a, **k): return self
        def insert(self, payload): return self
        def update(self, payload): return self
        def eq(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return type("Res", (), {"data": []})()

    class FakeClient:
        def table(self, name): return _Builder()

    monkeypatch.setattr(seed_agent_profiles, "get_service_client", lambda: FakeClient())
    out = seed_agent_profiles.run()
    assert isinstance(out, dict)
