"""voice_config is a FLAT block after the 2026-08-17 unification.

Pre-unification the duplex resolver read `voice_config["live"]` and merged an
`agent_channels.config.voice.live` layer over it. The nested shape is accepted
for one release (unmigrated branch DB, hand edit, pre-migration snapshot) but
must warn; the channel layer is gone entirely — the spec's gate found zero rows
using it on any stack.
"""
import logging

from voice.live.config import resolve_live_config


def test_flat_block_resolves():
    cfg = resolve_live_config(
        profile_voice={"enabled": True, "model_id": "m-1", "voice_id": "Nova"},
        channel=None,
    )
    assert cfg.enabled is True
    assert cfg.model_id == "m-1"
    assert cfg.voice_id == "Nova"


def test_legacy_nested_block_still_resolves_and_warns(caplog):
    """An unmigrated row keeps working, loudly."""
    with caplog.at_level(logging.WARNING):
        cfg = resolve_live_config(
            profile_voice={"live": {"enabled": True, "model_id": "m-legacy"}},
            channel=None,
        )
    assert cfg.enabled is True
    assert cfg.model_id == "m-legacy"
    assert any("legacy 'live'" in r.message for r in caplog.records)


def test_legacy_row_ignores_top_level_turn_based_keys():
    """Top-level keys were turn-based-only and the duplex resolver never read
    them. A legacy row must resolve from `live` ALONE — merging the top level in
    would leak the turn-based `voice_id` default (an Azure neural voice name)
    onto a duplex session that expects the provider's own default."""
    cfg = resolve_live_config(
        profile_voice={
            "voice_id": "en-US-AvaMultilingualNeural",  # turn-based key
            "enabled": True,                             # turn-based: true
            "live": {"model_id": "m-1"},                 # duplex: enabled absent
        },
        channel=None,
    )
    assert cfg.voice_id == "Kore"      # dataclass default, not the turn-based value
    assert cfg.enabled is False        # duplex default, not the turn-based true


def test_channel_voice_override_is_ignored():
    """The channel layer was highest-precedence, invisible, and written by
    nothing. Verified zero rows use it; it must no longer apply."""
    cfg = resolve_live_config(
        profile_voice={"enabled": True, "voice_id": "Nova"},
        channel={"config": {"voice": {"live": {"voice_id": "Hijacked"}}}},
    )
    assert cfg.voice_id == "Nova"


def test_channel_argument_is_optional():
    cfg = resolve_live_config(profile_voice={"model_id": "m-1"})
    assert cfg.model_id == "m-1"


def test_filler_enabled_is_gone():
    """Dead key: declared, coerced and written, read nowhere. A stored value
    must neither resolve onto the config nor raise."""
    cfg = resolve_live_config(
        profile_voice={"model_id": "m-1", "filler_enabled": False}
    )
    assert not hasattr(cfg, "filler_enabled")
    assert cfg.model_id == "m-1"


# --- promote_legacy_voice_config: the WRITER-side shim -----------------------
#
# It must mirror supabase/migrations/20260817120000_unify_voice_config.sql,
# including statements 2b / 4b — the `live`-less pre-duplex turn-based row. That
# shape reaches this function from `admin/db_snapshot.py restore`, which
# re-imports rows AFTER the migration has already applied to the fresh database
# and so can never be repaired by it.


def test_promote_archives_a_pre_duplex_turn_based_block():
    """`{enabled, voice_id, stt_provider}` is the pre-duplex portal Voice card's
    shape. Flattened as-is it resolves duplex-ON with no model_id — LookupError,
    then CLOSE_FORBIDDEN on every connect. Archived, the duplex keys are absent
    and `enabled` falls back to its default."""
    from voice.live.config import promote_legacy_voice_config

    stored = {"enabled": True, "voice_id": "ar-SA-HamedNeural", "stt_provider": "azure"}
    out = promote_legacy_voice_config(stored)

    assert out == {"_retired_turn_based": stored}
    assert resolve_live_config(profile_voice=out).enabled is False
    # Idempotent: the archived shape is not itself archivable.
    assert promote_legacy_voice_config(out) == out


def test_promote_leaves_a_flat_duplex_block_alone():
    """Any key outside the retired nine proves the block is duplex."""
    from voice.live.config import promote_legacy_voice_config

    stored = {"enabled": True, "voice_id": "marin", "model_id": "m-1"}
    assert promote_legacy_voice_config(stored) == stored


def test_promote_leaves_a_block_of_only_colliding_names_alone():
    """`{enabled, voice_id, language, speaking_rate}` are the names the two
    blocks SHARE — no discriminator either way, and also what a promoted
    `{"live": {...}}` row with no top-level keys becomes. Rewriting it would
    destroy real duplex config; the migration leaves it alone and so must this."""
    from voice.live.config import promote_legacy_voice_config

    stored = {"enabled": True, "voice_id": "ar-SA-HamedNeural", "language": "ar"}
    assert promote_legacy_voice_config(stored) == stored
    # Same shape reached the other way round — a nested block with no top-level
    # keys — must survive promotion unchanged too.
    assert promote_legacy_voice_config({"live": stored}) == stored


def test_promote_treats_filler_enabled_as_a_duplex_discriminator():
    """`filler_enabled` was a duplex-only key, so a block carrying it is duplex:
    statement 2 strips the dead key, statement 2b must not archive the row."""
    from voice.live.config import promote_legacy_voice_config

    out = promote_legacy_voice_config(
        {"enabled": True, "voice_id": "Kore", "sample_rate": 16000,
         "filler_enabled": True}
    )
    assert out == {"enabled": True, "voice_id": "Kore", "sample_rate": 16000}
