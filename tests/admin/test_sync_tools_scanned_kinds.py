"""A kind whose factory FAILED must not count as scanned.

Review finding: `scanned_kinds` mirrored the loop's filter, not its success.
A factory that raises (transient import error, broken dep) left its kind in
scope, so every tool of that kind was absent from the registry and got
mass-deprecated -- the exact "absent for reasons other than deletion" case
the scoping mechanism was built to stop.
"""
import sys
from types import SimpleNamespace

import admin.sync_tools as st


def test_failing_factory_kind_is_excluded_from_scope(monkeypatch):
    calls = {}

    def _capture_sync(scope_kinds=None):
        calls["scope"] = scope_kinds
        return {"inserted": 0, "updated": 0, "deprecated": 0}

    monkeypatch.setattr(st, "sync_to_db", _capture_sync)
    monkeypatch.setattr(st, "register_tool", lambda spec: None)
    monkeypatch.setattr(st, "_to_spec", lambda tool, kind, src: SimpleNamespace())
    monkeypatch.setattr(st, "active_scope", lambda: None)
    monkeypatch.setattr(st, "_ALL_SCOPE_KINDS", frozenset())

    def _boom():
        raise RuntimeError("factory broke")

    monkeypatch.setitem(
        sys.modules, "fake_good_mod", SimpleNamespace(make=lambda: [object()])
    )
    monkeypatch.setitem(sys.modules, "fake_bad_mod", SimpleNamespace(make=_boom))
    monkeypatch.setattr(st, "_FACTORIES", [
        ("goodkind", "x.py", "fake_good_mod", "make", {}),
        ("badkind", "y.py", "fake_bad_mod", "make", {}),
    ])

    st.run()
    assert calls["scope"] == {"goodkind"}, (
        f"badkind's factory raised, so it must not be scanned: {calls['scope']}"
    )
