"""Tests for core.agent_profile.external_mcp_endpoint_for_kind."""
import core.agent_profile as ap
from core.agent_profile import Profile


def _profile_with_external(endpoint):
    return Profile(
        profile_id="p", agent_kind="sample_partner", slug="sample_partner", environment="dev",
        mcp_attachments=[{
            "mcp_server": {"mcp_server_id": "m", "name": "partner-mock",
                           "source": "external", "endpoint": endpoint},
            "tool_subset": None, "position": 0, "member_tools": [],
        }],
    )


def test_returns_external_row_endpoint(monkeypatch):
    monkeypatch.setattr(ap.AgentProfileLoader, "load_active",
                        classmethod(lambda cls, kind, env=None: _profile_with_external("https://real.test/mcp")))
    monkeypatch.setenv("SAMPLE_PARTNER_MCP_ENDPOINT", "http://env-fallback/mcp")
    out = ap.external_mcp_endpoint_for_kind(
        "sample_partner", env_var="SAMPLE_PARTNER_MCP_ENDPOINT", default="http://localhost:8080/mcp")
    assert out == "https://real.test/mcp"  # row wins over env


def test_falls_back_to_env_when_no_external_attachment(monkeypatch):
    # Profile with only a bundle attachment (no external row) -> env fallback.
    prof = Profile(profile_id="p", agent_kind="sample_partner", slug="sample_partner", environment="dev",
                   mcp_attachments=[{"mcp_server": {"mcp_server_id": "b", "name": "__builtin__:sample_partner",
                                                    "source": "bundle"},
                                     "tool_subset": None, "position": 0, "member_tools": []}])
    monkeypatch.setattr(ap.AgentProfileLoader, "load_active",
                        classmethod(lambda cls, kind, env=None: prof))
    monkeypatch.setenv("SAMPLE_PARTNER_MCP_ENDPOINT", "http://env-fallback/mcp")
    out = ap.external_mcp_endpoint_for_kind(
        "sample_partner", env_var="SAMPLE_PARTNER_MCP_ENDPOINT", default="http://localhost:8080/mcp")
    assert out == "http://env-fallback/mcp"


def test_falls_back_to_default_when_env_unset(monkeypatch):
    prof = Profile(profile_id="p", agent_kind="sample_partner", slug="sample_partner", environment="dev",
                   mcp_attachments=[])
    monkeypatch.setattr(ap.AgentProfileLoader, "load_active",
                        classmethod(lambda cls, kind, env=None: prof))
    monkeypatch.delenv("SAMPLE_PARTNER_MCP_ENDPOINT", raising=False)
    out = ap.external_mcp_endpoint_for_kind(
        "sample_partner", env_var="SAMPLE_PARTNER_MCP_ENDPOINT", default="http://localhost:8080/mcp")
    assert out == "http://localhost:8080/mcp"


def test_profile_load_failure_falls_back_to_env(monkeypatch):
    def _boom(cls, kind, env=None):
        raise RuntimeError("no profile")
    monkeypatch.setattr(ap.AgentProfileLoader, "load_active", classmethod(_boom))
    monkeypatch.setenv("SAMPLE_PARTNER_MCP_ENDPOINT", "http://env-fallback/mcp")
    out = ap.external_mcp_endpoint_for_kind(
        "sample_partner", env_var="SAMPLE_PARTNER_MCP_ENDPOINT", default="http://localhost:8080/mcp")
    assert out == "http://env-fallback/mcp"
