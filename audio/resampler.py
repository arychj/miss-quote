"""
High-quality audio resampler for the Discord → Whisper pipeline.

Converts 48 kHz stereo int16 PCM (Discord Opus output)
           → 16 kHz mono  int16 PCM (Whisper input).

Uses torchaudio for anti-aliased resampling.
"""

from __future__ import annotations

import numpy as np
import torch
import torchaudio.functional as F

from config import audio_cfg
from utils.logging import get_logger

logger = get_logger(__name__)

# Pre-compute the resampling kernel once (thread-safe, immutable).
_RESAMPLE_KWARGS = dict(
    orig_freq=audio_cfg.input_sample_rate,
    new_freq=audio_cfg.output_sample_rate,
)


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
        # 1. bytes → numpy int16 → reshape to (samples, 2)
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, 2)

        # 2. Stereo → Mono (average L/R channels)
        mono = samples.mean(axis=1).astype(np.int16)

        # 3. int16 → float32 tensor  (torchaudio expects float)
        tensor = torch.from_numpy(mono.astype(np.float32))

        # 4. Resample with anti-aliasing (Kaiser-window low-pass filter)
        resampled = F.resample(tensor, **_RESAMPLE_KWARGS)

        # 5. float32 → int16 bytes
        out = resampled.numpy().astype(np.int16)
        return out.tobytes()
