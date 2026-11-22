import pytest

from voice.live.budget import (
    BudgetExhausted,
    check_audio_budget,
    reserve_audio_seconds,
)


class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._filters = {}

    def select(self, *_args):
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        key = (self._filters.get("business_id"), self._filters.get("usage_date"))
        row = self._store.get(key)
        return type("R", (), {"data": [row] if row else []})()

    def upsert(self, row, **_kwargs):
        self._store[(row["business_id"], row["usage_date"])] = row
        self._pending = row
        return self


class _FakeClient:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return _FakeTable(self.store, name)


def test_first_reservation_creates_the_row():
    client = _FakeClient()
    used = reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)
    assert used == 30
    assert client.store[("biz-1", _today())]["seconds_used"] == 30


def test_reservations_accumulate():
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)
    used = reserve_audio_seconds("biz-1", 25, client=client, daily_limit=100)
    assert used == 55


def test_exceeding_the_limit_raises():
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 90, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        reserve_audio_seconds("biz-1", 20, client=client, daily_limit=100)


def test_an_over_cap_charge_is_still_recorded_clamped():
    """The cap-bypass bug: the teardown charge (`elapsed - handshake`) was
    DISCARDED whenever it would exceed the limit, so a 15-minute call recorded
    only its 30s handshake and the caller could reconnect all day."""
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        reserve_audio_seconds("biz-1", 870, client=client, daily_limit=100)
    # The spend happened; the tally must reflect it (clamped at the cap)
    # rather than sitting at 30 forever.
    assert client.store[("biz-1", _today())]["seconds_used"] == 100


def test_a_reconnect_after_an_over_cap_charge_is_refused():
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        reserve_audio_seconds("biz-1", 870, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)


def test_check_audio_budget_is_read_only():
    """The handshake pre-check must not charge: the 30s handshake reservation
    now happens only after the provider session actually starts, so a failing
    setup can't drain the tenant's day via a reconnect loop."""
    client = _FakeClient()
    check_audio_budget("biz-1", client=client, daily_limit=100)
    assert client.store == {}


def test_check_audio_budget_raises_once_the_cap_is_reached():
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 100, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        check_audio_budget("biz-1", client=client, daily_limit=100)


def test_check_audio_budget_fails_open_when_the_db_is_down():
    class _Broken:
        def table(self, _name):
            raise RuntimeError("supabase down")

    check_audio_budget("biz-1", client=_Broken(), daily_limit=100)


def test_tenants_have_separate_budgets():
    client = _FakeClient()
    reserve_audio_seconds("biz-1", 90, client=client, daily_limit=100)
    assert reserve_audio_seconds("biz-2", 90, client=client, daily_limit=100) == 90


def test_a_missing_business_id_is_a_no_op():
    client = _FakeClient()
    assert reserve_audio_seconds("", 30, client=client, daily_limit=100) == 0
    assert client.store == {}


def test_a_db_failure_fails_open_rather_than_killing_the_call():
    class _Broken:
        def table(self, _name):
            raise RuntimeError("supabase down")

    assert reserve_audio_seconds("biz-1", 30, client=_Broken(), daily_limit=100) == 0


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Per-tenant limits + the atomic reservation RPC (migration 20260802140000).
# ---------------------------------------------------------------------------

from voice.live.budget import DEFAULT_DAILY_SECONDS, get_daily_limit


class _FakeBudgetsTable:
    """ai_voice_budgets lookups against a plain {business_id: seconds} dict."""

    def __init__(self, budgets):
        self._budgets = budgets
        self._bid = None

    def select(self, *_args):
        return self

    def eq(self, column, value):
        if column == "business_id":
            self._bid = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._bid in self._budgets:
            row = {"daily_seconds": self._budgets[self._bid]}
            return type("R", (), {"data": [row]})()
        return type("R", (), {"data": []})()


class _FakeRpcClient(_FakeClient):
    """A client whose DB carries the ai_reserve_voice_seconds function."""

    def __init__(self, budgets=None):
        super().__init__()
        self.budgets = budgets or {}
        self.rpc_calls = []
        self.totals = {}

    def table(self, name):
        if name == "ai_voice_budgets":
            return _FakeBudgetsTable(self.budgets)
        return super().table(name)

    def rpc(self, fn, params):
        assert fn == "ai_reserve_voice_seconds"
        self.rpc_calls.append(params)
        bid = params["p_business_id"]
        total = self.totals.get(bid, 0) + params["p_seconds"]
        self.totals[bid] = min(total, params["p_limit"])  # the clamp
        return type("R", (), {"data": total, "execute": lambda s: s})()


def test_get_daily_limit_prefers_the_tenant_row():
    client = _FakeRpcClient(budgets={"biz-9": 120})
    assert get_daily_limit("biz-9", client=client) == 120
    assert get_daily_limit("biz-unknown", client=client) == DEFAULT_DAILY_SECONDS


def test_reserve_uses_the_rpc_and_its_preclamp_total():
    client = _FakeRpcClient()
    assert reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100) == 30
    assert reserve_audio_seconds("biz-1", 50, client=client, daily_limit=100) == 80
    assert client.rpc_calls, "the atomic RPC path was never taken"


def test_reserve_rpc_exhaustion_still_records_the_clamped_tally():
    client = _FakeRpcClient()
    reserve_audio_seconds("biz-1", 90, client=client, daily_limit=100)
    with pytest.raises(BudgetExhausted):
        reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100)
    # The clamp is the whole point: the tally reaches the cap and STAYS there,
    # so the next handshake is refused instead of re-admitted.
    assert client.totals["biz-1"] == 100


def test_reserve_falls_back_to_read_modify_write_without_the_rpc():
    client = _FakeClient()  # no .rpc at all — a DB predating the migration
    assert reserve_audio_seconds("biz-1", 30, client=client, daily_limit=100) == 30
    assert client.store[("biz-1", _today())]["seconds_used"] == 30


def test_per_tenant_limit_drives_check_audio_budget():
    client = _FakeRpcClient(budgets={"biz-9": 10})
    client.store[("biz-9", _today())] = {
        "business_id": "biz-9", "usage_date": _today(), "seconds_used": 10,
    }
    with pytest.raises(BudgetExhausted):
        check_audio_budget("biz-9", client=client)
