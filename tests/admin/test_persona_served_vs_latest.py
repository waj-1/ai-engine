"""The Agents screen must say which persona version a CALLER hears.

Since 20260817121000 the duplex persona is pinned on the parent profile and
frozen into its promote snapshot — the same governance the brain prompt has,
and the same consequence: on a snapshot-served profile a persona saved in the
portal does not reach callers until the parent is promoted. The portal showed
the freshly saved text either way, so "saved" read as "live" when it was not.
The voice-config unification spec calls this display mandatory (Decision 2)
precisely because Part A shipped the governance flip without it.

Two things are pinned here:

* **The mirror.** `admin/_persona_versions.py` duplicates
  `endpoints/voice_live.py::resolve_persona_prompt`'s tier order. A display
  that disagrees with the resolver about which version is served is worse than
  none — it is the same defect class it exists to remove. So the tier-order
  tests do not assert against a hand-written expectation alone: they run the
  REAL resolver over the same fixtures and require both to name the same
  version.
* **The two renders differ.** served == latest and served != latest must not
  produce the same page, and the four tiers must be individually nameable from
  the page — an operator has to know whether promote/rollback governs what they
  are looking at.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from admin._persona_versions import (  # noqa: E402
    TIER_GLOBAL_LATEST,
    TIER_PARENT_PIN,
    TIER_SNAPSHOT,
    TIER_SPECIALIST_PIN,
    collect_persona_version_status,
    resolve_persona_tier,
)
from tests.admin.conftest import client  # noqa: F401,E402 — session superuser TestClient

SLUG = "orchard_voice_persona"
KIND = "orchard"


# --------------------------------------------------------------------------- #
# Fixtures shared by the pure-tier tests and the mirror test.
# --------------------------------------------------------------------------- #
def _parent_row(**over):
    row = {
        "profile_id": "parent-1", "agent_kind": KIND, "slug": "orchard_default",
        "display_name": "Orchard Assistant", "status": "active",
        "prompt_version_ids": {"system": "ver-system"},
        "voice_config": {
            "enabled": True,
            "agent_profile_slug": "orchard_voice",
            "persona_prompt_slug": SLUG,
        },
    }
    row.update(over)
    return row


def _specialist_row(**over):
    row = {
        "profile_id": "voice-1", "agent_kind": KIND, "slug": "orchard_voice",
        "display_name": "Noor — Orchard Voice Call Agent", "status": "specialist",
        "parent_profile_id": "parent-1", "prompt_version_ids": {"system": "ver-brain"},
        "voice_config": {},
    }
    row.update(over)
    return row


def _snapshot(pins=None, contents=None):
    """A promote snapshot shaped like runtime/compiler.py writes one."""
    return {
        "_schema_version": 6,
        "agent_kind": KIND,
        "prompt_version_ids": pins or {},
        "prompt_contents": contents or {},
        "voice_config": {"persona_prompt_slug": SLUG},
    }


def _view(row, snapshot=None, version_number=None, source=None):
    return {
        "row": row, "snapshot": snapshot,
        "snapshot_version_number": version_number, "snapshot_source": source,
    }


# --------------------------------------------------------------------------- #
# 1. Each tier is reported, and reported as the RESOLVER would take it.
# --------------------------------------------------------------------------- #
def test_tier_snapshot_when_the_parents_promote_snapshot_froze_the_text():
    parent = _view(
        _parent_row(),
        _snapshot(pins={SLUG: "ver-3"}, contents={SLUG: "FROZEN"}),
        version_number=12, source="active",
    )
    out = resolve_persona_tier(SLUG, parent, _view(_specialist_row()))

    assert out["tier"] == TIER_SNAPSHOT
    assert out["version_id"] == "ver-3"
    assert out["from_snapshot"] is True


def test_tier_parent_pin_on_a_live_row():
    parent = _view(_parent_row(prompt_version_ids={SLUG: "ver-7"}))
    out = resolve_persona_tier(SLUG, parent, _view(_specialist_row()))

    assert out["tier"] == TIER_PARENT_PIN
    assert out["version_id"] == "ver-7"
    # The live row IS what serves -> a save is live. This bool is the whole
    # dev-vs-prod semantic difference, so it is asserted, not implied.
    assert out["from_snapshot"] is False


def test_tier_parent_pin_read_off_a_snapshot_is_still_frozen_config():
    """A snapshot carrying the pin but no frozen text (a dangling pin at
    promote time) resolves the id LIVE — but repointing the live row's pin
    does not move it, so this must not read as "saved is live"."""
    parent = _view(
        _parent_row(prompt_version_ids={SLUG: "ver-newer"}),
        _snapshot(pins={SLUG: "ver-frozen"}), version_number=4, source="pin",
    )
    out = resolve_persona_tier(SLUG, parent, None)

    assert out["tier"] == TIER_PARENT_PIN
    assert out["version_id"] == "ver-frozen"   # the snapshot's, not the row's
    assert out["from_snapshot"] is True


def test_tier_specialist_pin_is_reported_when_only_the_specialist_carries_one():
    parent = _view(_parent_row())
    specialist = _view(_specialist_row(prompt_version_ids={SLUG: "ver-legacy"}))
    out = resolve_persona_tier(SLUG, parent, specialist)

    assert out["tier"] == TIER_SPECIALIST_PIN
    assert out["version_id"] == "ver-legacy"


def test_parent_pin_outranks_a_specialist_pin():
    """The bug the reversed D5 decision fixed: the specialist's pin used to
    win silently, so a parent-side persona save changed nothing."""
    parent = _view(_parent_row(prompt_version_ids={SLUG: "ver-parent"}))
    specialist = _view(_specialist_row(prompt_version_ids={SLUG: "ver-legacy"}))

    assert resolve_persona_tier(SLUG, parent, specialist)["version_id"] == "ver-parent"


def test_the_specialist_branch_is_skipped_when_it_is_the_same_row():
    """`resolve_persona_prompt` compares profile_id before honouring the
    misplaced pin — a self-served profile must fall to global-latest, not
    report its own pin as a misplaced one."""
    row = _parent_row(prompt_version_ids={})
    out = resolve_persona_tier(SLUG, _view(row), _view(row))

    assert out["tier"] == TIER_GLOBAL_LATEST


def test_tier_global_latest_when_nothing_pins_it():
    out = resolve_persona_tier(SLUG, _view(_parent_row()), _view(_specialist_row()))

    assert out["tier"] == TIER_GLOBAL_LATEST
    assert out["version_id"] is None   # the caller supplies it from the latest lookup


# --------------------------------------------------------------------------- #
# 2. THE MIRROR: the display and endpoints/voice_live.py must agree.
# --------------------------------------------------------------------------- #
def _resolver_client(latest_id="ver-latest"):
    """Supabase-shaped mock for PromptResolver: every version resolves to a
    marker naming its own id, so the returned string identifies the version
    the real resolver picked."""
    from unittest.mock import MagicMock

    client_ = MagicMock()
    state = {"table": None, "filters": {}}

    def _table(name):
        state["table"] = name
        state["filters"] = {}
        return client_

    def _eq(col, val):
        state["filters"][col] = val
        return client_

    def _order(col, desc=False):
        state["filters"][f"order:{col}"] = desc
        return client_

    def _execute():
        f = dict(state["filters"])
        if state["table"] == "agent_prompts":
            return SimpleNamespace(data=[{"prompt_id": "prompt-1"}])
        if "order:version_number" in f:          # slug -> latest version
            return SimpleNamespace(data=[{"prompt_version_id": latest_id}])
        return SimpleNamespace(
            data=[{"content": f"CONTENT FOR {f.get('prompt_version_id')}"}]
        )

    client_.table.side_effect = _table
    client_.select.return_value = client_
    client_.eq.side_effect = _eq
    client_.order.side_effect = _order
    client_.limit.return_value = client_
    client_.execute.side_effect = _execute
    return client_


def _profile_for(view):
    """Build the Profile the runtime loader would build from this view.

    Goes through `AgentProfileLoader._profile_from_snapshot` for the snapshot
    case rather than hand-assembling one, so the mirror is anchored to the real
    loader's snapshot->Profile mapping, not to this test's idea of it.
    """
    from core.agent_profile import AgentProfileLoader, Profile

    if view is None:
        return None
    row = view["row"]
    if view.get("snapshot") is not None:
        return AgentProfileLoader._profile_from_snapshot(row, view["snapshot"], "prod")
    return Profile(
        profile_id=row["profile_id"], agent_kind=row["agent_kind"], slug=row["slug"],
        environment="staging",
        prompt_version_ids=row.get("prompt_version_ids") or {},
    )


_MIRROR_CASES = [
    # (id, parent view, specialist view, the version whose content must come back)
    (
        "snapshot",
        _view(_parent_row(), _snapshot(pins={SLUG: "ver-3"},
                                       contents={SLUG: "CONTENT FOR ver-3"})),
        _view(_specialist_row(prompt_version_ids={SLUG: "ver-legacy"})),
        "ver-3",
    ),
    (
        "parent_pin",
        _view(_parent_row(prompt_version_ids={SLUG: "ver-7"})),
        _view(_specialist_row(prompt_version_ids={SLUG: "ver-legacy"})),
        "ver-7",
    ),
    (
        "specialist_pin",
        _view(_parent_row()),
        _view(_specialist_row(prompt_version_ids={SLUG: "ver-legacy"})),
        "ver-legacy",
    ),
    (
        "global_latest",
        _view(_parent_row()),
        _view(_specialist_row()),
        "ver-latest",
    ),
]


@pytest.mark.parametrize(
    "case_id,parent,specialist,expected_version",
    _MIRROR_CASES, ids=[c[0] for c in _MIRROR_CASES],
)
def test_display_names_the_same_version_the_voice_resolver_serves(
    case_id, parent, specialist, expected_version,
):
    """Run BOTH implementations over one fixture and require agreement.

    Discriminating by construction: the four cases differ only in which rows
    carry a pin, so any reordering of the display's tiers (preferring the
    specialist, ignoring frozen content, checking the live row on a
    snapshot-served profile) makes at least one case name a different version
    than the endpoint actually serves, and that case fails.
    """
    from core import prompt_resolver as pr
    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    pr.PromptResolver.invalidate()
    try:
        cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug=SLUG)
        with patch.object(pr, "get_service_client", return_value=_resolver_client()):
            served_text = resolve_persona_prompt(
                cfg, KIND,
                profile=_profile_for(parent),
                specialist_profile=_profile_for(specialist),
            )
    finally:
        pr.PromptResolver.invalidate()

    display = resolve_persona_tier(SLUG, parent, specialist)
    display_version = display["version_id"] or "ver-latest"  # tier 4 = the latest row

    assert served_text == f"CONTENT FOR {expected_version}"
    assert display_version == expected_version, (
        f"{case_id}: the screen would show {display_version}, the caller hears "
        f"{expected_version}"
    )


# --------------------------------------------------------------------------- #
# 3. collect_persona_version_status: served vs latest, and the env branch.
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Minimal PostgREST-shaped stub for the three tables this module reads."""

    def __init__(self, *, profile_versions=None, prompt_versions=None, prompt_row=True):
        self.profile_versions = profile_versions or []
        self.prompt_versions = prompt_versions or []
        self.prompt_row = prompt_row
        self._table = None
        self._filters = {}

    def table(self, name):
        self._table = name
        self._filters = {}
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, col, desc=False):
        self._filters[f"order:{col}"] = desc
        return self

    def limit(self, _n):
        return self

    def execute(self):
        f = self._filters
        if self._table == "agent_profile_versions":
            rows = self.profile_versions
            if "version_id" in f:
                rows = [r for r in rows if r["version_id"] == f["version_id"]]
            else:
                rows = sorted(
                    [r for r in rows if r.get("profile_id") == f.get("profile_id")],
                    key=lambda r: r["version_number"], reverse=True,
                )
            return SimpleNamespace(data=rows[:1])
        if self._table == "agent_prompts":
            return SimpleNamespace(data=[{"prompt_id": "prompt-1"}] if self.prompt_row else [])
        return SimpleNamespace(data=self.prompt_versions)


_VERSIONS = [
    {"prompt_version_id": "ver-9", "version_number": 9, "created_at": "2026-08-16"},
    {"prompt_version_id": "ver-7", "version_number": 7, "created_at": "2026-08-10"},
    {"prompt_version_id": "ver-3", "version_number": 3, "created_at": "2026-07-01"},
]


def test_served_equals_latest_reports_no_divergence():
    fake = _FakeClient(prompt_versions=_VERSIONS)
    status = collect_persona_version_status(
        fake, environment="staging",
        parent_row=_parent_row(prompt_version_ids={SLUG: "ver-9"}),
        specialist_row=_specialist_row(),
    )

    assert status["tier"] == TIER_PARENT_PIN
    assert status["served"]["version_number"] == 9
    assert status["latest"]["version_number"] == 9
    assert status["diverged"] is False
    assert status["behind_by"] == 0
    assert status["governed"] is True
    assert status["warns_at_connect"] is False


def test_served_behind_latest_is_reported_with_the_gap():
    fake = _FakeClient(prompt_versions=_VERSIONS)
    status = collect_persona_version_status(
        fake, environment="staging",
        parent_row=_parent_row(prompt_version_ids={SLUG: "ver-3"}),
        specialist_row=_specialist_row(),
    )

    assert status["served"]["version_number"] == 3
    assert status["latest"]["version_number"] == 9
    assert status["diverged"] is True
    assert status["behind_by"] == 6


def test_prod_serves_the_snapshot_so_a_save_is_not_live():
    """The dev-vs-prod difference, derived from the loader's own branch: prod
    builds from the newest snapshot for profile+env, dev/staging from the live
    row. Same rows, opposite answer to "is my save live?"."""
    versions = [{
        "version_id": "pv-12", "profile_id": "parent-1", "version_number": 12,
        "snapshot": _snapshot(pins={SLUG: "ver-3"}, contents={SLUG: "FROZEN"}),
    }]
    row = _parent_row(prompt_version_ids={SLUG: "ver-9"})

    prod = collect_persona_version_status(
        _FakeClient(profile_versions=versions, prompt_versions=_VERSIONS),
        environment="prod", parent_row=row, specialist_row=_specialist_row(),
    )
    staging = collect_persona_version_status(
        _FakeClient(profile_versions=versions, prompt_versions=_VERSIONS),
        environment="staging", parent_row=row, specialist_row=_specialist_row(),
    )

    assert prod["tier"] == TIER_SNAPSHOT
    assert prod["served"]["version_number"] == 3
    assert prod["snapshot_version_number"] == 12
    assert prod["save_reaches_callers"] is False
    assert prod["diverged"] is True

    # Same rows, live-served env: the row's own pin serves and a save is live.
    assert staging["tier"] == TIER_PARENT_PIN
    assert staging["served"]["version_number"] == 9
    assert staging["save_reaches_callers"] is True
    assert staging["diverged"] is False


def test_a_served_version_pin_makes_staging_snapshot_served_too():
    """`served_version_id` is honoured in EVERY environment, so "staging =
    saved is live" is not a rule the display may hardcode."""
    versions = [{
        "version_id": "pv-4", "profile_id": "parent-1", "version_number": 4,
        "snapshot": _snapshot(pins={SLUG: "ver-7"}, contents={SLUG: "FROZEN"}),
    }]
    status = collect_persona_version_status(
        _FakeClient(profile_versions=versions, prompt_versions=_VERSIONS),
        environment="staging",
        parent_row=_parent_row(prompt_version_ids={SLUG: "ver-9"},
                               served_version_id="pv-4"),
    )

    assert status["tier"] == TIER_SNAPSHOT
    assert status["served"]["version_number"] == 7
    assert status["snapshot_source"] == "pin"
    assert status["save_reaches_callers"] is False


def test_a_served_version_pin_owned_by_another_profile_is_ignored():
    """The loader treats a cross-profile pin as dangling and falls through;
    reporting its snapshot here would name a version no caller hears."""
    versions = [{
        "version_id": "pv-4", "profile_id": "some-other-profile", "version_number": 4,
        "snapshot": _snapshot(pins={SLUG: "ver-3"}, contents={SLUG: "FROZEN"}),
    }]
    status = collect_persona_version_status(
        _FakeClient(profile_versions=versions, prompt_versions=_VERSIONS),
        environment="staging",
        parent_row=_parent_row(prompt_version_ids={SLUG: "ver-9"},
                               served_version_id="pv-4"),
    )

    assert status["tier"] == TIER_PARENT_PIN
    assert status["served"]["version_number"] == 9


def test_an_unpinned_persona_is_flagged_as_ungoverned_even_though_it_is_latest():
    """Tier 4 always serves the newest version, so "no divergence" is true and
    beside the point — promote and rollback do not control it at all."""
    status = collect_persona_version_status(
        _FakeClient(prompt_versions=_VERSIONS),
        environment="prod", parent_row=_parent_row(), specialist_row=_specialist_row(),
    )

    assert status["tier"] == TIER_GLOBAL_LATEST
    assert status["diverged"] is False
    assert status["governed"] is False
    assert status["warns_at_connect"] is True
    # Ungoverned means live-on-save in EVERY env, prod included.
    assert status["save_reaches_callers"] is True


def test_no_persona_slug_means_nothing_to_display():
    assert collect_persona_version_status(
        _FakeClient(), environment="staging",
        parent_row=_parent_row(voice_config={"enabled": True}),
    ) is None
    assert collect_persona_version_status(
        _FakeClient(), environment="staging", parent_row=None,
    ) is None


def test_a_malformed_voice_config_column_does_not_raise():
    assert collect_persona_version_status(
        _FakeClient(), environment="staging",
        parent_row=_parent_row(voice_config="not-a-dict"),
    ) is None


def test_a_pin_whose_version_row_is_gone_is_reported_as_unnamed():
    """Better an explicit "unnamed" than silently showing the latest as served."""
    status = collect_persona_version_status(
        _FakeClient(prompt_versions=_VERSIONS), environment="staging",
        parent_row=_parent_row(prompt_version_ids={SLUG: "ver-deleted"}),
    )

    assert status["served"] is None
    assert status["served_known"] is False
    assert status["diverged"] is False   # unknown is not "unchanged"


# --------------------------------------------------------------------------- #
# 4. The render: four tiers, and divergence that looks different.
# --------------------------------------------------------------------------- #
def _ctx(active_profile, persona_versions, voice_config_owner=None, prompts=None):
    return {
        "profiles": [active_profile],
        "voice_config_owner": voice_config_owner,
        "persona_versions": persona_versions,
        "live_voice_config": active_profile.get("voice_config") or {},
        "top_level_profiles": [active_profile],
        "sub_profiles_by_parent": {},
        "active_profile": active_profile,
        "profile_versions": [],
        "served_version_id": None,
        "prompts": prompts or [
            {"prompt_id": "prompt-persona-1", "slug": SLUG,
             "content": "أنت جوهرة.", "agent_kind": KIND},
        ],
        "tools": [], "data_sources": [], "mcp_servers": [], "mcp_attachments": [],
        "attachable_mcps": [], "skill_attachments": [], "attachable_skills": [],
        "downstream_candidates": [], "e2e_runs": [],
        "mapped": {"tool_ids": set(), "source_ids": set(),
                   "mcp_ids": set(), "downstream_ids": set()},
        "all_models": [], "realtime_models": [],
        "input_schema": None, "input_validators": {},
    }


def _status(**over):
    base = {
        "slug": SLUG, "agent_kind": KIND, "environment": "prod",
        "tier": TIER_SNAPSHOT,
        "served": {"version_id": "ver-3", "version_number": 3},
        "latest": {"version_id": "ver-9", "version_number": 9},
        "diverged": True, "served_known": True, "behind_by": 6,
        "from_snapshot": True, "save_reaches_callers": False,
        "snapshot_version_number": 12, "snapshot_source": "active",
        "governed": True, "warns_at_connect": False,
        "parent": {"profile_id": "parent-1", "slug": "orchard_default",
                   "display_name": "Orchard Assistant"},
        "specialist": {"profile_id": "voice-1", "slug": "orchard_voice",
                       "display_name": "Noor — Orchard Voice Call Agent"},
        "parent_is_active": True,
    }
    base.update(over)
    return base


def _render(client, ctx):  # noqa: F811
    with patch("admin.screens.agents.load_agents_context", return_value=ctx):
        return client.get("/admin/agents?profile_id=parent-1&kind=orchard&tab=instructions")


def test_snapshot_tier_says_promote_is_what_publishes(client):  # noqa: F811
    r = _render(client, _ctx(_parent_row(), _status()))

    assert r.status_code == 200, r.text[:600]
    assert "callers hear v3" in r.text
    assert "latest saved: v9" in r.text
    assert "promoted" in r.text and "snapshot" in r.text
    assert "v12" in r.text          # the profile version whose snapshot froze it
    assert "Saved is not live here" in r.text
    assert "Promote" in r.text
    assert "6 versions behind" in r.text


def test_parent_pin_on_a_live_row_says_the_save_is_live(client):  # noqa: F811
    r = _render(client, _ctx(_parent_row(), _status(
        tier=TIER_PARENT_PIN, environment="staging",
        served={"version_id": "ver-9", "version_number": 9},
        diverged=False, behind_by=0, from_snapshot=False, save_reaches_callers=True,
        snapshot_version_number=None, snapshot_source=None,
    )))

    assert r.status_code == 200
    assert "callers hear v9" in r.text
    assert "Saved is live here" in r.text
    assert "prompt_version_ids." + SLUG in r.text
    # No divergence, so no call to action about publishing.
    assert "versions behind" not in r.text
    assert "Saved is not live here" not in r.text


def test_served_equals_latest_renders_differently_from_a_divergence(client):  # noqa: F811
    """The one comparison that matters: the ok state and the behind state must
    not be the same page with different numbers."""
    same = _render(client, _ctx(_parent_row(), _status(
        served={"version_id": "ver-9", "version_number": 9},
        diverged=False, behind_by=0,
    ))).text
    behind = _render(client, _ctx(_parent_row(), _status())).text

    assert "chip-ok" in same and "versions behind" not in same
    assert "chip-warn" in behind and "6 versions behind" in behind
    # The divergent render escalates the whole callout, not just a number: the
    # ok state is an info callout, the behind state a warning one.
    assert "alert-warning" in behind and "alert-warning" not in same
    assert "alert-info" in same


def test_specialist_pin_is_named_as_the_wrong_owner(client):  # noqa: F811
    r = _render(client, _ctx(_parent_row(), _status(
        tier=TIER_SPECIALIST_PIN, environment="staging",
        served={"version_id": "ver-legacy", "version_number": 5},
        diverged=True, behind_by=4, from_snapshot=False, save_reaches_callers=True,
        governed=False, warns_at_connect=True,
        snapshot_version_number=None, snapshot_source=None,
    )))

    assert r.status_code == 200
    assert "legacy pin on the specialist" in r.text
    assert "Noor — Orchard Voice Call Agent" in r.text
    assert "warning naming both rows" in r.text
    assert "alert-warning" in r.text


def test_global_latest_is_named_as_ungoverned(client):  # noqa: F811
    r = _render(client, _ctx(_parent_row(), _status(
        tier=TIER_GLOBAL_LATEST, environment="prod",
        served={"version_id": "ver-9", "version_number": 9},
        diverged=False, behind_by=0, from_snapshot=False, save_reaches_callers=True,
        governed=False, warns_at_connect=True,
        snapshot_version_number=None, snapshot_source=None,
    )))

    assert r.status_code == 200
    assert "Nothing pins this persona" in r.text
    assert "promote and rollback do not govern it" in r.text
    # Reads as a problem even though served == latest.
    assert "alert-warning" in r.text


def test_no_persona_means_no_banner(client):  # noqa: F811
    plain = {"profile_id": "p1", "agent_kind": "sales", "slug": "default",
             "display_name": "Sales", "status": "active", "voice_config": {}}
    r = _render(client, _ctx(plain, None, prompts=[]))

    assert r.status_code == 200
    assert "Voice persona" not in r.text


def test_the_specialist_page_shows_the_same_answer(client):  # noqa: F811
    """Viewing the specialist must not hide the served version — the operator
    who opens the row named after the voice agent is the one most likely to
    edit the persona there."""
    owner = {"profile_id": "parent-1",
             "display_name": "Orchard Assistant",
             "live": _parent_row()["voice_config"]}
    r = _render(client, _ctx(_specialist_row(), _status(), voice_config_owner=owner))

    assert r.status_code == 200
    assert "callers hear v3" in r.text
    assert "Saved is not live here" in r.text
