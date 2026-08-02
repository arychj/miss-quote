"""Turning a clip down on its way to the player."""

import math

import numpy as np
import pytest

from miss_quote.audio.gain import (
    LOUDEST_SAMPLE,
    QUIETEST_SAMPLE,
    amplitude,
    scaled,
)
from miss_quote.config import SILENT_VOLUME, UNITY_VOLUME

SAMPLE_DTYPE = np.int16
QUIETER = 0.8
LOUDER = 1.2

# Two positions on the knob with exact answers, which is what makes them worth
# asserting on: half as loud is 10 dB down by the definition the curve is built
# from, and a quarter as loud is 20 dB, which is a tenth of the amplitude.
HALF = 0.5
QUARTER = 0.25
DECIBELS_AT_HALF = -10.0
DECIBELS_AT_QUARTER = -20.0
AMPLITUDE_AT_QUARTER = 0.1

# A knob below silence, which is not a quieter clip but an inverted one.
INVERTED = -0.5

# Loud enough that a tenth of it is still well clear of rounding.
LOUD_SAMPLE = 10_000
QUARTER_OF_LOUD = 1_000

# Two knobs to turn one after the other, for the property that lets the speaker
# multiply a deployment's loudness by a tool's scale before either is converted.
FIRST_KNOB = 0.6
SECOND_KNOB = 0.3

DECIBELS_PER_AMPLITUDE_DECADE = 20.0

# The curve is a power law evaluated in float; comparing two orderings of it is
# comparing the last bits of a double.
ROUNDING = 1e-12


def _pcm(*samples: int) -> bytes:
    return np.array(samples, dtype=SAMPLE_DTYPE).tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=SAMPLE_DTYPE)


def _decibels(gain: float) -> float:
    return DECIBELS_PER_AMPLITUDE_DECADE * math.log10(gain)


# ── the curve ─────────────────────────────────────


def test_both_ends_of_the_knob_are_themselves() -> None:
    """Full is full and silent is silent, or it is not a volume control."""
    assert amplitude(UNITY_VOLUME) == UNITY_VOLUME
    assert amplitude(SILENT_VOLUME) == SILENT_VOLUME


def test_half_the_knob_is_half_as_loud() -> None:
    """The whole point: ten decibels down, not the three that half amplitude is."""
    assert _decibels(amplitude(HALF)) == pytest.approx(DECIBELS_AT_HALF)


def test_a_quarter_of_the_knob_is_a_quarter_as_loud() -> None:
    assert _decibels(amplitude(QUARTER)) == pytest.approx(DECIBELS_AT_QUARTER)
    assert amplitude(QUARTER) == pytest.approx(AMPLITUDE_AT_QUARTER)


def test_the_knob_only_ever_goes_one_way() -> None:
    """A position between two others is between them in loudness as well."""
    positions = [SILENT_VOLUME, QUARTER, HALF, QUIETER, UNITY_VOLUME, LOUDER]
    gains = [amplitude(position) for position in positions]

    assert gains == sorted(gains)


def test_turning_two_knobs_is_turning_their_product() -> None:
    """
    What lets the speaker multiply before converting.

    A power law commutes with multiplication, so the deployment's loudness and
    whatever a tool asked for can be combined as knobs and converted once, which
    is the only reason one conversion point covers every setting there is.
    """
    together = amplitude(FIRST_KNOB * SECOND_KNOB)
    separately = amplitude(FIRST_KNOB) * amplitude(SECOND_KNOB)

    assert together == pytest.approx(separately, abs=ROUNDING)


def test_below_silence_is_silence() -> None:
    """A negative factor inverts a waveform, and a fractional power of one is
    not a real number at all."""
    assert amplitude(INVERTED) == SILENT_VOLUME


# ── the samples ───────────────────────────────────


def test_unity_hands_back_exactly_what_it_was_given() -> None:
    """The default, and it should not cost a round trip through numpy."""
    pcm = _pcm(-1000, 0, 1000)

    assert scaled(pcm, UNITY_VOLUME) is pcm


def test_a_knob_below_one_quietens() -> None:
    scaled_samples = _samples(scaled(_pcm(-LOUD_SAMPLE, 0, LOUD_SAMPLE), QUARTER))

    assert list(scaled_samples) == [-QUARTER_OF_LOUD, 0, QUARTER_OF_LOUD]


def test_a_knob_above_one_amplifies() -> None:
    scaled_samples = _samples(scaled(_pcm(-LOUD_SAMPLE, 0, LOUD_SAMPLE), LOUDER))

    assert list(np.abs(scaled_samples)) == [
        pytest.approx(LOUD_SAMPLE * amplitude(LOUDER), abs=1),
        0,
        pytest.approx(LOUD_SAMPLE * amplitude(LOUDER), abs=1),
    ]


def test_amplifying_clips_rather_than_wraps() -> None:
    """int16 wraps to the opposite extreme, which is a crack mid-word."""
    scaled_samples = _samples(scaled(_pcm(QUIETEST_SAMPLE, LOUDEST_SAMPLE), LOUDER))

    assert list(scaled_samples) == [QUIETEST_SAMPLE, LOUDEST_SAMPLE]


def test_silence_is_a_valid_volume() -> None:
    scaled_samples = _samples(scaled(_pcm(-LOUD_SAMPLE, LOUD_SAMPLE), SILENT_VOLUME))

    assert not scaled_samples.any()
