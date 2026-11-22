"""Regression test for Defect A (Task 13 review): `resolve_persona_prompt`
used to pass `cfg.persona_prompt_slug` (a STRING slug, e.g.
"sample_voice_persona") as `PromptResolver.resolve`'s first positional
argument, which expects a `prompt_version_id` UUID —
`core/prompt_resolver.py::PromptResolver._fetch_or_fallback` queries
`agent_prompt_versions.prompt_version_id` with it. With no `fallback`
supplied, that produced a `ValueError` (not caught by
`endpoints/voice_live.py`'s `except (LookupError, HTTPException)` around the
call site) — duplex voice crashed at connect the moment a tenant's
`persona_prompt_slug` was actually set.

Every test in `tests/voice/test_voice_live_endpoint.py` monkeypatches
`resolve_persona_prompt` itself (see its `harness` fixture), which is
exactly why this bug survived the whole endpoint suite. This test drives
`resolve_persona_prompt` for real and stubs only the Supabase-client
boundary (`core.prompt_resolver.get_service_client`), so the actual
slug -> agent_prompts -> latest agent_prompt_versions -> content path gets
exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(AI_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AI_AGENT_DIR))

from core import prompt_resolver as pr
from core.agent_profile import Profile
from endpoints.voice_live import resolve_persona_prompt
from voice.live.config import LiveVoiceConfig


@pytest.fixture(autouse=True)
def _reset_resolver_cache():
    """The resolver caches content by prompt_version_id at class scope, and
    the prompt-override ContextVar is shared process state — isolate each
    test from the others (mirrors tests/runtime/test_prompt_resolver.py's
    fixture)."""
    pr.clear_prompt_override()
    pr.PromptResolver.invalidate()
    yield
    pr.clear_prompt_override()
    pr.PromptResolver.invalidate()


def _table_router(handlers):
    """Chainable Supabase-style mock whose `.execute().data` is decided by a
    per-table handler. Each `.table(name)` call starts a fresh filter dict so
    one client instance can serve the agent_prompts lookup and both
    agent_prompt_versions lookups (latest-version, then content-by-id) in a
    single `resolve_by_slug()` call.
    """
    client = MagicMock()
    state = {"table": None, "filters": {}}

    def _table(name):
        state["table"] = name
        state["filters"] = {}
        return client

    def _eq(col, val):
        state["filters"][col] = val
        return client

    def _order(col, desc=False):
        state["filters"][f"order:{col}"] = desc
        return client

    def _execute():
        handler = handlers[state["table"]]
        rows = handler(dict(state["filters"]))
        return SimpleNamespace(data=rows)

    client.table.side_effect = _table
    client.select.return_value = client
    client.eq.side_effect = _eq
    client.order.side_effect = _order
    client.limit.return_value = client
    client.execute.side_effect = _execute
    return client


def test_resolve_persona_prompt_follows_slug_to_active_version_content():
    """The real bug: `cfg.persona_prompt_slug` must resolve through
    agent_prompts(agent_kind, slug) -> the latest agent_prompt_versions row
    -> its content — NOT be handed to PromptResolver.resolve() as if it were
    already a prompt_version_id. Fails against the pre-fix code because the
    unfixed `resolve_persona_prompt` queries agent_prompt_versions with
    `prompt_version_id == "sample_voice_persona"` directly, which never
    matches this mock's `ver-active` row.
    """

    def _prompts(filters):
        assert filters == {"agent_kind": "sample_voice", "slug": "sample_voice_persona"}
        return [{"prompt_id": "prompt-sample-voice"}]

    def _versions(filters):
        if "order:version_number" in filters:
            # Slug -> latest-version lookup.
            assert filters == {
                "prompt_id": "prompt-sample-voice",
                "order:version_number": True,
            }
            return [{"prompt_version_id": "ver-active"}]
        # Content fetch for the resolved active version id.
        assert filters == {"prompt_version_id": "ver-active"}
        return [{"content": "SEEDED PERSONA CONTENT"}]

    client = _table_router({"agent_prompts": _prompts, "agent_prompt_versions": _versions})

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with patch.object(pr, "get_service_client", return_value=client):
        out = resolve_persona_prompt(cfg, "sample_voice")

    assert out == "SEEDED PERSONA CONTENT"


def test_resolve_persona_prompt_raises_lookup_error_when_slug_unset():
    """Existing behaviour preserved: an unset persona_prompt_slug is a
    misconfigured tenant, not a silent default — must still raise
    LookupError (the call site in voice_live.py closes the socket with
    CLOSE_FORBIDDEN on exactly this type)."""
    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug=None)
    with pytest.raises(LookupError):
        resolve_persona_prompt(cfg, "sample_voice")


def test_resolve_persona_prompt_raises_lookup_error_when_no_prompt_row_exists():
    """A slug with no matching agent_prompts row (never seeded, or a typo'd
    operator entry) must raise LookupError too — same "misconfigured tenant"
    bucket, not an uncaught DB-shaped exception."""

    def _prompts(_filters):
        return []

    client = _table_router({"agent_prompts": _prompts, "agent_prompt_versions": lambda f: []})

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="missing_slug")
    with patch.object(pr, "get_service_client", return_value=client):
        with pytest.raises(LookupError):
            resolve_persona_prompt(cfg, "sample_voice")


# ---------------------------------------------------------------------------
# Review finding IMPORTANT 3: the persona prompt must obey the same
# promote/rollback governance every other prompt does (profile snapshot /
# pin), not always serve slug-latest.
# ---------------------------------------------------------------------------


def test_resolve_persona_prompt_prefers_the_profiles_resolved_snapshot():
    """Compile-on-promote governance: a profile's frozen prod snapshot
    (`resolved_prompts`) must win over slug-latest — otherwise saving a
    draft persona version in the portal ships to every prod voice caller on
    the very next connect, with no promote step at all."""
    profile = Profile(
        profile_id="p-1", agent_kind="sample_voice", slug="sample_default", environment="prod",
        resolved_prompts={"sample_voice_persona": "SNAPSHOT PERSONA"},
    )
    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")

    boom = MagicMock()
    boom.table.side_effect = AssertionError(
        "must not query the DB when the profile snapshot already has the content"
    )
    with patch.object(pr, "get_service_client", return_value=boom):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=profile)

    assert out == "SNAPSHOT PERSONA"


def test_resolve_persona_prompt_prefers_the_profiles_pinned_version_over_latest():
    """An operator's explicit `prompt_version_ids` pin (dev/staging) must
    win over slug-latest — otherwise rolling a profile back to an older
    pinned persona version would be a silent no-op for voice."""
    profile = Profile(
        profile_id="p-1", agent_kind="sample_voice", slug="sample_default", environment="staging",
        prompt_version_ids={"sample_voice_persona": "ver-pinned"},
    )
    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")

    def _versions(filters):
        assert filters == {"prompt_version_id": "ver-pinned"}
        return [{"content": "PINNED CONTENT"}]

    # No "agent_prompts" handler at all: a pinned version must be resolved
    # directly (PromptResolver.resolve), never via the slug -> agent_prompts
    # lookup — hitting that table would KeyError against this router.
    client = _table_router({"agent_prompt_versions": _versions})
    with patch.object(pr, "get_service_client", return_value=client):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=profile)

    assert out == "PINNED CONTENT"


def test_resolve_persona_prompt_falls_back_to_slug_latest_when_profile_has_no_entry():
    """A profile that doesn't (yet) carry an entry for this slug — a fresh
    tenant, or one whose `prompt_version_ids` predates this slug being
    seeded — must still fall back to slug -> latest version, not raise or
    silently return nothing."""
    profile = Profile(
        profile_id="p-1", agent_kind="sample_voice", slug="sample_default", environment="staging",
        resolved_prompts={}, prompt_version_ids={},
    )
    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")

    def _prompts(filters):
        assert filters == {"agent_kind": "sample_voice", "slug": "sample_voice_persona"}
        return [{"prompt_id": "prompt-sample-voice"}]

    def _versions(filters):
        if "order:version_number" in filters:
            return [{"prompt_version_id": "ver-active"}]
        assert filters == {"prompt_version_id": "ver-active"}
        return [{"content": "SEEDED PERSONA CONTENT"}]

    client = _table_router({"agent_prompts": _prompts, "agent_prompt_versions": _versions})
    with patch.object(pr, "get_service_client", return_value=client):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=profile)

    assert out == "SEEDED PERSONA CONTENT"


# ---------------------------------------------------------------------------
# Review finding MINOR 5: a transient DB/transport blip must not be
# relabeled as "misconfigured tenant" (LookupError).
# ---------------------------------------------------------------------------


def test_resolve_persona_prompt_propagates_a_transient_db_error_as_itself():
    """`endpoints/voice_live.py`'s call site maps LookupError -> CLOSE_FORBIDDEN
    ("misconfigured tenant") and anything else -> CLOSE_PROVIDER_ERROR ("not
    the caller's fault") — mirroring how `_live_config_for` already splits
    the same two cases 40 lines earlier in that file. A DB/transport error
    while resolving the slug must propagate AS ITSELF (here, a bare
    RuntimeError), not get relabeled LookupError, or the split is defeated."""

    class _BoomClient:
        def table(self, _name):
            raise RuntimeError("connection reset")

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with patch.object(pr, "get_service_client", return_value=_BoomClient()):
        with pytest.raises(RuntimeError):
            resolve_persona_prompt(cfg, "sample_voice")


# ---------------------------------------------------------------------------
# Review finding MINOR 6: `prompt_key` must default to the real slug so an
# eval run can actually pin the persona.
# ---------------------------------------------------------------------------


def test_resolve_persona_prompt_prompt_key_lets_eval_pin_the_persona_by_version():
    """Regression: the old call site hardcoded `prompt_key="voice_persona"`,
    an unrelated literal — the real `agent_prompts.slug` is
    "sample_voice_persona". `_lookup_override_version_id` matches the
    override on `(agent_kind, prompt_key)` treated as `(agent_kind, slug)`,
    so the mismatched literal meant `X-WAJ-Prompt-Override:
    {"sample_voice/sample_voice_persona": vN}` could never match — the persona
    could never be eval-pinned. `resolve_by_slug`'s own `prompt_key or slug`
    default (now actually reached, since the call site no longer overrides
    it) fixes this."""

    def _prompts(filters):
        assert filters == {"agent_kind": "sample_voice", "slug": "sample_voice_persona"}
        return [{"prompt_id": "prompt-sample-voice"}]

    def _versions(filters):
        if filters == {"prompt_id": "prompt-sample-voice", "version_number": 3}:
            return [{"prompt_version_id": "ver-3"}]
        if "order:version_number" in filters:
            return [{"prompt_version_id": "ver-active"}]
        if filters == {"prompt_version_id": "ver-3"}:
            return [{"content": "PINNED v3 PERSONA"}]
        raise AssertionError(f"unexpected agent_prompt_versions filters: {filters}")

    client = _table_router({"agent_prompts": _prompts, "agent_prompt_versions": _versions})

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    pr.set_prompt_override({"sample_voice/sample_voice_persona": 3})
    with patch.object(pr, "get_service_client", return_value=client):
        out = resolve_persona_prompt(cfg, "sample_voice")

    assert out == "PINNED v3 PERSONA"


def test_speech_locale_maps_the_portal_choice():
    """Saudi merchants: `ar` must pin to ar-SA, not a generic Arabic the
    provider can render in any dialect."""
    from endpoints.voice_live import speech_locale

    assert speech_locale("ar") == ("ar-SA", "Arabic, Saudi dialect")
    assert speech_locale("en") == ("en-US", "English")
    assert speech_locale("ar-SA") == ("ar-SA", "Arabic, Saudi dialect")
    # A full tag is the caller's ACCENT choice (the company.example demo picker) and
    # keeps its region — including regions the names map doesn't know.
    assert speech_locale("ar-EG") == ("ar-EG", "Arabic, Egyptian dialect")
    assert speech_locale("en-GB") == ("en-GB", "English, British accent")
    assert speech_locale("ar-TN") == ("ar-TN", "Arabic (ar-TN dialect)")
    # Unknown or blank returns None so the caller can tell "no portal choice"
    # from a real choice -- ONLY a real choice may override the operator's
    # voice_config.live.language knob (review finding: the old always-English
    # fallback clobbered it on every connection without ?locale).
    assert speech_locale(None) is None
    assert speech_locale("fr") is None


def test_language_name_names_accents_and_falls_back_to_the_raw_tag():
    from endpoints.voice_live import language_name

    assert language_name("ar-SA") == "Arabic, Saudi dialect"
    assert language_name("ar-EG") == "Arabic, Egyptian dialect"
    # Unlisted region of a known language: coherent generic name.
    assert language_name("ar-TN") == "Arabic (ar-TN dialect)"
    # Unknown language entirely: the raw tag, unambiguous if ugly.
    assert language_name("fr-FR") == "fr-FR"


def test_persona_binds_the_language_placeholder(monkeypatch):
    import endpoints.voice_live as live_ep
    from types import SimpleNamespace

    profile = SimpleNamespace(
        resolved_prompts={"sample_voice_persona": "Speak {language}, and only {language}."},
        prompt_version_ids={},
    )
    out = live_ep.resolve_persona_prompt(
        SimpleNamespace(persona_prompt_slug="sample_voice_persona"),
        "sample_voice", profile=profile, language="Arabic, Saudi dialect",
    )
    assert out == "Speak Arabic, Saudi dialect, and only Arabic, Saudi dialect."


def test_persona_row_without_the_placeholder_still_renders(monkeypatch):
    """A persona authored before {language} existed must not start raising."""
    import endpoints.voice_live as live_ep
    from types import SimpleNamespace

    profile = SimpleNamespace(
        resolved_prompts={"sample_voice_persona": "Be brief."}, prompt_version_ids={}
    )
    out = live_ep.resolve_persona_prompt(
        SimpleNamespace(persona_prompt_slug="sample_voice_persona"),
        "sample_voice", profile=profile, language="English",
    )
    assert out == "Be brief."


def test_unpinned_persona_falls_back_to_global_latest_and_warns(caplog, monkeypatch):
    """The fallback is legitimate (a tenant may genuinely have no pin) but it
    means promote/rollback do not govern what the caller hears, so it must be
    visible rather than silent."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    calls = {}

    class _Resolver:
        @staticmethod
        def resolve_by_slug(slug, variables=None, agent_kind=None):
            calls["slug"] = slug
            return "PERSONA FROM GLOBAL LATEST"

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    class _Profile:
        resolved_prompts = {}
        prompt_version_ids = {"system": "ver-brain"}  # no persona entry
        slug = "sample_voice_specialist"

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=_Profile())

    assert out == "PERSONA FROM GLOBAL LATEST"
    assert calls["slug"] == "sample_voice_persona"
    assert any("not pinned" in r.message for r in caplog.records), (
        "an ungoverned persona must warn"
    )


# ---------------------------------------------------------------------------
# 2026-08-17: the persona pin moved from the SPECIALIST to the PARENT profile.
#
# The portal's persona editor posts its version repoint to the ACTIVE profile's
# profile_id and every doc tells operators to edit the parent, so resolving the
# pin off the specialist made that save a silent no-op: the portal showed the
# new persona, the caller heard the specialist's older pin, and the ungoverned
# warning never fired because that stale pin satisfied the governed branch.
# ---------------------------------------------------------------------------


def test_parent_pin_is_honoured_over_a_stale_specialist_pin(caplog, monkeypatch):
    """THE regression. Both profiles carry a pin for the persona slug; the
    PARENT's must win, and nothing must warn (the parent pin is governed).

    Discriminates: with the resolver reading the specialist first, this returns
    "STALE SPECIALIST PERSONA" — exactly the live symptom (an operator's portal
    save inert, the previous version still spoken).
    """
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    contents = {"ver-parent": "PARENT PINNED PERSONA", "ver-specialist": "STALE SPECIALIST PERSONA"}

    class _Resolver:
        @staticmethod
        def resolve(version_id, variables=None, agent_kind=None, prompt_key=None):
            return contents[version_id]

        @staticmethod
        def resolve_by_slug(slug, variables=None, agent_kind=None):
            raise AssertionError("must not fall back to global-latest with a parent pin present")

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    parent = SimpleNamespace(
        profile_id="p-parent", slug="sample_default",
        resolved_prompts={}, prompt_version_ids={"sample_voice_persona": "ver-parent"},
    )
    specialist = SimpleNamespace(
        profile_id="p-voice", slug="sample_voice_specialist",
        resolved_prompts={}, prompt_version_ids={"sample_voice_persona": "ver-specialist"},
    )

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(
            cfg, "sample_voice", profile=parent, specialist_profile=specialist
        )

    assert out == "PARENT PINNED PERSONA"
    warnings = _voice_live_warnings(caplog)
    assert not warnings, f"the governed (parent-pin) path must not warn: {warnings}"


def test_specialist_only_pin_is_honoured_and_warns(caplog, monkeypatch):
    """A MISPLACED pin (on the specialist, not the parent) keeps working — but
    VISIBLY. It is reachable: the Agents screen's persona editor posts the
    profile being VIEWED, so saving it while looking at the specialist pins it
    there. Silently preferring the specialist is the bug; ignoring it outright
    would regress that tenant to global-latest.
    """
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    class _Resolver:
        @staticmethod
        def resolve(version_id, variables=None, agent_kind=None, prompt_key=None):
            assert version_id == "ver-specialist"
            return "MISPLACED SPECIALIST PERSONA"

        @staticmethod
        def resolve_by_slug(slug, variables=None, agent_kind=None):
            raise AssertionError("a misplaced specialist pin must be used, not global-latest")

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    parent = SimpleNamespace(
        profile_id="p-parent", slug="sample_default",
        resolved_prompts={}, prompt_version_ids={"system": "ver-brain"},
    )
    specialist = SimpleNamespace(
        profile_id="p-voice", slug="sample_voice_specialist",
        resolved_prompts={}, prompt_version_ids={"sample_voice_persona": "ver-specialist"},
    )

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(
            cfg, "sample_voice", profile=parent, specialist_profile=specialist
        )

    assert out == "MISPLACED SPECIALIST PERSONA"
    messages = [r.getMessage() for r in _voice_live_warnings(caplog)]
    assert messages, "a specialist-only pin must warn, not resolve silently"
    joined = " ".join(messages)
    # Names BOTH profiles so an operator can see which row to migrate.
    assert "sample_voice_specialist" in joined and "sample_default" in joined, joined


def test_specialist_snapshot_is_honoured_and_warns(caplog, monkeypatch):
    """Same misplaced-pin case, promoted shape: a specialist promoted while it
    held the pin carries the persona in its frozen `resolved_prompts`."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    class _Resolver:
        @staticmethod
        def resolve_by_slug(slug, variables=None, agent_kind=None):
            raise AssertionError("a misplaced specialist snapshot must be used, not global-latest")

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    parent = SimpleNamespace(
        profile_id="p-parent", slug="sample_default",
        resolved_prompts={}, prompt_version_ids={},
    )
    specialist = SimpleNamespace(
        profile_id="p-voice", slug="sample_voice_specialist",
        resolved_prompts={"sample_voice_persona": "Speak {language}."},
        prompt_version_ids={},
    )

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(
            cfg, "sample_voice", profile=parent, specialist_profile=specialist,
            language="Arabic, Saudi dialect",
        )

    assert out == "Speak Arabic, Saudi dialect."
    assert _voice_live_warnings(caplog), "a misplaced specialist snapshot must warn"


def test_neither_profile_pinned_still_falls_back_to_global_latest_and_warns(caplog, monkeypatch):
    """With the specialist consulted as a misplaced-pin fallback, the last resort
    must still be reached (and still warn) when NEITHER profile carries an
    entry."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    class _Resolver:
        @staticmethod
        def resolve_by_slug(slug, variables=None, agent_kind=None):
            return "PERSONA FROM GLOBAL LATEST"

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    parent = SimpleNamespace(
        profile_id="p-parent", slug="sample_default",
        resolved_prompts={}, prompt_version_ids={"system": "ver-brain"},
    )
    specialist = SimpleNamespace(
        profile_id="p-voice", slug="sample_voice_specialist",
        resolved_prompts={}, prompt_version_ids={"system": "ver-voice-brain"},
    )

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(
            cfg, "sample_voice", profile=parent, specialist_profile=specialist
        )

    assert out == "PERSONA FROM GLOBAL LATEST"
    joined = " ".join(r.getMessage() for r in _voice_live_warnings(caplog))
    assert "not pinned" in joined, joined


def test_one_profile_serving_both_roles_does_not_warn(caplog, monkeypatch):
    """A tenant with no `agent_profile_slug` serves voice off the parent itself,
    so the endpoint passes the SAME row as both `profile` and
    `specialist_profile`. That must resolve on the governed parent branch and
    never trip the misplaced-pin warning — that branch is gated on the two rows
    being different profiles."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    class _Resolver:
        @staticmethod
        def resolve(version_id, variables=None, agent_kind=None, prompt_key=None):
            assert version_id == "ver-parent"
            return "SOLE PROFILE PERSONA"

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    only = SimpleNamespace(
        profile_id="p-only", slug="sales_default",
        resolved_prompts={}, prompt_version_ids={"voice_persona": "ver-parent"},
    )

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(cfg, "sales", profile=only, specialist_profile=only)

    assert out == "SOLE PROFILE PERSONA"
    warnings = _voice_live_warnings(caplog)
    assert not warnings, f"one profile in both roles must not warn: {warnings}"


def _voice_live_warnings(caplog):
    """Warnings emitted by endpoints.voice_live specifically -- scoped so an
    unrelated library warning captured by caplog can't make these assertions
    brittle (review finding: the two tests below must isolate THIS module's
    warning, not assert the record list is globally empty)."""
    import logging

    return [
        r for r in caplog.records
        if r.name == "endpoints.voice_live" and r.levelno == logging.WARNING
    ]


def test_resolved_snapshot_path_does_not_warn(caplog):
    """Review finding: the original test only asserted the warning FIRES on
    the ungoverned fallback -- nothing pinned it ABSENT on the two governed
    branches. A regression that hoisted the warning to fire unconditionally
    would have passed every test in this file. The frozen compile-on-promote
    snapshot is fully governed (promote controls it), so it must never warn
    -- a warning here would train operators to ignore the line, which is
    worse than no warning at all."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    class _Profile:
        resolved_prompts = {"sample_voice_persona": "SNAPSHOT PERSONA"}
        prompt_version_ids = {}
        slug = "sample_voice_specialist"

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=_Profile())

    assert out == "SNAPSHOT PERSONA"
    warnings = _voice_live_warnings(caplog)
    assert not warnings, f"the governed (resolved-snapshot) path must not warn: {warnings}"


def test_pinned_version_path_does_not_warn(caplog, monkeypatch):
    """Same review finding, other governed branch: an explicit
    prompt_version_ids pin (dev/staging) is also fully governed -- rollback
    controls it -- so it must not warn either."""
    import logging

    from endpoints.voice_live import resolve_persona_prompt
    from voice.live.config import LiveVoiceConfig

    calls = {}

    class _Resolver:
        @staticmethod
        def resolve(version_id, variables=None, agent_kind=None, prompt_key=None):
            calls["version_id"] = version_id
            return "PINNED PERSONA"

    monkeypatch.setattr("endpoints.voice_live.PromptResolver", _Resolver)

    class _Profile:
        resolved_prompts = {}
        prompt_version_ids = {"sample_voice_persona": "ver-pinned"}
        slug = "sample_voice_specialist"

    cfg = LiveVoiceConfig(enabled=True, persona_prompt_slug="sample_voice_persona")
    with caplog.at_level(logging.WARNING):
        out = resolve_persona_prompt(cfg, "sample_voice", profile=_Profile())

    assert out == "PINNED PERSONA"
    assert calls["version_id"] == "ver-pinned"
    warnings = _voice_live_warnings(caplog)
    assert not warnings, f"the governed (pinned-version) path must not warn: {warnings}"


def test_an_operator_accent_beats_the_bare_portal_locale():
    # A deployment's voice speaks Levantine (voice_config.language = ar-JO,
    # 2026-10-01) while the app sends a bare ?locale=ar.
    from endpoints.voice_live import language_name, session_language

    assert session_language("ar", "ar-JO", "ar-SA") == "ar-JO"
    assert language_name("ar-JO") == "Arabic, Levantine (Jordanian) dialect"
    # A different language, or an explicit accent, still wins.
    assert session_language("en", "ar-JO", "en-US") == "en-US"
    assert session_language("ar-EG", "ar-JO", "ar-EG") == "ar-EG"
    # Nothing configured: the map's default, as before.
    assert session_language("ar", None, "ar-SA") == "ar-SA"
    assert session_language("ar", "ar", "ar-SA") == "ar-SA"
