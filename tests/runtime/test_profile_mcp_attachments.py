"""AgentProfileLoader: agent_profile_mcp_servers -> Profile.mcp_attachments."""


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def execute(self):
        return _Result(self._rows)


class FakeSupabase:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _tables():
    return {
        "agent_profile_mcp_servers": [
            {"profile_id": "p1", "mcp_server_id": "m-bundle", "tool_subset": ["get_clients"], "position": 1},
            {"profile_id": "p1", "mcp_server_id": "m-ext", "tool_subset": None, "position": 0},
            {"profile_id": "other", "mcp_server_id": "m-bundle", "tool_subset": None, "position": 0},
        ],
        "agent_mcp_servers": [
            {"mcp_server_id": "m-bundle", "name": "Business Tools", "source": "bundle",
             "is_active": True, "context_fields": {}},
            {"mcp_server_id": "m-ext", "name": "partner-reports", "source": "external",
             "endpoint": "http://mock-mcp.test/mcp", "is_active": True,
             "context_fields": {"token": "$ctx.user_token"}},
        ],
        "agent_mcp_tools": [
            {"mcp_server_id": "m-bundle", "tool_id": "t1", "position": 0,
             "context_fields": {"business_id": "$ctx.business_id"}},
            {"mcp_server_id": "m-bundle", "tool_id": "t2", "position": 1, "context_fields": None},
        ],
        "agent_tools": [
            {"tool_id": "t1", "python_name": "get_clients", "kind": "python", "is_deprecated": False},
            {"tool_id": "t2", "python_name": "old_tool", "kind": "python", "is_deprecated": True},
        ],
    }


def test_fetch_attachments_ordered_and_denormalized():
    from core.agent_profile import _fetch_mcp_attachments

    atts = _fetch_mcp_attachments(FakeSupabase(_tables()), "p1")
    assert [a["mcp_server"]["name"] for a in atts] == ["partner-reports", "Business Tools"]  # position order
    ext, bundle = atts
    assert ext["member_tools"] == []
    assert ext["tool_subset"] is None
    assert bundle["tool_subset"] == ["get_clients"]
    # deprecated member filtered out; member context_fields carried through
    assert [m["tool"]["python_name"] for m in bundle["member_tools"]] == ["get_clients"]
    assert bundle["member_tools"][0]["context_fields"] == {"business_id": "$ctx.business_id"}


def test_fetch_attachments_swallow_db_failure():
    from core.agent_profile import _fetch_mcp_attachments

    class Exploding:
        def table(self, name):
            raise RuntimeError("boom")

    assert _fetch_mcp_attachments(Exploding(), "p1") == []


def test_profile_default_has_empty_attachments():
    from core.agent_profile import FALLBACK_PROFILE

    assert FALLBACK_PROFILE.mcp_attachments == []
