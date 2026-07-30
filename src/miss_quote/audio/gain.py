"""
Loudness, applied to playback PCM on its way to the player.

Its own module rather than part of `resampler`, which converts between formats;
this changes the samples without changing what they are.

Scaling happens at playback rather than at synthesis, so a clip is cached at
whatever the synthesizer produced and turning the volume down does not
invalidate every phrase already rendered — including the chime, which nothing
here synthesized and which should be as quiet as the sentence behind it.
"""

from __future__ import annotations

import numpy as np

from miss_quote.config import UNITY_VOLUME

SAMPLE_DTYPE = np.int16
SCALING_DTYPE = np.float32

QUIETEST_SAMPLE = np.iinfo(SAMPLE_DTYPE).min
LOUDEST_SAMPLE = np.iinfo(SAMPLE_DTYPE).max


def scaled(pcm: bytes, volume: float) -> bytes:
    """
    int16 PCM at some fraction of the loudness it arrived at.

    A no-op at unity, which is the default: audio nobody asked to be turned
    down should not pay a round trip through numpy to arrive at what it was
    handed.

    Scaled in float and clipped at full scale rather than left to wrap. int16
    wraps to the opposite extreme, so a factor above 1.0 on a passage already
    near the ceiling would come out as a crack in the middle of a word instead
    of as more of the same.
    """
    if volume == UNITY_VOLUME:
        return pcm

    samples = np.frombuffer(pcm, dtype=SAMPLE_DTYPE).astype(SCALING_DTYPE) * volume

    return samples.clip(QUIETEST_SAMPLE, LOUDEST_SAMPLE).astype(SAMPLE_DTYPE).tobytes()
