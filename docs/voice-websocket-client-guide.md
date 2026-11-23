# Voice WebSocket Client Guide

This guide provides comprehensive documentation for the voice WebSocket protocol (`WS /v1/agents/{slug}/voice`) and includes a Python client example for streaming audio.

## Overview

The voice WebSocket enables real-time, duplex voice interactions with AI agents. This protocol handles audio streaming, muting/unmuting, and various control frames.

## Authentication

The connection begins with an authentication frame:

```json
{
  "type": "auth",
  "api_key": "your-api-key"
}
```

## Audio Format

- **Encoding**: PCM
- **Sample Rate**: 16,000 Hz
- **Channels**: 1 (mono)
- **Sample Width**: 2 bytes (16-bit)
- **Frame Duration**: 20ms (320 samples per frame)

## Frame Types

### Client Frames

1. **Auth Frame** (sent at connection)
   ```json
   {
     "type": "auth",
     "api_key": "your-api-key"
   }
   ```

2. **Audio Frame** (sent continuously)
   ```json
   {
     "type": "audio",
     "frames": [base64-encoded-pcm-data],
     "is_mute": false
   }
   ```

3. **Mute Frame** (to mute/unmute)
   ```json
   {
     "type": "mute",
     "is_mute": true
   }
   ```

4. **Played Frames Frame** (acknowledges played frames)
   ```json
   {
     "type": "played_frames",
     "count": 5
   }
   ```

### Server Frames

1. **Audio Frame** (server audio response)
   ```json
   {
     "type": "audio",
     "frames": [base64-encoded-pcm-data]
   }
   ```

2. **Mute Frame** (server mute status)
   ```json
   {
     "type": "mute",
     "is_mute": false
   }
   ```

3. **Played Frames Frame** (server acknowledges played frames)
   ```json
   {
     "type": "played_frames",
     "count": 3
   }
   ```

4. **Error Frame** (server error)
   ```json
   {
     "type": "error",
     "code": 4001,
     "message": "Authentication failed"
   }
   ```

## Close Codes

| Code | Description |
|------|-------------|
| 1000 | Normal closure |
| 4001 | Authentication failed |
| 4002 | Invalid audio format |
| 4003 | Unsupported audio encoding |
| 4004 | Session not found |
| 4005 | Agent not configured for voice |
| 4006 | Rate limit exceeded |
| 4100 | Session expired |
| 5000 | Internal server error |

## Python Client Example

See [`docs/examples/voice_websocket_client.py`](./examples/voice_websocket_client.py) for a complete Python client that streams a WAV file to the voice WebSocket.

## Testing

### Handshake Testing

Use the simulator to test the handshake:

```bash
VOICE_LIVE_SIM_ENABLED=1 python docs/examples/voice_websocket_client.py
```

### Real Audio Testing

For real audio testing, you'll need a Gemini or Azure realtime API key:

1. Set your API key as an environment variable:
   ```bash
   export VOICE_API_KEY="your-api-key"
   ```

2. Run the client:
   ```bash
   python docs/examples/voice_websocket_client.py
   ```

## Implementation Notes

- The client must handle base64 encoding/decoding for audio frames
- Audio must be resampled to 16kHz mono if necessary
- Implement proper error handling for all frame types
- Track played frames for proper synchronization
