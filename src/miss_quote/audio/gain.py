"""
Loudness, applied to playback PCM on its way to the player.

Its own module rather than part of `resampler`, which converts between formats;
this changes the samples without changing what they are.

Scaling happens at playback rather than at synthesis, so a clip is cached at
whatever the synthesizer produced and turning the volume down does not
invalidate every phrase already rendered — including the chime, which nothing
here synthesized and which should be as quiet as the sentence behind it.

Every volume in this process is a knob rather than a multiplier: 1 is full, 0 is
silent, and half of it is half as loud to whoever is listening. That is not the
same as half the amplitude, because hearing is logarithmic — a clip at half
amplitude is a shade under 3 dB down and still sounds around four fifths as
loud. `amplitude` is the curve between the two, and it is applied here because
this is the one place every gain in the process passes through on its way to
becoming samples. A setting says what a channel should hear; nothing that sets
one should have to know what that is in dB.

It composes, which is why one conversion point is enough for settings that
multiply. The curve is a power law, so scaling by two knobs and converting the
product is the same arithmetic as converting each and multiplying — the
deployment's own loudness and whatever a tool asked for can be combined in
either order and in either domain, and `speaker` combines them before this ever
sees them.
"""

from __future__ import annotations

import math

import numpy as np

from miss_quote.config import SILENT_VOLUME, UNITY_VOLUME

SAMPLE_DTYPE = np.int16
SCALING_DTYPE = np.float32

QUIETEST_SAMPLE = np.iinfo(SAMPLE_DTYPE).min
LOUDEST_SAMPLE = np.iinfo(SAMPLE_DTYPE).max

# What the curve is derived from rather than the number it comes out as. Halving
# the perceived loudness of something takes about 10 dB off it, and amplitude
# moves a decade every 20, so a knob turned to a half is a shade under a third of
# the amplitude it was.
DECIBELS_PER_HALVING = 10.0
DECIBELS_PER_AMPLITUDE_DECADE = 20.0
HALVING = 2.0

LOUDNESS_EXPONENT = DECIBELS_PER_HALVING / (
    DECIBELS_PER_AMPLITUDE_DECADE * math.log10(HALVING)
)


def amplitude(volume: float) -> float:
    """
    What to multiply samples by for a knob to land where it is pointing.

    Both ends are themselves — full is full and silent is silent — and every
    position between them is the fraction of the loudness it reads as. A knob at
    a quarter is 20 dB down, which is a tenth of the amplitude and a quarter of
    what anybody hears.

    Above full it is a boost on the same terms, so a deployment asking for a
    fifth louder gets a fifth louder rather than the eighth that a fifth more
    amplitude works out as. Below silent there is nowhere to go: a negative
    factor inverts a waveform rather than quietening it, and a fractional power
    of one is not a real number at all.
    """
    if volume <= SILENT_VOLUME:
        return SILENT_VOLUME

    return volume**LOUDNESS_EXPONENT


def scaled(pcm: bytes, volume: float) -> bytes:
    """
    int16 PCM at some fraction of the loudness it arrived at.

    `volume` is a knob and not a multiplier: see `amplitude` for the difference
    and for why the conversion happens here.

    A no-op at unity, which is the default and which the curve leaves alone:
    audio nobody asked to be turned down should not pay a round trip through
    numpy to arrive at what it was handed.

    Scaled in float and clipped at full scale rather than left to wrap. int16
    wraps to the opposite extreme, so a factor above 1.0 on a passage already
    near the ceiling would come out as a crack in the middle of a word instead
    of as more of the same.
    """
    if volume == UNITY_VOLUME:
        return pcm

    gain = amplitude(volume)
    samples = np.frombuffer(pcm, dtype=SAMPLE_DTYPE).astype(SCALING_DTYPE) * gain

    return samples.clip(QUIETEST_SAMPLE, LOUDEST_SAMPLE).astype(SAMPLE_DTYPE).tobytes()
