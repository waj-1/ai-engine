from endpoints.voice_live import _apply_text_only_overrides, _suppress_extraction_display
from voice.live.config import LiveVoiceConfig
from voice.live.delegate import DelegateResult


def test_overrides_disable_every_audio_coupled_subsystem():
    cfg = LiveVoiceConfig(text_only=True)
    _apply_text_only_overrides(cfg)
    assert cfg.progress_after_sec == 0.0       # holding lines
    assert cfg.answer_watchdog_sec == 0.0      # re-delivery watchdog
    assert cfg.stream_sentences is False       # per-sentence speech
    assert cfg.greeting_trigger == ""          # a scribe listens; it does not greet


def test_overrides_noop_when_not_text_only():
    cfg = LiveVoiceConfig()
    cfg.greeting_trigger = "hello"
    before = (cfg.progress_after_sec, cfg.answer_watchdog_sec, cfg.stream_sentences, cfg.greeting_trigger)
    _apply_text_only_overrides(cfg)
    assert (cfg.progress_after_sec, cfg.answer_watchdog_sec, cfg.stream_sentences, cfg.greeting_trigger) == before


def test_overrides_keep_the_stall_tiers_armed():
    """A stalled turn is what a clinician experiences as "it listened and
    showed me nothing" — no proposals, no diagnosis, no note. That failure has
    nothing to do with audio, so text_only must NOT disarm the recovery that
    dispatches a question the model dropped without calling ask_agent.
    Reported from staging on 2026-08-10 after an earlier version of the
    override zeroed these along with the audio machinery.
    """
    defaults = LiveVoiceConfig()
    cfg = LiveVoiceConfig(text_only=True)
    _apply_text_only_overrides(cfg)
    assert cfg.stall_recovery_sec == defaults.stall_recovery_sec
    assert cfg.stall_recovery_unannounced_sec == defaults.stall_recovery_unannounced_sec
    assert cfg.stall_recovery_silent_sec == defaults.stall_recovery_silent_sec
    assert cfg.stall_recovery_sec > 0


def test_extraction_display_is_suppressed_only_for_scribe_turns_with_proposals():
    """The single gate all three delegate sites (ask_agent, relayed,
    recovered) consult before sending the display frame. text_only + proposals
    is an extraction turn — prose narrates cards already on screen. Every
    other combination keeps the frame: no proposals means a genuine answer
    whose only delivery is the display, and audio mode must stay
    byte-identical for the phone agents."""
    card = {"type": "proposal", "kind": "diagnosis", "payload": {}}
    scribe = LiveVoiceConfig(text_only=True)
    phone = LiveVoiceConfig()
    with_cards = DelegateResult(spoken="s", display="d", proposals=[card])
    without = DelegateResult(spoken="s", display="d")

    assert _suppress_extraction_display(scribe, with_cards) is True
    assert _suppress_extraction_display(scribe, without) is False
    assert _suppress_extraction_display(phone, with_cards) is False
    assert _suppress_extraction_display(phone, without) is False
