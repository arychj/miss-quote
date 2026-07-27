"""
Discord AudioSink that captures voice data, resamples it,
and hands it to the STT processor.
"""

from __future__ import annotations

from typing import Optional, Union

import discord
from discord.ext.voice_recv import AudioSink, VoiceData

from audio.resampler import AudioResampler
from stt.processor import STTProcessor
from utils.logging import get_logger

logger = get_logger(__name__)

UNKNOWN_CHANNEL = "unknown"


class STTAudioSink(AudioSink):
    """
    Receives 48 kHz stereo PCM from Discord, resamples it to 16 kHz mono, and
    submits it to the processor.

    `write` runs on the voice receive thread while the router holds its lock, so
    it does only the resample — everything else is scheduled onto the loop.
    """

    def __init__(self, processor: STTProcessor, channel: discord.abc.Connectable) -> None:
        super().__init__()
        self._processor = processor
        self._resampler = AudioResampler()
        self._channel_name = getattr(channel, "name", UNKNOWN_CHANNEL)
        logger.info("STTAudioSink listening on '%s'.", self._channel_name)

    def wants_opus(self) -> bool:
        """We want decoded PCM, not raw Opus."""
        return False

    def write(
        self,
        user: Optional[Union[discord.User, discord.Member]],
        data: VoiceData,
    ) -> None:
        if user is None:
            return

        try:
            resampled = self._resampler.resample(data.pcm)
            self._processor.submit(
                user.id, user.display_name, self._channel_name, resampled
            )
        except Exception as exc:
            logger.error("AudioSink write error: %s", exc)

    def cleanup(self) -> None:
        logger.info("STTAudioSink cleanup for '%s'.", self._channel_name)
