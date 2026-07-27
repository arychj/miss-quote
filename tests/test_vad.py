import math
import wave
from pathlib import Path

import numpy as np
import pytest

from config import audio_cfg, vad_cfg, MILLISECONDS_PER_SECOND
from stt.vad import SileroVAD

SPEECH_FIXTURE = Path(__file__).parent / "fixtures" / "speech_16k_mono.wav"

# The fixture is continuous speech, so the model should be confident across
# most of it once it has warmed up.
MIN_TRIGGERED_FRACTION = 0.8


@pytest.fixture(scope="module")
def vad() -> SileroVAD:
    return SileroVAD()


def _silence() -> np.ndarray:
    return np.zeros(vad_cfg.frame_samples, dtype=np.float32)


def _speech_frames() -> list[np.ndarray]:
    with wave.open(str(SPEECH_FIXTURE), "rb") as handle:
        assert handle.getframerate() == audio_cfg.output_sample_rate
        assert handle.getnchannels() == audio_cfg.output_channels
        pcm = handle.readframes(handle.getnframes())

    return [
        SileroVAD.frame_to_array(pcm[offset : offset + vad_cfg.frame_bytes])
        for offset in range(0, len(pcm) - vad_cfg.frame_bytes, vad_cfg.frame_bytes)
    ]


def test_vendored_model_is_present() -> None:
    assert vad_cfg.model_path.is_file()


def test_frame_conversion_normalises_int16(vad: SileroVAD) -> None:
    pcm = np.array([0, 32767, -32768], dtype=np.int16).tobytes()

    converted = SileroVAD.frame_to_array(pcm)

    assert converted.dtype == np.float32
    assert converted[0] == pytest.approx(0.0)
    assert converted[1] == pytest.approx(1.0, abs=1e-4)
    assert converted[2] == pytest.approx(-1.0)


def test_real_speech_triggers(vad: SileroVAD) -> None:
    """
    Guards the context window: the v5 graph scores `context + frame` together,
    and fed a bare frame it returns near-zero on unmistakable speech. Silence
    reads low either way, so only real audio catches a regression here.
    """
    iterator = vad.create_iterator()
    frames = _speech_frames()

    triggered = sum(bool(iterator(frame) or iterator.triggered) for frame in frames)

    assert triggered >= len(frames) * MIN_TRIGGERED_FRACTION


def test_context_carries_between_frames(vad: SileroVAD) -> None:
    """The tail of each frame must become the next frame's context."""
    iterator = vad.create_iterator()
    frame = _speech_frames()[5]

    iterator(frame)

    assert np.allclose(iterator._context[0], frame[-vad_cfg.context_samples :])


def test_silence_never_triggers(vad: SileroVAD) -> None:
    iterator = vad.create_iterator()

    for _ in range(50):
        iterator(_silence())

    assert iterator.triggered is False


def test_iterators_hold_independent_state(vad: SileroVAD) -> None:
    """The model is recurrent, so two speakers must not share a session state."""
    first = vad.create_iterator()
    second = vad.create_iterator()

    for _ in range(5):
        first(_silence())

    assert first._current_sample == 5 * vad_cfg.frame_samples
    assert second._current_sample == 0


def test_reset_clears_trigger_state(vad: SileroVAD) -> None:
    iterator = vad.create_iterator()
    iterator.triggered = True
    iterator._temp_end = 999

    iterator.reset_states()

    assert iterator.triggered is False
    assert iterator._temp_end == 0
    assert iterator._current_sample == 0


def test_release_requires_sustained_silence(vad: SileroVAD) -> None:
    """
    A speech probability dip shorter than min_silence_duration_ms must not end
    the utterance, or a pause between words splits one line into several.
    """
    iterator = vad.create_iterator()
    iterator.triggered = True

    min_silence_samples = (
        audio_cfg.output_sample_rate
        * vad_cfg.min_silence_duration_ms
        / MILLISECONDS_PER_SECOND
    )
    # One frame arms the timer, and the elapsed count is measured from there.
    frames_to_release = math.ceil(min_silence_samples / vad_cfg.frame_samples) + 1

    for _ in range(frames_to_release - 1):
        iterator(_silence())
        assert iterator.triggered is True, "released before the silence window elapsed"

    iterator(_silence())

    assert iterator.triggered is False
