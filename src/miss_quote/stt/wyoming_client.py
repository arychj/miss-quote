"""
Wyoming ASR client.

Wyoming is utterance-based rather than streaming: one connection carries one
utterance and the server closes it after replying, so there is no session to
keep alive and no reconnect logic to own.
"""

from __future__ import annotations

import asyncio

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient

from miss_quote.config import audio_cfg, stt_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)


async def transcribe(pcm: bytes) -> str | None:
    """
    Send one utterance of 16 kHz mono int16 PCM and return its transcript.

    Returns None when the utterance is too short to be worth a round trip, when
    the server yields no transcript, or when the exchange fails — a dropped
    utterance must never take the bot down with it.
    """
    if len(pcm) < stt_cfg.min_audio_bytes:
        return None

    try:
        async with asyncio.timeout(stt_cfg.timeout_seconds):
            return await _exchange(pcm)
    except TimeoutError:
        logger.warning(
            "Wyoming transcription timed out after %.0fs; dropping utterance.",
            stt_cfg.timeout_seconds,
        )
    except (OSError, ConnectionError) as exc:
        logger.error("Wyoming connection to %s:%d failed: %s", stt_cfg.host, stt_cfg.port, exc)
    except Exception as exc:
        logger.error("Wyoming transcription failed: %s", exc, exc_info=True)

    return None


async def _exchange(pcm: bytes) -> str | None:
    async with AsyncTcpClient(stt_cfg.host, stt_cfg.port) as client:
        await client.write_event(Transcribe(language=stt_cfg.language).event())
        await client.write_event(
            AudioStart(
                rate=audio_cfg.output_sample_rate,
                width=audio_cfg.sample_width,
                channels=audio_cfg.output_channels,
            ).event()
        )

        for offset in range(0, len(pcm), stt_cfg.chunk_bytes):
            await client.write_event(
                AudioChunk(
                    rate=audio_cfg.output_sample_rate,
                    width=audio_cfg.sample_width,
                    channels=audio_cfg.output_channels,
                    audio=pcm[offset : offset + stt_cfg.chunk_bytes],
                ).event()
            )

        await client.write_event(AudioStop().event())

        while (event := await client.read_event()) is not None:
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text or None

    return None
