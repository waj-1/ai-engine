#!/usr/bin/env python3
"""
Voice WebSocket Client Example

This script demonstrates how to connect to the voice WebSocket endpoint
and stream a WAV file. It handles the authentication, audio streaming,
and frame processing as described in the voice WebSocket protocol.

Usage:
  # With simulator (for testing handshake)
  VOICE_LIVE_SIM_ENABLED=1 python voice_websocket_client.py
  
  # With real service (requires API key)
  VOICE_API_KEY=your-api-key python voice_websocket_client.py
"""

import asyncio
import base64
import json
import logging
import os
import wave
import websockets
from typing import Optional

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
WS_URL = "wss://your-domain.com/v1/agents/{slug}/voice"
API_KEY = os.getenv("VOICE_API_KEY", "your-api-key")
WAV_FILE = "sample.wav"  # Path to your WAV file


class VoiceWebSocketClient:
    def __init__(self, ws_url: str, api_key: str):
        self.ws_url = ws_url
        self.api_key = api_key
        self.websocket = None
        self.played_frames = 0
        self.received_frames = 0
        self.is_mute = False

    async def connect(self):
        """Connect to the voice WebSocket and authenticate."""
        logger.info(f"Connecting to {self.ws_url}")
        try:
            self.websocket = await websockets.connect(self.ws_url)
            logger.info("Connected to WebSocket")
            
            # Send authentication frame
            auth_frame = {
                "type": "auth",
                "api_key": self.api_key
            }
            await self.websocket.send(json.dumps(auth_frame))
            logger.info("Sent authentication frame")
            
            # Wait for response
            response = await self.websocket.recv()
            response_data = json.loads(response)
            
            if response_data.get("type") == "error":
                logger.error(f"Authentication failed: {response_data.get('message')}")
                return False
                
            logger.info("Authentication successful")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return False

    async def stream_audio(self, wav_file: str):
        """Stream audio from a WAV file."""
        try:
            with wave.open(wav_file, "rb") as wav_file:
                # Check WAV file format
                if wav_file.getnchannels() != 1:
                    logger.error("WAV file must be mono")
                    return
                if wav_file.getsampwidth() != 2:
                    logger.error("WAV file must be 16-bit")
                    return
                if wav_file.getframerate() != 16000:
                    logger.warning("WAV file is not 16kHz - resampling required")
                
                # Get audio data
                frames = wav_file.readframes(wav_file.getnframes())
                
                # Split into 20ms frames (320 samples per frame)
                frame_size = 320 * 2  # 320 samples * 2 bytes per sample
                audio_frames = [frames[i:i+frame_size] for i in range(0, len(frames), frame_size)]
                
                logger.info(f"Streaming {len(audio_frames)} audio frames")
                
                # Send audio frames
                for i, frame_data in enumerate(audio_frames):
                    if not frame_data:
                        continue
                        
                    # Encode frame as base64
                    encoded_frame = base64.b64encode(frame_data).decode("utf-8")
                    
                    # Create audio frame
                    audio_frame = {
                        "type": "audio",
                        "frames": [encoded_frame],
                        "is_mute": self.is_mute
                    }
                    
                    # Send frame
                    await self.websocket.send(json.dumps(audio_frame))
                    
                    # Log progress
                    if (i + 1) % 10 == 0:
                        logger.info(f"Sent {i + 1}/{len(audio_frames)} frames")
                    
                    # Simulate real-time streaming
                    await asyncio.sleep(0.02)  # 20ms per frame
                    
                logger.info("Finished streaming audio")
                
        except Exception as e:
            logger.error(f"Audio streaming failed: {str(e)}")

    async def listen_for_responses(self):
        """Listen for server responses."""
        try:
            async for message in self.websocket:
                response_data = json.loads(message)
                
                response_type = response_data.get("type")
                
                if response_type == "audio":
                    self.received_frames += 1
                    logger.info(f"Received audio frame ({self.received_frames} total)")
                    
                elif response_type == "mute":
                    self.is_mute = response_data.get("is_mute", False)
                    logger.info(f"Server mute status: {self.is_mute}")
                    
                elif response_type == "played_frames":
                    played_count = response_data.get("count", 0)
                    self.played_frames = played_count
                    logger.info(f"Server acknowledged {played_count} played frames")
                    
                elif response_type == "error":
                    error_code = response_data.get("code", 0)
                    error_message = response_data.get("message", "Unknown error")
                    logger.error(f"Server error: {error_message} (code: {error_code})")
                    
                else:
                    logger.info(f"Received unknown frame type: {response_type}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket connection closed")
        except Exception as e:
            logger.error(f"Error listening for responses: {str(e)}")

    async def close(self):
        """Close the WebSocket connection."""
        if self.websocket:
            await self.websocket.close()
            logger.info("WebSocket connection closed")


async def main():
    # Create client
    client = VoiceWebSocketClient(WS_URL.format(slug="your-agent-slug"), API_KEY)
    
    # Connect
    if not await client.connect():
        logger.error("Failed to connect to WebSocket")
        return
    
    try:
        # Start listening for responses in background
        listener_task = asyncio.create_task(client.listen_for_responses())
        
        # Stream audio
        await client.stream_audio(WAV_FILE)
        
        # Wait a bit for any final responses
        await asyncio.sleep(2)
        
        # Cancel listener task
        listener_task.cancel()
        
        # Wait for task to complete
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
            
    finally:
        # Close connection
        await client.close()


if __name__ == "__main__":
    # Replace with your agent slug
    agent_slug = "your-agent-slug"
    WS_URL = WS_URL.format(slug=agent_slug)
    
    asyncio.run(main())
