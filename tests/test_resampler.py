import pytest

np = pytest.importorskip("numpy")

try:
    import torch  # noqa: F401
    import torchaudio  # noqa: F401
except (ImportError, OSError) as exc:
    pytest.skip(f"torch/torchaudio is not available: {exc}", allow_module_level=True)

from audio.resampler import AudioResampler


def test_resampler_outputs_16khz_mono_int16_bytes() -> None:
    stereo_48khz_10ms = np.zeros((480, 2), dtype=np.int16).tobytes()

    resampled = AudioResampler.resample(stereo_48khz_10ms)

    assert isinstance(resampled, bytes)
    assert len(resampled) == 160 * 2
