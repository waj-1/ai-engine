"""Whole-utterance back-channel detection for the supersede rule.

A committed caller utterance supersedes any answer dispatched before it
(endpoints/voice_live.py). That is right for "no, the best branch instead"
and catastrophic for "mm-hm" — superseding a nod drops the answer the caller
is actively waiting for, which is worse than the bug the rule fixes.
"""
import pytest

from endpoints.voice_live import _is_backchannel


@pytest.mark.parametrize("utterance", [
    "yes", "yeah", "ok", "okay", "sure", "right", "mm-hm", "uh-huh", "hmm",
    "ok ok",
    "أيوه", "ايوه", "نعم", "طيب", "تمام", "اها", "زين", "إيه",
    # The spelling that actually escaped: a live ASR transcribed the same nod
    # as "أيوة" (ta-marbuta) while the token set listed "أيوه" (ha), so it read
    # as a real utterance and dropped the answer mid-lookup. Normalisation —
    # not a longer list — is what makes this class safe.
    "أيوة", "أيوة.", "طيّب",
    "", "   ", "...",
])
def test_nods_are_backchannel(utterance):
    assert _is_backchannel(utterance) is True


@pytest.mark.parametrize("utterance", [
    # Availability probes: the caller checking the line during a long tool
    # wait. Live 2026-08-03: an answer landed mode=silent after a 14.6s
    # delegate because "هلو؟" committed as a substantive utterance and
    # superseded it — the caller then re-asked the same question across
    # three redials. Probing the line must not drop the answer.
    "هلو", "هلو؟", "هالو", "الو", "ألو", "ألو؟",
    "سامعني", "سامعتني؟", "تسمعني", "معي؟", "وينك",
    "hello", "hello?", "hey", "هلو هلو",
])
def test_availability_probes_are_backchannel(utterance):
    assert _is_backchannel(utterance) is True


@pytest.mark.parametrize("utterance", [
    # Cancellations. Negatives are deliberately NOT acknowledgements.
    "no", "لا", "لا، لا، أبي أعرف أفضل فرع بدل كذا",
    "no, the best branch instead",
    # A nod PLUS a real turn is a real turn.
    "yes, and what about Jeddah?",
    "طيب وش عن جدة؟",
    # Ordinary questions.
    "how were my sales last month", "كيف كانت مبيعاتي الشهر الماضي؟",
    # A probe PLUS a real turn is a real turn.
    "hello, what were my sales yesterday?",
    "هلو، كم مبيعات أمس؟",
])
def test_real_turns_are_not_backchannel(utterance):
    assert _is_backchannel(utterance) is False


# --- relay-intent matching -------------------------------------------------
# Custody over the IBAN flow opens on the CALLER'S OWN WORDS, before any tool
# runs. When that match misses, the realtime model runs the protected flow
# itself — and on 2026-08-01 it asked for the new IBAN with no passcode sent,
# which is the protection running backwards.

import pytest as _pytest

from endpoints.voice_live import _normalize_intent_text

_TERMS = ["iban", "e ban", "e band", "eye ban",
          "آيبان", "ايبان", "آيبن", "اي بان"]


def _matches(utterance: str) -> bool:
    norm = _normalize_intent_text(utterance)
    return any(_normalize_intent_text(t) in norm for t in _TERMS)


@_pytest.mark.parametrize("utterance", [
    # The one that actually missed: ASR wrote "الآي بأن" (hamza-on-alef) while
    # the term list carries "آيبان". Spacing normalisation alone did not save
    # it — the orthography has to be normalised too.
    "أبي أغير الآي بأن حقي",
    "وش الآيبأن المسجل بحسابي؟",
    "أبي أغير الآيبان",
    "ابغى اغير الاي بان",
    "I want to change my IBAN",
    "change my E-band please",
    "what's my eye ban",
])
def test_iban_intent_survives_asr_spelling(utterance):
    assert _matches(utterance), f"relay custody would not open for {utterance!r}"


@_pytest.mark.parametrize("utterance", [
    "كم مبيعاتي الشهر الماضي؟",
    "which branch did best",
    "الجهاز ما يشتغل",
])
def test_unrelated_questions_do_not_open_custody(utterance):
    assert not _matches(utterance)
