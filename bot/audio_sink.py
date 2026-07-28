"""
Discord AudioSink that captures voice data, resamples it,
and hands it to the STT processor.
"""

from __future__ import annotations

from typing import Optional, Union

import discord
from discord.ext.voice_recv import AudioSink, VoiceData

from audio.resampler import AudioResampler
from config import file_cfg
from stt.processor import STTProcessor
from transcript.writer import Source
from utils.logging import get_logger

logger = get_logger(__name__)

UNKNOWN_NAME = "unknown"
UNKNOWN_ID = 0


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

        # A sink is bound to one channel for its lifetime, so the origin is
        # resolved once here rather than per frame.
        guild = getattr(channel, "guild", None)
        self._source = Source(
            guild_id=getattr(guild, "id", UNKNOWN_ID),
            guild=getattr(guild, "name", UNKNOWN_NAME),
            channel_id=getattr(channel, "id", UNKNOWN_ID),
            channel=getattr(channel, "name", UNKNOWN_NAME),
        )

        logger.info(
            "STTAudioSink listening on '%s/%s', writing to %s.",
            self._source.guild,
            self._source.channel,
            self._source.relative_directory,
        )

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
                user.id,
                file_cfg.name_for(user.id, user.display_name),
                self._source,
                resampled,
            )
        except Exception as exc:
            logger.error("AudioSink write error: %s", exc)

    def cleanup(self) -> None:
        logger.info("STTAudioSink cleanup for '%s'.", self._source.channel)
