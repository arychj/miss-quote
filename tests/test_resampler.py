import numpy as np

from audio.resampler import AudioResampler, PlaybackResampler

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


# ── outbound: TTS to Discord ──────────────────────

TTS_RATE = 24_000
PLAYBACK_RATE = 48_000
PLAYBACK_CHANNELS = 2
FREQUENCY = 440
AMPLITUDE = 10_000
CHUNK_SAMPLES = 1_000


def _tone(samples: int, rate: int) -> np.ndarray:
    t = np.arange(samples) / rate
    return (AMPLITUDE * np.sin(2 * np.pi * FREQUENCY * t)).astype(np.int16)


def test_playback_resampler_outputs_48khz_stereo() -> None:
    resampler = PlaybackResampler(TTS_RATE)
    mono = _tone(TTS_RATE, TTS_RATE)

    played = resampler.feed(mono.tobytes()) + resampler.flush()
    samples = np.frombuffer(played, dtype=np.int16)

    assert len(samples) == PLAYBACK_RATE * PLAYBACK_CHANNELS
    assert np.array_equal(samples[0::2], samples[1::2])


def test_playback_resampler_preserves_a_tone() -> None:
    resampler = PlaybackResampler(TTS_RATE)
    mono = _tone(TTS_RATE // 2, TTS_RATE)

    played = resampler.feed(mono.tobytes()) + resampler.flush()
    left = np.frombuffer(played, dtype=np.int16)[0::2]

    spectrum = np.abs(np.fft.rfft(left.astype(np.float64)))
    peak_hz = np.fft.rfftfreq(len(left), 1 / PLAYBACK_RATE)[np.argmax(spectrum)]

    assert abs(peak_hz - FREQUENCY) < 5


def test_playback_resampler_streams_without_losing_samples() -> None:
    """Chunk boundaries are a filter-state problem, not a truncation problem."""
    mono = _tone(TTS_RATE, TTS_RATE)
    resampler = PlaybackResampler(TTS_RATE)

    played = b""
    for offset in range(0, len(mono), CHUNK_SAMPLES):
        played += resampler.feed(mono[offset : offset + CHUNK_SAMPLES].tobytes())
    played += resampler.flush()

    assert len(np.frombuffer(played, dtype=np.int16)) == PLAYBACK_RATE * PLAYBACK_CHANNELS


def test_playback_resampler_skips_the_filter_at_the_target_rate() -> None:
    """A synthesizer already at 48 kHz should not pay for a filter that does nothing."""
    resampler = PlaybackResampler(PLAYBACK_RATE)
    mono = _tone(CHUNK_SAMPLES, PLAYBACK_RATE)

    played = np.frombuffer(resampler.feed(mono.tobytes()), dtype=np.int16)

    assert np.array_equal(played[0::2], mono)
    assert resampler.flush() == b""
