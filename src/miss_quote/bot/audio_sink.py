"""
Discord AudioSink that captures voice data, resamples it,
and hands it to the STT processor.
"""

from __future__ import annotations

from typing import Optional, Union

import discord
from discord.ext.voice_recv import AudioSink, VoiceData

from miss_quote.audio.resampler import AudioResampler
from miss_quote.config import file_cfg
from miss_quote.stt.processor import STTProcessor
from miss_quote.transcript.writer import TranscriptSession
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)


class STTAudioSink(AudioSink):
    """
    Receives 48 kHz stereo PCM from Discord, resamples it to 16 kHz mono, and
    submits it to the processor.

    `write` runs on the voice receive thread while the router holds its lock, so
    it does only the resample — everything else is scheduled onto the loop.
    """

    def __init__(self, processor: STTProcessor, session: TranscriptSession) -> None:
        super().__init__()
        self._processor = processor
        self._resampler = AudioResampler()

        # A sink is bound to one connection for its lifetime, and so is the
        # session, so the origin is resolved once here rather than per frame.
        self._session = session
        self._guild_id = session.source.guild_id

        logger.info(
            "STTAudioSink listening on '%s/%s', writing to %s.",
            session.source.guild_alias,
            session.source.channel,
            session.path,
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
                file_cfg.name_for(self._guild_id, user.id, user.display_name),
                self._session,
                resampled,
            )
        except Exception as exc:
            logger.error("AudioSink write error: %s", exc)

    def cleanup(self) -> None:
        logger.info("STTAudioSink cleanup for '%s'.", self._session.source.channel)
