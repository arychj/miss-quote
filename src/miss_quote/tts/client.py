"""
Wyoming TTS client.

The mirror of `stt.wyoming_client`: one connection carries one phrase and the
server closes it after answering, so there is no session to keep alive.

Audio is yielded as it arrives rather than collected and returned. A synthesizer
generates faster than real time but not instantly, and the caller can start
playing the first chunk while the rest is still being made.

Failure raises. The rule that a tool must never take the bot down is enforced by
the runner, which isolates every tool; swallowing errors here as the ASR client
does would instead hide them from the cache, which needs to know that a clip is
complete before it stores one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

from miss_quote.config import audio_cfg, tts_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

MONO_CHANNELS = 1


class SynthesisError(RuntimeError):
    """The synthesizer did not produce a complete clip."""


@dataclass(frozen=True)
class Speech:
    """One piece of a clip, at whatever rate the synthesizer works in."""

    rate: int
    pcm: bytes


async def synthesize(text: str) -> AsyncIterator[Speech]:
    """
    Speak one phrase, yielding mono PCM as it is generated.

    The voice is process-wide (`TTS_VOICE`) rather than per caller: a bot that
    answers in two voices is a bot nobody can tell is one bot.
    """
    async with AsyncTcpClient(tts_cfg.host, tts_cfg.port) as client:
        await client.write_event(Synthesize(text=text, voice=_voice()).event())

        rate: int | None = None
        spoke = False

        while True:
            event = await _next_event(client)

            if event is None:
                # The connection closed without an AudioStop. Anything already
                # yielded has been played, but it is not a whole phrase.
                raise SynthesisError("the synthesizer hung up mid-phrase")

            if AudioStart.is_type(event.type):
                rate = _accepted_rate(AudioStart.from_event(event))
                continue

            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                if not chunk.audio:
                    continue
                spoke = True
                yield Speech(rate=rate or chunk.rate, pcm=chunk.audio)
                continue

            if AudioStop.is_type(event.type):
                break

        if not spoke:
            raise SynthesisError("the synthesizer returned no audio")


def _voice() -> SynthesizeVoice | None:
    return SynthesizeVoice(name=tts_cfg.voice) if tts_cfg.voice else None


async def _next_event(client: AsyncTcpClient):
    """
    Wait for one event, on a budget.

    The timeout covers a single wait rather than the whole exchange, so a long
    phrase arriving steadily is not cut off for taking a long time, while a
    server that stops answering is given up on.
    """
    try:
        async with asyncio.timeout(tts_cfg.timeout_seconds):
            return await client.read_event()
    except TimeoutError as exc:
        raise SynthesisError(
            f"the synthesizer went quiet for {tts_cfg.timeout_seconds:.0f}s"
        ) from exc


def _accepted_rate(start: AudioStart) -> int:
    """
    The rate to read the clip at, having checked the rest of the format.

    Only mono 16-bit is handled: it is what every TTS server in reach produces,
    and reading stereo as mono would play back at half speed rather than fail.
    """
    if start.channels != MONO_CHANNELS or start.width != audio_cfg.sample_width:
        raise SynthesisError(
            f"expected {MONO_CHANNELS}-channel {audio_cfg.sample_width * 8}-bit audio, "
            f"got {start.channels}-channel {start.width * 8}-bit"
        )

    return start.rate
