"""Secret-masking tests for admin.env_vars_handlers.

Env var rows can hold secrets. The screen and audit log must never leak a
secret value — `_mask` is the chokepoint, so it gets direct coverage along
with the grouped fetch the Environments screen renders.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin import env_vars_handlers as evh


def test_mask_leaves_non_secret_untouched():
    row = {"name": "REGION", "value": "us-central1", "is_secret": False}
    assert evh._mask(row) == row


def test_mask_replaces_secret_value_with_placeholder():
    row = {"name": "API_KEY", "value": "super-secret", "is_secret": True}
    masked = evh._mask(row)
    assert masked["value"] == evh.SECRET_PLACEHOLDER
    assert masked["value"] != "super-secret"
    assert masked["_masked"] is True
    # Original dict is not mutated (caller may still need the real value).
    assert row["value"] == "super-secret"


def test_mask_secret_with_no_value_stays_none():
    row = {"name": "API_KEY", "value": None, "is_secret": True}
    masked = evh._mask(row)
    assert masked["value"] is None
    assert masked["_masked"] is True


class _FakeEnvVarClient:
    """Honours the (environment) eq-filter the per-env fetch now applies."""

    def __init__(self, rows):
        self._rows = rows
        self._env = None

    def table(self, _name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, _col, value):
        self._env = value
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        rows = [r for r in self._rows if r.get("environment") == self._env]
        return types.SimpleNamespace(data=rows)


def test_fetch_env_vars_by_env_groups_and_masks():
    rows = [
        {"name": "REGION", "value": "us", "is_secret": False, "environment": "staging"},
        {"name": "KEY", "value": "shh", "is_secret": True, "environment": "prod"},
        {"name": "ORPHAN", "value": "v", "is_secret": False, "environment": "qa"},
    ]
    grouped = evh.fetch_env_vars_by_env(_FakeEnvVarClient(rows))
    # ONE dashboard, two managed envs — exactly these buckets.
    assert set(grouped.keys()) == {"staging", "prod"}
    assert grouped["staging"][0]["name"] == "REGION"
    # Secret value masked in the prod bucket.
    assert grouped["prod"][0]["value"] == evh.SECRET_PLACEHOLDER
    # Rows from retired envs (dev/qa) no longer surface anywhere.
    assert all(r["name"] != "ORPHAN" for rs in grouped.values() for r in rs)
