"""Turning a clip down on its way to the player."""

import numpy as np

from miss_quote.audio.gain import LOUDEST_SAMPLE, QUIETEST_SAMPLE, scaled
from miss_quote.config import SILENT_VOLUME, UNITY_VOLUME

SAMPLE_DTYPE = np.int16
QUIETER = 0.8
LOUDER = 1.2


def _pcm(*samples: int) -> bytes:
    return np.array(samples, dtype=SAMPLE_DTYPE).tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=SAMPLE_DTYPE)


def test_unity_hands_back_exactly_what_it_was_given() -> None:
    """The default, and it should not cost a round trip through numpy."""
    pcm = _pcm(-1000, 0, 1000)

    assert scaled(pcm, UNITY_VOLUME) is pcm


def test_a_factor_below_one_quietens() -> None:
    scaled_samples = _samples(scaled(_pcm(-10_000, 0, 10_000), QUIETER))

    assert list(scaled_samples) == [-8_000, 0, 8_000]


def test_a_factor_above_one_amplifies() -> None:
    scaled_samples = _samples(scaled(_pcm(-10_000, 0, 10_000), LOUDER))

    assert list(scaled_samples) == [-12_000, 0, 12_000]


def test_amplifying_clips_rather_than_wraps() -> None:
    """int16 wraps to the opposite extreme, which is a crack mid-word."""
    scaled_samples = _samples(scaled(_pcm(QUIETEST_SAMPLE, LOUDEST_SAMPLE), LOUDER))

    assert list(scaled_samples) == [QUIETEST_SAMPLE, LOUDEST_SAMPLE]


def test_silence_is_a_valid_volume() -> None:
    scaled_samples = _samples(scaled(_pcm(-10_000, 10_000), SILENT_VOLUME))

    assert not scaled_samples.any()
