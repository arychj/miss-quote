"""
Rate conversion at both ends of the pipeline.

Inbound, `AudioResampler` converts 48 kHz stereo int16 PCM (Discord Opus output)
→ 16 kHz mono int16 PCM (Silero and Wyoming input). That direction is
load-bearing rather than a convenience: Silero only accepts 512-sample frames at
16 kHz, so it cannot be delegated to the ASR server.

Outbound, `PlaybackResampler` takes a synthesizer's mono output at whatever rate
it works in and produces the 48 kHz stereo Discord plays.
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

MONO_CHANNELS = 1
SAMPLE_DTYPE = np.int16
MONO_COLUMN_SHAPE = (-1, MONO_CHANNELS)
NO_SAMPLES = np.empty(0, dtype=SAMPLE_DTYPE)


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


class PlaybackResampler:
    """
    Converts one synthesized clip to the 48 kHz stereo Discord plays.

    Stateful and single-use. `soxr.ResampleStream` carries its filter state from
    one call to the next, which is what keeps the seam between two chunks of a
    streaming clip from clicking; an instance therefore belongs to one clip and
    is discarded with it. Resampling each chunk independently would be simpler
    and audibly wrong.

    Input is mono, because that is what a TTS server produces. Widening to
    stereo is a duplicate of the one channel rather than a mix.
    """

    __slots__ = ("_stream",)

    def __init__(self, rate: int) -> None:
        # An exact match is the common case once a synthesizer is configured to
        # Discord's rate, and it should not pay for a filter that does nothing.
        self._stream = (
            None
            if rate == audio_cfg.playback_sample_rate
            else soxr.ResampleStream(
                rate,
                audio_cfg.playback_sample_rate,
                MONO_CHANNELS,
                dtype=SAMPLE_DTYPE,
                quality=RESAMPLE_QUALITY,
            )
        )

    def feed(self, pcm: bytes) -> bytes:
        """Convert one chunk of mono PCM, returning what is ready to play."""
        return self._widen(self._convert(np.frombuffer(pcm, dtype=SAMPLE_DTYPE)))

    def flush(self) -> bytes:
        """
        Convert whatever the filter is still holding.

        The resampler delays a few samples to fill its window, so the tail of a
        clip only comes out once it is told there is no more input.
        """
        return self._widen(self._convert(NO_SAMPLES, last=True))

    def _convert(self, mono: np.ndarray, last: bool = False) -> np.ndarray:
        if self._stream is None:
            return mono
        return self._stream.resample_chunk(mono, last=last)

    @staticmethod
    def _widen(mono: np.ndarray) -> bytes:
        if mono.size == 0:
            return b""
        return np.repeat(
            mono.reshape(*MONO_COLUMN_SHAPE), audio_cfg.playback_channels, axis=CHANNEL_AXIS
        ).tobytes()
