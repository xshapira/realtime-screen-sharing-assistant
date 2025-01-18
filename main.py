import asyncio
import base64
import contextlib
import io
import json
import wave
from dataclasses import dataclass, field
from typing import Any

import google.generativeai as generative
import websockets
from google import genai
from pydub import AudioSegment
from websockets.protocol import Protocol

from config import config
from logger import setup_logger

API_KEY = config.GOOGLE_API_KEY
MODEL = "gemini-2.0-flash-exp"
TRANSCRIPTION_MODEL = "gemini-1.5-flash-8b"

generative.configure(api_key="")  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


log = setup_logger(__name__)

# model_list = list(generative.list_models())
# for model in model_list:
#     log.info(model.name)


client = genai.Client(
    api_key="",
    http_options={
        "api_version": "v1alpha",
    },
)


@dataclass
class GeminiSession:
    """Holds session state and data"""

    websocket: Protocol
    session: Any
    audio_data: bytearray = field(default_factory=bytearray)


async def process_media_chunk(session: Any, chunk: dict) -> None:
    """Process a single media chunk and send it to Gemini."""
    mime_type = chunk.get("mime_type")
    if mime_type in ("audio/pcm", "image/jpeg"):
        await session.send(
            {
                "mime_type": mime_type,
                "data": chunk["data"],
            }
        )


async def handle_client_message(session: Any, message: str) -> None:
    """Process a single message form the client."""
    try:
        data = json.loads(message)
        if "realtime_input" in data:
            for chunk in data["realtime_input"]["media_chunks"]:
                await process_media_chunk(session, chunk)
    except json.JSONDecodeError:
        log.error("Invalid JSON in client message")
    except KeyError as exc:
        log.error(f"Missing required field in message: {exc}")
    except Exception as exc:
        log.error(f"Error processing client message: {exc}")


async def send_to_client(websocket: Protocol, data: dict) -> None:
    """Send formatted data to the client."""
    try:
        await websocket.send(json.dumps(data))
    except Exception as exc:
        log.error(f"Error sending to client: {exc}")


async def handle_text_part(gemini_session: GeminiSession, text: str) -> None:
    """Handle text response from Gemini."""
    await send_to_client(gemini_session.websocket, {"text": text})


async def handle_audio_part(gemini_session: GeminiSession, audio_data: bytes) -> None:
    """Handle audio response from Gemini."""
    base64_audio = base64.b64encode(audio_data).decode("utf-8")
    await send_to_client(gemini_session.websocket, {"audio": base64_audio})
    gemini_session.audio_data.extend(audio_data)


async def handle_turn_complete(gemini_session: GeminiSession) -> None:
    """Handle turn completion and audio transcription."""
    if gemini_session.audio_data:
        if transcribed_text := transcribe_audio(bytes(gemini_session.audio_data)):
            await send_to_client(gemini_session.websocket, {"text": transcribed_text})
        gemini_session.audio_data = bytearray()


async def handle_model_turn(gemini_session: GeminiSession, model_turn: Any) -> None:
    """Process a model turn and its parts."""
    for part in model_turn.parts:
        if hasattr(part, "text") and part.text:
            await handle_text_part(gemini_session, part.text)
        elif hasattr(part, "inline_data") and part.inline_data.data:
            await handle_audio_part(gemini_session, part.inline_data.data)


async def gemini_to_client_loop(gemini_session: GeminiSession) -> None:
    """Handle messages from Gemini to client."""
    try:
        async for response in gemini_session.session.receive():
            if not response.server_content:
                log.warning(f"Unhandled server message: {response}")
                continue

            if response.server_content.model_turn:
                await handle_model_turn(
                    gemini_session, response.server_content.model_turn
                )

            if response.server_content.turn_complete:
                await handle_turn_complete(gemini_session)

    except websockets.exceptions.ConnectionClosedOK:
        log.info("Client connection closed normally")
    except Exception as exc:
        log.error(f"Error in Gemini-to-client loop: {exc}")

    finally:
        log.info("Gemini-to-client loop terminated")


async def client_to_gemini_loop(gemini_session: GeminiSession) -> None:
    """Handle messages from client to Gemini."""
    try:
        async for message in gemini_session.websocket:
            # log.info(
            #     f"Received message from client: {message[:100]}..."
            # )  # log first 100 chars
            await handle_client_message(gemini_session.session, message)
    except websockets.exceptions.ConnectionClosedOK:
        log.info("Client connection closed normally")
    except Exception as exc:
        log.error(f"Error in client-to-Gemini loop: {exc}")
    finally:
        log.info("Client-to-Gemini loop terminated")


async def gemini_session_handler(
    client_websocket: Protocol,
) -> None:
    send_task = None
    receive_task = None

    try:
        message = await client_websocket.recv()
        log.info(f"Received initial message: {message}")
        config_data = json.loads(message)
        default_config = {
            "generation_config": {
                "response_modalities": ["AUDIO", "TEXT"],
                "language": "en",
                "temperature": 0.7,
                "candidate_count": 1,
            },
            "safety_settings": {
                "harassment": "block_none",
                "hate_speech": "block_none",
                "sexually_explicit": "block_none",
                "dangerous_content": "block_none",
            },
        }

        # get setup config from client or use defaults
        client_setup = config_data.get("setup", {})
        config = {"setup": default_config | client_setup}

        # log.info(f"Connecting to Gemini with config: {json.dumps(config, indent=2)}")

        async with client.aio.live.connect(model=MODEL, config=config) as session:
            log.info("Connected to Gemini API")

            gemini_session = GeminiSession(
                websocket=client_websocket,
                session=session,
            )

            send_task = asyncio.create_task(gemini_to_client_loop(gemini_session))
            receive_task = asyncio.create_task(client_to_gemini_loop(gemini_session))
            await asyncio.gather(send_task, receive_task)

    except json.JSONDecodeError:
        log.error("Invalid configuration received")
    except Exception as exc:
        log.error(f"Session error: {exc}")
    finally:
        # clean up tasks
        for task in (send_task, receive_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        log.info("Gemini session closed")


def transcribe_audio(audio_data: bytes) -> str | None:
    """Transcribes audio using Gemini 1.5 Flash."""
    try:
        if not audio_data:
            return "No audio data received."

        mp3_audio_base64 = convert_pcm_to_mp3(audio_data)
        if not mp3_audio_base64:
            return "Audio conversion failed."

        transcription_client = generative.GenerativeModel(
            model_name=TRANSCRIPTION_MODEL
        )

        prompt = """Generate a transcript of the speech.
        Please do not include any other text in the response.
        If you cannot hear the speech, please only say '<Not recognizable>'."""

        response = transcription_client.generate_content(
            [
                prompt,
                {
                    "mime_type": "audio/mp3",
                    "data": base64.b64decode(mp3_audio_base64),
                },
            ]
        )

        return response.text

    except Exception as exc:
        log.error(f"Error transcribing audio: {exc}")
        return None


def convert_pcm_to_mp3(pcm_data: bytes) -> str | None:
    """Converts PCM audio to base64 encoded MP3."""
    try:
        # create a WAV in memory first
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(24000)  # 24kHz
            wav_file.writeframes(pcm_data)

        wav_buffer.seek(0)
        audio_segment = AudioSegment.from_wav(wav_buffer)
        mp3_buffer = io.BytesIO()
        audio_segment.export(mp3_buffer, format="mp3", codec="libmp3lame")
        return base64.b64encode(mp3_buffer.getvalue()).decode("utf-8")

    except Exception as exc:
        log.error(f"Error converting PCM to MP3: {exc}")
        return None


async def main() -> None:
    async with websockets.serve(gemini_session_handler, "localhost", 9083):
        log.info("Running websocket server on localhost:9083...")
        await asyncio.Future()  # running indefinitely


if __name__ == "__main__":
    asyncio.run(main())
