"""Foundation tests for the runtime package.

These test the building-block behaviour — binding registry, prepare()
applying the profile whitelist + binding filter, compile_profile_snapshot
swallowing exceptions. Per-agent migrations get their own integration
tests as each lands.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))


@pytest.fixture(autouse=True)
def _stub_google_key():
    prev = os.environ.get("GOOGLE_API_KEY")
    os.environ["GOOGLE_API_KEY"] = "test-google"
    yield
    if prev is None:
        os.environ.pop("GOOGLE_API_KEY", None)
    else:
        os.environ["GOOGLE_API_KEY"] = prev


def test_binding_registry_falls_back_to_default():
    from runtime.bindings import get_binding, register_binding, BindingResult

    # No binding registered for this kind → returns default that yields empty result.
    fn = get_binding("nonexistent_agent_kind_xyz")
    result = fn(MagicMock(), {})
    assert isinstance(result, BindingResult)
    assert result.system_addendum == ""
    assert result.tool_filter is None


def test_binding_registry_returns_registered_fn():
    from runtime.bindings import get_binding, register_binding, BindingResult

    def custom_binding(profile, ctx):
        return BindingResult(system_addendum="INJECTED", metadata={"tag": "test"})

    register_binding("test_agent_kind", custom_binding)
    fn = get_binding("test_agent_kind")
    result = fn(MagicMock(), {})
    assert result.system_addendum == "INJECTED"
    assert result.metadata == {"tag": "test"}


def test_runtime_agent_falls_back_when_profile_missing():
    """If the profile loader returns FALLBACK_PROFILE, the runtime
    constructs but model_main is None — _build_llm returns None."""
    from runtime.agent import RuntimeAgent
    from core.agent_profile import FALLBACK_PROFILE

    with patch("runtime.agent.AgentProfileLoader.load_active", return_value=FALLBACK_PROFILE):
        agent = RuntimeAgent("test_kind")
        assert agent.profile.is_fallback
        # llm is None because the fallback profile has no model_main
        assert agent.llm is None


def test_runtime_prepare_applies_mcp_membership_then_binding_filter():
    """prepare() should narrow the tool pool by MCP membership then by binding."""
    from core.agent_profile import Profile
    from runtime.agent import RuntimeAgent
    from runtime.bindings import register_binding, BindingResult

    class _T:
        def __init__(self, name):
            self.name = name

    # A built-in bundle binding a, b, c (d is in the pool but not a member).
    bundle = {
        "mcp_server": {"mcp_server_id": "m1", "name": "__builtin__:prepare_test",
                       "source": "bundle", "context_fields": {}},
        "tool_subset": None,
        "position": 0,
        "member_tools": [
            {"tool": {"tool_id": f"t-{n}", "python_name": n, "kind": "python"},
             "context_fields": None, "position": i}
            for i, n in enumerate(["a", "b", "c"])
        ],
    }
    profile = Profile(
        profile_id="00000000-0000-0000-0000-000000000001",
        agent_kind="prepare_test",
        slug="default",
        environment="dev",
        mcp_attachments=[bundle],
        model_main=None,
    )

    # Binding strips "b".
    def binding(p, ctx):
        return BindingResult(
            tool_filter=lambda tools: [t for t in tools if t.name != "b"],
            system_addendum="EXTRA",
        )

    register_binding("prepare_test", binding)

    with patch("runtime.agent.AgentProfileLoader.load_active", return_value=profile):
        agent = RuntimeAgent("prepare_test")
        all_tools = [_T("a"), _T("b"), _T("c"), _T("d")]  # d is not in profile.
        ready = agent.prepare(
            ctx={"fallback_system_prompt": "BASE"},
            all_tools=all_tools,
        )

    # MCP membership binds a, b, c (d excluded); binding filter then strips "b".
    assert [t.name for t in ready["tools"]] == ["a", "c"]
    # Binding addendum appended.
    assert ready["system_prompt"].endswith("EXTRA")
    assert "BASE" in ready["system_prompt"]


def test_compiler_load_snapshot_returns_none_on_db_failure():
    """load_active_snapshot must swallow any DB exception."""
    from runtime.compiler import load_active_snapshot

    def _boom(*a, **k):
        raise RuntimeError("simulated DB down")

    with patch("runtime.compiler.get_service_client", side_effect=_boom):
        out = load_active_snapshot("00000000-0000-0000-0000-000000000001", "staging")
    assert out is None


def test_compiler_compile_snapshot_raises_when_profile_missing():
    """compile_profile_snapshot raises SnapshotCompileError on missing
    profile — promote handler converts this into a 502 so the admin
    sees the half-promotion instead of getting a silent no-op."""
    from runtime.compiler import compile_profile_snapshot, SnapshotCompileError

    class _Builder:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def insert(self, payload): return self
        def execute(self): return type("Res", (), {"data": []})()

    class FakeClient:
        def table(self, name): return _Builder()

    with patch("runtime.compiler.get_service_client", return_value=FakeClient()):
        with pytest.raises(SnapshotCompileError, match="not found"):
            compile_profile_snapshot(
                profile_id="00000000-0000-0000-0000-000000000001",
                environment="staging",
            )


def test_compiler_snapshot_is_self_contained():
    """Snapshot must denormalize model row, tool python_names, and
    prompt contents — otherwise a target Supabase project that doesn't
    have the dependent rows can't serve the agent."""
    from runtime.compiler import compile_profile_snapshot

    inserted_payload: dict = {}

    class _Builder:
        def __init__(self, table_name):
            self.table_name = table_name
            self._payload = None
            self._select_fields = None
            self._in_ids = None

        def select(self, *a, **k):
            self._select_fields = a
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def in_(self, col, ids):
            self._in_ids = ids
            return self

        def insert(self, payload):
            self._payload = payload
            return self

        def execute(self):
            if self.table_name == "agent_profiles":
                return type("Res", (), {"data": [{
                    "profile_id": "p1",
                    "agent_kind": "sales", "slug": "default",
                    "model_main_id": "m1", "model_light_id": "m2",
                    "tool_ids": ["t1", "t2"],
                    "prompt_version_ids": {"system": "pv1"},
                    "data_source_ids": [],
                    "mcp_server_ids": [],
                    "endpoint_path": "/sales/chat",
                    "downstream_profile_ids": [],
                    "escalation_policy": {},
                }]})()
            if self.table_name == "agent_models":
                return type("Res", (), {"data": [{
                    "model_id": "m1", "name": "primary", "provider": "google_genai",
                    "model_string": "gemini-2.5-pro", "temperature": 0.2,
                }]})()
            if self.table_name == "agent_tools":
                return type("Res", (), {"data": [
                    {"python_name": "search_db"}, {"python_name": "create_booking"},
                ]})()
            if self.table_name == "agent_prompt_versions":
                return type("Res", (), {"data": [
                    {"prompt_version_id": "pv1", "content": "RESOLVED SYSTEM PROMPT"},
                ]})()
            if self.table_name == "agent_profile_versions":
                if self._payload is not None:
                    inserted_payload.update(self._payload)
                    return type("Res", (), {"data": [{"version_id": "v1"}]})()
                return type("Res", (), {"data": []})()
            # Phase 1a: return empty lists for MCP tables so _fetch_mcp_attachments
            # degrades gracefully (best-effort pattern)
            if self.table_name in ("agent_profile_mcp_servers", "agent_mcp_servers",
                                    "agent_mcp_tools"):
                return type("Res", (), {"data": []})()
            return type("Res", (), {"data": []})()

    class FakeClient:
        def table(self, name):
            return _Builder(name)

    with patch("runtime.compiler.get_service_client", return_value=FakeClient()):
        version_id = compile_profile_snapshot(
            profile_id="p1",
            environment="staging",
        )

    assert version_id == "v1"
    snap = inserted_payload["snapshot"]
    # Resolved data is present so the target DB doesn't need lookups.
    assert snap["model_main"]["model_string"] == "gemini-2.5-pro"
    assert sorted(snap["tool_python_names"]) == ["create_booking", "search_db"]
    assert snap["prompt_contents"]["system"] == "RESOLVED SYSTEM PROMPT"
    # Shallow ids retained too for audit.
    assert snap["model_main_id"] == "m1"
    assert snap["agent_kind"] == "sales"
    # Phase 1a: the snapshot must freeze the profile's MCP attachments so prod
    # resolution needs no live join.
    assert "mcp_attachments" in snap
    assert isinstance(snap["mcp_attachments"], list)
    # v5: same freeze posture for platform-skill attachments (agent skills).
    assert "skill_attachments" in snap
    assert isinstance(snap["skill_attachments"], list)
    # v6: delegation config frozen alongside skills.
    assert "downstream_delegates" in snap
    assert isinstance(snap["downstream_delegates"], list)
    assert snap["_schema_version"] == 6


def test_compiler_retries_unique_violation_then_succeeds():
    """If two promotes race on the same (profile, env, version_number),
    the second sees a 23505 and retries with a fresh max."""
    from runtime.compiler import compile_profile_snapshot

    class _APIError(Exception):
        code = "23505"

    state = {"insert_attempts": 0, "current_max": 0}

    class _Builder:
        def __init__(self, table_name):
            self.table_name = table_name
            self._payload = None

        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self

        def insert(self, payload):
            self._payload = payload
            return self

        def execute(self):
            if self.table_name == "agent_profiles":
                return type("Res", (), {"data": [{
                    "agent_kind": "sales", "slug": "default",
                }]})()
            # agent_profile_versions
            if self._payload is not None:
                state["insert_attempts"] += 1
                # First insert collides; second succeeds.
                if state["insert_attempts"] == 1:
                    state["current_max"] = 1
                    raise _APIError("duplicate key value violates unique constraint")
                return type("Res", (), {"data": [{"version_id": "v2"}]})()
            return type("Res", (), {"data": (
                [{"version_number": state["current_max"]}] if state["current_max"] else []
            )})()

    class FakeClient:
        def table(self, name): return _Builder(name)

    with patch("runtime.compiler.get_service_client", return_value=FakeClient()):
        version_id = compile_profile_snapshot(
            profile_id="00000000-0000-0000-0000-000000000001",
            environment="staging",
        )

    assert version_id == "v2"
    assert state["insert_attempts"] == 2  # one collision + one success


def test_compiler_snapshot_carries_data_sources_via_correct_pk():
    """The snapshot must denormalize the profile's data-source (KB) rows,
    parallel to tools/model/prompts, so a target project can serve without a
    live join. Regression guard: agent_data_sources' PK is `source_id`, NOT
    `data_source_id` — probing the wrong column silently returns [] and drops
    every data source from the snapshot."""
    from runtime.compiler import compile_profile_snapshot

    inserted_payload: dict = {}
    probe_cols: dict = {}  # table -> the column name .in_() was called with

    class _Builder:
        def __init__(self, table_name):
            self.table_name = table_name
            self._payload = None

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def in_(self, col, ids):
            probe_cols[self.table_name] = col
            self._in_col = col
            self._in_ids = ids
            return self

        def insert(self, payload):
            self._payload = payload
            return self

        def execute(self):
            if self.table_name == "agent_profiles":
                return type("Res", (), {"data": [{
                    "profile_id": "p1", "agent_kind": "sales", "slug": "default",
                    "model_main_id": "m1", "tool_ids": [],
                    "prompt_version_ids": {},
                    "data_source_ids": ["ds1", "ds2"],
                    "mcp_server_ids": [], "endpoint_path": "/sales/chat",
                    "downstream_profile_ids": [], "escalation_policy": {},
                }]})()
            if self.table_name == "agent_models":
                return type("Res", (), {"data": [{
                    "model_id": "m1", "name": "primary", "provider": "google_genai",
                    "model_string": "gemini-2.5-pro", "temperature": 0.2,
                }]})()
            if self.table_name == "agent_data_sources":
                # Only answer when queried by the REAL PK (source_id). A probe
                # on the wrong column (`data_source_id`) yields nothing → the
                # snapshot would carry no data sources, failing the assert below.
                if getattr(self, "_in_col", None) == "source_id":
                    return type("Res", (), {"data": [
                        {"source_id": "ds1", "name": "WAJ Docs"},
                        {"source_id": "ds2", "name": "Policies"},
                    ]})()
                return type("Res", (), {"data": []})()
            if self.table_name == "agent_profile_versions":
                if self._payload is not None:
                    inserted_payload.update(self._payload)
                    return type("Res", (), {"data": [{"version_id": "v1"}]})()
                return type("Res", (), {"data": []})()
            return type("Res", (), {"data": []})()

    class FakeClient:
        def table(self, name):
            return _Builder(name)

    with patch("runtime.compiler.get_service_client", return_value=FakeClient()):
        compile_profile_snapshot(profile_id="p1", environment="staging")

    # The probe used the correct PK column.
    assert probe_cols.get("agent_data_sources") == "source_id"
    # And the snapshot actually carries the denormalized rows.
    snap = inserted_payload["snapshot"]
    assert [d["source_id"] for d in snap["data_sources"]] == ["ds1", "ds2"]
