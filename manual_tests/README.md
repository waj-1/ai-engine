# Manual/Integration Tests

These tests require a running server and valid API credentials.

## Tests

- `test_deep_conversation.py` - Multi-turn conversation testing
- `test_multilingual_agent.py` - Language-specific testing

## Usage

```bash
# Start local server
python main.py

# Run deep conversation test
python manual_tests/test_deep_conversation.py --url http://localhost:8000 --token YOUR_TOKEN --business YOUR_BUSINESS_ID

# Run multilingual test
python manual_tests/test_multilingual_agent.py --url http://localhost:8000 --token YOUR_TOKEN --business YOUR_BUSINESS_ID
```

# Duplex voice scenarios: the portal's Testing screens run asserted live-voice
# scenarios against a real /voice/live edge (admin/voice_scenario_runner.py).

# Gemini Live end-of-speech A/B (how the turn-detection default was chosen)
# Times end-of-speech -> first audio for each silence_duration_ms, and checks
# whether a mid-sentence think-pause survives as one turn. Re-run it before
# changing gemini_live._DEFAULT_TURN_DETECTION; the recorded numbers are in
# that module and in this script's docstring.
GOOGLE_API_KEY=... AZURE_SPEECH_KEY=... AZURE_SPEECH_REGION=<your region> \
    python manual_tests/probe_gemini_turn_detection.py --model gemini-3.1-flash-live-preview

# Gemini Live: what does CALLER SILENCE do? (the 2026-08-19 "she fills the
# emptiness" report). Replays one full delegated turn — sentence-streamed
# WHEN_IDLE tool results, exactly as the endpoint delivers an answer — then
# streams real-time silence or room-ish noise and counts the NEW responses the
# model starts while the caller says nothing. Also the only honest
# end-of-speech -> first-audio reading available on this provider: production
# reports None there on purpose (a marker derived from the first audio would
# read ~0ms forever). Reach for it before blaming the endpoint's recovery
# tiers for an unprompted-sounding reply. Measured results in the docstring.
GOOGLE_API_KEY=... venv/bin/python manual_tests/probe_gemini_silence.py \
    --modes silence,noise-quiet,noise-loud --tail 45

## Note

These are integration tests that test the full system end-to-end.
For unit tests, use `pytest tests/`
