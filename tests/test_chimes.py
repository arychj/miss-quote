"""Clips kept by hand, and the directory they are read out of."""

import wave

import numpy as np
import pytest

from miss_quote.audio.chimes import ChimeLibrary
from miss_quote.config import audio_cfg
from miss_quote.tts.cache import SpeechCache

CLIP_NAME = "chime.wav"
STEREO_CHANNELS = 2
EIGHT_BIT_WIDTH = 1
PLAYBACK_BYTES_PER_FRAME = audio_cfg.playback_channels * audio_cfg.sample_width

SOURCE_RATE = 24_000
SOURCE_SECONDS = 0.5
SOURCE_SAMPLES = int(SOURCE_RATE * SOURCE_SECONDS)

# Least-significant bits of a 16-bit sample. A downmix and a one-pass filter
# round differently; at this magnitude the difference is around -78 dB, which is
# inaudible, and the bound is loose so a soxr release cannot fail the suite over
# a rounding change.
FILTER_TOLERANCE = 8


def _tone(samples: int = SOURCE_SAMPLES) -> bytes:
    """Half a second of 440 Hz, so a resample has something to preserve."""
    t = np.arange(samples)
    return (np.sin(t * 2 * np.pi * 440 / SOURCE_RATE) * 10_000).astype(np.int16).tobytes()


def _write_clip(
    path,
    rate: int = SOURCE_RATE,
    channels: int = 1,
    width: int = audio_cfg.sample_width,
) -> bytes:
    """A WAV in the chime directory, as an operator would leave one."""
    mono = _tone()
    frames = (
        np.repeat(np.frombuffer(mono, dtype=np.int16), channels).tobytes()
        if channels > 1
        else mono
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(frames)

    return mono


def _largest_difference(first: bytes, second: bytes) -> int:
    return int(
        np.abs(
            np.frombuffer(first, dtype=np.int16).astype(np.int32)
            - np.frombuffer(second, dtype=np.int16).astype(np.int32)
        ).max()
    )


# ── reading ───────────────────────────────────────


async def test_a_clip_is_read_and_made_playable(tmp_path):
    _write_clip(tmp_path / CLIP_NAME)

    playback = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    expected = SOURCE_SAMPLES * audio_cfg.playback_sample_rate // SOURCE_RATE
    assert len(playback) // PLAYBACK_BYTES_PER_FRAME == pytest.approx(expected, abs=2)


async def test_a_clip_is_read_off_disk_only_once(tmp_path):
    path = tmp_path / CLIP_NAME
    _write_clip(path)
    chimes = ChimeLibrary(directory=tmp_path)

    first = await chimes.clip(CLIP_NAME)
    path.unlink()
    second = await chimes.clip(CLIP_NAME)

    assert first == second


async def test_a_stereo_clip_is_folded_down(tmp_path):
    """Discord wants stereo, but the playback path widens mono to get there."""
    _write_clip(tmp_path / CLIP_NAME)
    mono = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    _write_clip(tmp_path / CLIP_NAME, channels=STEREO_CHANNELS)
    stereo = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    assert _largest_difference(mono, stereo) <= FILTER_TOLERANCE


async def test_a_clip_already_at_the_playback_rate_is_not_resampled(tmp_path):
    source = _write_clip(tmp_path / CLIP_NAME, rate=audio_cfg.playback_sample_rate)

    playback = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    assert len(playback) == len(source) * audio_cfg.playback_channels


async def test_a_clip_may_live_in_a_subdirectory(tmp_path):
    _write_clip(tmp_path / "fines" / CLIP_NAME)

    playback = await ChimeLibrary(directory=tmp_path).clip(f"fines/{CLIP_NAME}")

    assert playback != b""


# ── what is refused ───────────────────────────────


async def test_a_missing_clip_plays_nothing(tmp_path, caplog):
    playback = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "No clip" in caplog.text


async def test_a_missing_directory_is_a_missing_clip(tmp_path, caplog):
    """Nothing writes here, so an absent directory is not a degradation."""
    playback = await ChimeLibrary(directory=tmp_path / "never-mounted").clip(CLIP_NAME)

    assert playback == b""
    assert "No clip" in caplog.text


async def test_a_clip_that_is_not_a_wav_plays_nothing(tmp_path, caplog):
    (tmp_path / CLIP_NAME).write_bytes(b"ID3\x04\x00not actually a wav")

    playback = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "unplayable" in caplog.text


async def test_a_clip_that_is_not_16_bit_plays_nothing(tmp_path, caplog):
    _write_clip(tmp_path / CLIP_NAME, width=EIGHT_BIT_WIDTH)

    playback = await ChimeLibrary(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "8-bit" in caplog.text


async def test_a_clip_above_the_chime_directory_is_refused(tmp_path, caplog):
    """A name from configuration is not a licence to read the host."""
    elsewhere = tmp_path / "elsewhere"
    _write_clip(elsewhere / CLIP_NAME)
    chimes = ChimeLibrary(directory=tmp_path / "chimes")

    playback = await chimes.clip(f"../elsewhere/{CLIP_NAME}")

    assert playback == b""
    assert "resolves outside" in caplog.text


async def test_an_absolute_clip_path_is_refused(tmp_path, caplog):
    elsewhere = tmp_path / "elsewhere"
    _write_clip(elsewhere / CLIP_NAME)
    chimes = ChimeLibrary(directory=tmp_path / "chimes")

    playback = await chimes.clip(str(elsewhere / CLIP_NAME))

    assert playback == b""
    assert "resolves outside" in caplog.text


# ── the cache never touches these ─────────────────


async def test_a_chime_outlives_the_speech_cache_reaper(tmp_path):
    """
    The two directories are separate, which is the whole point of the split.

    The reaper sweeps everything in its own directory now; a chime survives
    because it is not in it, not because it is named something safe.
    """
    speech = tmp_path / "speech"
    chime = speech / "chimes" / CLIP_NAME
    _write_clip(chime)

    SpeechCache(directory=speech / "cache", retention_days=1)

    assert chime.is_file()
