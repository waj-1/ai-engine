# tests/admin/test_promotion_service.py
"""promote(): eval gate, force override auditing, sibling remap, snapshot."""
import pytest
from unittest.mock import MagicMock, patch

from admin.services import promotion
from admin.services.promotion import EvalGateFailed, ProfileNotFound, promote

PROFILE = {"profile_id": "p1", "agent_kind": "business", "slug": "main",
           "environment": "staging", "display_name": "Biz",
           "parent_profile_id": None, "downstream_profile_ids": []}


def _client_with(rows_by_call):
    """MagicMock supabase client: .table(name) returns a fluent builder whose
    execute() pops the next canned response for that table."""
    client = MagicMock()

    def table(name):
        builder = MagicMock()
        for m in ("select", "eq", "in_", "order", "limit", "insert", "update"):
            getattr(builder, m).return_value = builder
        data = rows_by_call.get(name, [])
        builder.execute.return_value = MagicMock(data=data)
        return builder

    client.table.side_effect = table
    return client


def test_profile_not_found():
    with patch.object(promotion, "promote", wraps=promote):
        with patch("core.auth.admin_service_client",
                   return_value=_client_with({"agent_profiles": []})):
            with pytest.raises(ProfileNotFound):
                promote(profile_id="missing", target_env="prod", force=False,
                        actor_user_id="u1", actor_email="a@company.example")


def test_eval_gate_blocks_below_threshold():
    client = _client_with({"agent_profiles": [PROFILE]})
    with patch("core.auth.admin_service_client", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile",
                      return_value={"status": "below_threshold", "score": 0.4,
                                    "threshold": 0.7, "suite_name": "s"}):
        with pytest.raises(EvalGateFailed) as exc:
            promote(profile_id="p1", target_env="prod", force=False,
                    actor_user_id="u1", actor_email="a@company.example")
    assert exc.value.detail["error"] == "eval_gate_failed"
    assert exc.value.detail["score"] == 0.4


def test_force_promotes_and_audits_override(monkeypatch):
    client = _client_with({"agent_profiles": [PROFILE]})
    audits = []
    monkeypatch.setattr(promotion, "record_change",
                        lambda **kw: audits.append(kw))
    with patch("core.auth.admin_service_client", return_value=client), \
         patch("core.auth.get_service_client_for_env", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile",
                      return_value={"status": "below_threshold", "score": 0.4,
                                    "threshold": 0.7, "suite_name": "s"}), \
         patch.object(promotion, "_upsert_target_profile", return_value="p2"), \
         patch("runtime.compiler.compile_profile_snapshot"):
        out = promote(profile_id="p1", target_env="prod", force=True,
                      actor_user_id="u1", actor_email="a@company.example")
    assert out["profile_id"] == "p2"
    assert out["forced"] is True
    assert any(a["action"] == "EVAL_GATE_OVERRIDE" for a in audits)
    assert any(a["action"] == "CONFIG_PROMOTE" for a in audits)


# --------------------------------------------------------------------------
# prompt-vs-bound-tools gate (#1032 recurrence guard)
# --------------------------------------------------------------------------

def test_prompt_tool_guard_blocks_when_prompt_mandates_unbound_tool():
    from admin.services.promotion import PromptToolMismatch
    client = _client_with({"agent_profiles": [PROFILE]})
    with patch("core.auth.admin_service_client", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile", return_value=None), \
         patch.object(promotion, "_prompt_mandated_unbound_tools",
                      return_value=["search_documentation"]):
        with pytest.raises(PromptToolMismatch) as exc:
            promote(profile_id="p1", target_env="prod", force=False,
                    actor_user_id="u1", actor_email="a@company.example")
    assert exc.value.detail["error"] == "prompt_tool_mismatch"
    assert exc.value.detail["missing_tools"] == ["search_documentation"]


def test_prompt_tool_guard_force_overrides_and_audits(monkeypatch):
    client = _client_with({"agent_profiles": [PROFILE]})
    audits = []
    monkeypatch.setattr(promotion, "record_change", lambda **kw: audits.append(kw))
    with patch("core.auth.admin_service_client", return_value=client), \
         patch("core.auth.get_service_client_for_env", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile", return_value=None), \
         patch.object(promotion, "_prompt_mandated_unbound_tools",
                      return_value=["search_documentation"]), \
         patch.object(promotion, "_upsert_target_profile", return_value="p2"), \
         patch("runtime.compiler.compile_profile_snapshot"):
        out = promote(profile_id="p1", target_env="prod", force=True,
                      actor_user_id="u1", actor_email="a@company.example")
    assert out["forced"] is True
    assert any(a["action"] == "PROMPT_TOOL_MISMATCH_OVERRIDE" for a in audits)
    assert any(a["action"] == "CONFIG_PROMOTE" for a in audits)


def test_prompt_mandated_unbound_tools_reads_attachments_and_prompt():
    """Wiring: fetch frozen attachments + system prompt → flag the mismatch."""
    src = {"profile_id": "p1", "prompt_version_ids": {"system": "v-sys"}}
    attachments = [{
        "mcp_server": {"name": "b", "source": "bundle"},
        "tool_subset": ["get_bookings"],  # docs omitted from allowlist
        "member_tools": [
            {"tool": {"python_name": "get_bookings", "kind": "python"}},
            {"tool": {"python_name": "search_documentation", "kind": "python"}},
        ],
    }]
    with patch("core.agent_profile._fetch_mcp_attachments", return_value=attachments), \
         patch("runtime.compiler._fetch_prompt_contents",
               return_value={"system": "For HOW-TO you MUST call `search_documentation`."}):
        assert promotion._prompt_mandated_unbound_tools(MagicMock(), src) == [
            "search_documentation"]


def test_prompt_tool_guard_check_failure_is_non_blocking():
    """An infra error inside the guard check must not block promotes."""
    src = {"profile_id": "p1", "prompt_version_ids": {}}
    with patch("core.agent_profile._fetch_mcp_attachments",
               side_effect=RuntimeError("db down")):
        assert promotion._prompt_mandated_unbound_tools(MagicMock(), src) == []


# --------------------------------------------------------------------------
# cross-env sibling remap — parent + same-kind delegate map to DISTINCT
# target siblings (no self-reference; delegate id is remapped, not dropped)
# --------------------------------------------------------------------------

def _remap_client(source_refs, target_rows):
    """Fake supabase client: `.in_("profile_id", …)` returns the source-ref
    rows; `.eq("environment", …)` returns the target-env rows."""
    client = MagicMock()

    def table(_name):
        builder = MagicMock()
        state = {"mode": "target"}
        for m in ("select", "order", "limit", "insert", "update"):
            getattr(builder, m).return_value = builder

        def _in(_col, _vals):
            state["mode"] = "source"
            return builder

        def _eq(_col, _val):
            return builder

        builder.in_.side_effect = _in
        builder.eq.side_effect = _eq
        builder.execute.side_effect = lambda: MagicMock(
            data=source_refs if state["mode"] == "source" else target_rows)
        return builder

    client.table.side_effect = table
    return client


def test_remap_same_kind_delegate_maps_to_its_own_sibling_not_self():
    # staging: main (business/default) with downstream = title + a same-kind
    # delegate (business/business_reporting). prod has all three siblings.
    source_refs = [
        {"profile_id": "s-title", "agent_kind": "business_title", "slug": "default"},
        {"profile_id": "s-spec", "agent_kind": "business", "slug": "business_reporting"},
    ]
    target_rows = [
        {"profile_id": "p-main", "agent_kind": "business", "slug": "default"},
        {"profile_id": "p-title", "agent_kind": "business_title", "slug": "default"},
        {"profile_id": "p-spec", "agent_kind": "business", "slug": "business_reporting"},
    ]
    client = _remap_client(source_refs, target_rows)
    src = {"parent_profile_id": None,
           "downstream_profile_ids": ["s-title", "s-spec"]}
    new_row = dict(src)
    promotion._remap_cross_env_refs(client, client, "prod", src, new_row)
    # delegate maps to the prod SPECIALIST (p-spec), NOT the prod main — the
    # agent_kind-only bug collapsed it onto whichever business row came last.
    assert new_row["downstream_profile_ids"] == ["p-title", "p-spec"]
    assert "p-main" not in new_row["downstream_profile_ids"]  # no self-reference


def test_remap_keeps_id_when_no_target_sibling_yet():
    source_refs = [{"profile_id": "s-spec", "agent_kind": "business",
                    "slug": "business_reporting"}]
    target_rows = []  # delegate not promoted to prod yet
    client = _remap_client(source_refs, target_rows)
    src = {"parent_profile_id": None, "downstream_profile_ids": ["s-spec"]}
    new_row = dict(src)
    promotion._remap_cross_env_refs(client, client, "prod", src, new_row)
    assert new_row["downstream_profile_ids"] == ["s-spec"]  # kept, not dropped


def test_force_reason_is_recorded_on_override_and_promote(monkeypatch):
    """The portal's confirm dialog sends a reason when it forces past the
    quality gate; it lands on the override row and the CONFIG_PROMOTE row."""
    client = _client_with({"agent_profiles": [PROFILE]})
    audits = []
    monkeypatch.setattr(promotion, "record_change", lambda **kw: audits.append(kw))
    with patch("core.auth.admin_service_client", return_value=client), \
         patch("core.auth.get_service_client_for_env", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile",
                      return_value={"status": "below_threshold", "score": 0.4,
                                    "threshold": 0.7, "suite_name": "s"}), \
         patch.object(promotion, "_upsert_target_profile", return_value="p2"), \
         patch("runtime.compiler.compile_profile_snapshot"):
        promote(profile_id="p1", target_env="prod", force=True,
                actor_user_id="u1", actor_email="a@company.example", reason="  flaky judge  ")
    override = next(a for a in audits if a["action"] == "EVAL_GATE_OVERRIDE")
    assert override["after"]["reason"] == "flaky judge"
    promote_row = next(a for a in audits if a["action"] == "CONFIG_PROMOTE")
    assert promote_row["after"]["_reason"] == "flaky judge"


def test_promote_without_reason_keeps_old_audit_shape(monkeypatch):
    client = _client_with({"agent_profiles": [PROFILE]})
    audits = []
    monkeypatch.setattr(promotion, "record_change", lambda **kw: audits.append(kw))
    with patch("core.auth.admin_service_client", return_value=client), \
         patch("core.auth.get_service_client_for_env", return_value=client), \
         patch.object(promotion, "_latest_eval_for_profile",
                      return_value={"status": "below_threshold", "score": 0.4,
                                    "threshold": 0.7, "suite_name": "s"}), \
         patch.object(promotion, "_upsert_target_profile", return_value="p2"), \
         patch("runtime.compiler.compile_profile_snapshot"):
        promote(profile_id="p1", target_env="prod", force=True,
                actor_user_id="u1", actor_email="a@company.example")
    override = next(a for a in audits if a["action"] == "EVAL_GATE_OVERRIDE")
    assert "reason" not in override["after"]
    promote_row = next(a for a in audits if a["action"] == "CONFIG_PROMOTE")
    assert "_reason" not in promote_row["after"]
