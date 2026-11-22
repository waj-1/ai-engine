"""A build must not deprecate another build's tools.

`sync_tools.run()` only registers the kinds the CURRENT build serves (the
active deployment scope's, or every kind no scope owns on a full build).
`sync_to_db` then deprecates every Python tool missing from that registry --
which, unscoped, means a WAJ boot flags all of orchard's tools in a shared AI
project. The orchard agent then resolves to a tool set missing exactly those
and the model calls tools that aren't bound: the recurring Tool-not-found
class of incident. Reproduced for real -- running the test suite (conftest
forces a full build) between two questions of one live voice session
was enough to break the second.
"""
import core.tool_registry as tr


class _FakeTable:
    def __init__(self, store, updates):
        self._store, self._updates = store, updates
        self._payload = None

    def select(self, *_a):
        return self

    def execute(self):
        return type("R", (), {"data": list(self._store)})()

    def update(self, payload):
        self._payload = payload
        return self

    def in_(self, _col, names):
        if self._payload == {"is_deprecated": True}:
            self._updates.extend(names)
        return type("E", (), {"execute": lambda _s=None: None})()

    def eq(self, *_a):
        return type("E", (), {"execute": lambda _s=None: None})()

    def insert(self, _p):
        return type("E", (), {"execute": lambda _s=None: None})()


class _FakeClient:
    def __init__(self, store, updates):
        self._store, self._updates = store, updates

    def table(self, _name):
        return _FakeTable(self._store, self._updates)


def _rows():
    return [
        {"python_name": "get_sales_summary", "kind": "python", "agent_kind": "orchard"},
        {"python_name": "get_bookings", "kind": "python", "agent_kind": "business"},
    ]


def test_waj_boot_does_not_deprecate_orchard_tools(monkeypatch):
    updates = []
    monkeypatch.setattr(tr, "get_service_client", lambda: _FakeClient(_rows(), updates))
    monkeypatch.setattr(tr, "all_specs", lambda: [])
    tr.sync_to_db(scope_kinds={"business"})
    assert "get_sales_summary" not in updates, "a WAJ boot must leave orchard's tools alone"
    assert "get_bookings" in updates, "an in-scope missing tool is still deprecated"


def test_unscoped_call_keeps_the_legacy_behaviour(monkeypatch):
    updates = []
    monkeypatch.setattr(tr, "get_service_client", lambda: _FakeClient(_rows(), updates))
    monkeypatch.setattr(tr, "all_specs", lambda: [])
    tr.sync_to_db()
    assert set(updates) == {"get_sales_summary", "get_bookings"}


def _rows_with_shared():
    return [
        # render_chart: primary harbor, but ALSO serves orchard. Whichever build
        # booted last stamps the primary kind, so scoping by primary alone
        # would let a harbor-only boot deprecate it out from under orchard.
        {"python_name": "render_chart", "kind": "python", "agent_kind": "harbor",
         "additional_agent_kinds": ["orchard"]},
        {"python_name": "harbor_only_tool", "kind": "python", "agent_kind": "harbor",
         "additional_agent_kinds": []},
    ]


def test_shared_tool_is_not_deprecated_unless_every_kind_is_in_scope(monkeypatch):
    """Review finding: a tool built by two builds' factories carries the other
    build's kind in additional_agent_kinds; a harbor boot that no longer builds
    it must NOT deprecate it while orchard still ships it."""
    updates = []
    monkeypatch.setattr(tr, "get_service_client", lambda: _FakeClient(_rows_with_shared(), updates))
    monkeypatch.setattr(tr, "all_specs", lambda: [])
    tr.sync_to_db(scope_kinds={"harbor"})
    assert "render_chart" not in updates, "orchard is out of scope -- leave the shared row alone"
    assert "harbor_only_tool" in updates, "a genuinely harbor-only missing tool still deprecates"
