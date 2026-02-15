"""
Discord AudioSink that captures voice data, resamples it,
and forwards it to the STT process via IPC queue.
"""

from __future__ import annotations

from multiprocessing import Queue as MPQueue
from typing import Optional, Union

import discord
from discord.ext.voice_recv import AudioSink, VoiceData

from audio.resampler import AudioResampler
from utils.logging import get_logger

logger = get_logger(__name__)


class STTAudioSink(AudioSink):
    """
    Receives 48 kHz stereo PCM from Discord, resamples to
    16 kHz mono, and pushes (user_id, pcm_bytes) into the queue.
    """

    def __init__(self, audio_queue: MPQueue) -> None:
        super().__init__()
        self._queue = audio_queue
        self._resampler = AudioResampler()
        logger.info("STTAudioSink initialised.")

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
            self._queue.put((user.id, resampled))
        except Exception as exc:
            logger.error("AudioSink write error: %s", exc)

    def cleanup(self) -> None:
        logger.info("STTAudioSink cleanup.")
