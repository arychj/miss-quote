"""
Audio resampler for the Discord → ASR pipeline.

Converts 48 kHz stereo int16 PCM (Discord Opus output)
           → 16 kHz mono  int16 PCM (Silero and Wyoming input).

Resampling is load-bearing rather than a convenience: Silero only accepts
512-sample frames at 16 kHz, so it cannot be delegated to the ASR server.
"""

from __future__ import annotations

import numpy as np
import soxr

from config import audio_cfg
from utils.logging import get_logger

logger = get_logger(__name__)

STEREO_INTERLEAVED_SHAPE = (-1, audio_cfg.input_channels)
CHANNEL_AXIS = 1

# VHQ is the bit-exact-enough end of soxr's quality ladder and costs microseconds
# on 20 ms frames.
RESAMPLE_QUALITY = "VHQ"


class AudioResampler:
    """Stateless converter: 48 kHz stereo → 16 kHz mono (int16 PCM bytes)."""

    __slots__ = ()

    @staticmethod
    def resample(pcm_bytes: bytes) -> bytes:
        """
        Parameters
        ----------
        pcm_bytes : bytes
            Raw PCM int16, 48 kHz, 2-channel (interleaved L-R).

        Returns
        -------
        bytes
            Raw PCM int16, 16 kHz, mono.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(
            *STEREO_INTERLEAVED_SHAPE
        )

        # Average in int32 so a pair of near-full-scale samples cannot wrap.
        mono = samples.astype(np.int32).mean(axis=CHANNEL_AXIS).astype(np.int16)

        resampled = soxr.resample(
            mono,
            audio_cfg.input_sample_rate,
            audio_cfg.output_sample_rate,
            quality=RESAMPLE_QUALITY,
        )

        return resampled.astype(np.int16).tobytes()
