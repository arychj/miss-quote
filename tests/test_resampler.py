import numpy as np

from audio.resampler import AudioResampler

INPUT_RATE = 48_000
OUTPUT_RATE = 16_000
BYTES_PER_SAMPLE = 2


def _stereo_frame(milliseconds: int) -> bytes:
    samples = INPUT_RATE * milliseconds // 1000
    return np.zeros((samples, 2), dtype=np.int16).tobytes()


def test_resampler_outputs_16khz_mono_int16_bytes() -> None:
    resampled = AudioResampler.resample(_stereo_frame(10))

    assert isinstance(resampled, bytes)
    assert len(resampled) == 160 * BYTES_PER_SAMPLE


def test_resampler_preserves_a_tone() -> None:
    """A 440 Hz tone must survive the rate conversion intact."""
    duration_seconds = 0.5
    frequency = 440
    amplitude = 10_000

    t = np.arange(int(INPUT_RATE * duration_seconds)) / INPUT_RATE
    mono = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.int16)
    stereo = np.repeat(mono[:, None], 2, axis=1).tobytes()

    out = np.frombuffer(AudioResampler.resample(stereo), dtype=np.int16)

    assert len(out) == int(OUTPUT_RATE * duration_seconds)

    # Dominant FFT bin should land on the original frequency.
    spectrum = np.abs(np.fft.rfft(out.astype(np.float64)))
    peak_hz = np.fft.rfftfreq(len(out), 1 / OUTPUT_RATE)[np.argmax(spectrum)]

    assert abs(peak_hz - frequency) < 5


def test_stereo_is_averaged_without_overflow() -> None:
    """Two near-full-scale channels must not wrap when averaged."""
    loud = np.full((480, 2), 32_000, dtype=np.int16).tobytes()

    out = np.frombuffer(AudioResampler.resample(loud), dtype=np.int16)

    assert out.min() > 0
