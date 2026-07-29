"""Rendering a phrase once and keeping it."""

import os
import time
import wave
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from config import audio_cfg, tts_cfg
from tts import cache as cache_module
from tts.cache import SpeechCache
from tts.client import Speech, SynthesisError

PHRASE = "you are fined one credit"
OTHER_PHRASE = "and another one"

SOURCE_RATE = 24_000
SOURCE_SECONDS = 0.5
SOURCE_SAMPLES = int(SOURCE_RATE * SOURCE_SECONDS)

CHUNKS = 4
KEEP_TWO = 2

# Least-significant bits of a 16-bit sample. Chunked and one-pass filtering
# round differently; at this magnitude the difference is around -78 dB, which is
# inaudible, and the bound is loose so a soxr release cannot fail the suite over
# a rounding change.
FILTER_TOLERANCE = 8


def _tone(samples: int = SOURCE_SAMPLES) -> bytes:
    """Half a second of 440 Hz, so a resample has something to preserve."""
    t = np.arange(samples)
    return (np.sin(t * 2 * np.pi * 440 / SOURCE_RATE) * 10_000).astype(np.int16).tobytes()


class FakeSynthesizer:
    """Stands in for the Wyoming server, counting how often it is asked."""

    def __init__(self, chunks: int = CHUNKS, fail_after: int | None = None) -> None:
        self.calls: list[str] = []
        self._chunks = chunks
        self._fail_after = fail_after

    async def __call__(self, text: str):
        self.calls.append(text)

        pcm = _tone()
        step = len(pcm) // self._chunks

        for index in range(self._chunks):
            if self._fail_after is not None and index == self._fail_after:
                raise SynthesisError("the synthesizer hung up mid-phrase")
            yield Speech(rate=SOURCE_RATE, pcm=pcm[index * step : (index + 1) * step])


@pytest.fixture
def synthesizer(monkeypatch) -> FakeSynthesizer:
    fake = FakeSynthesizer()
    monkeypatch.setattr(cache_module, "synthesize", fake)
    return fake


async def _collect(cache: SpeechCache, text: str = PHRASE) -> bytes:
    return b"".join([chunk async for chunk in cache.stream(text)])


def _cached_files(directory) -> list:
    return sorted(directory.glob("*.wav"))


def _largest_difference(first: bytes, second: bytes) -> int:
    return int(
        np.abs(
            np.frombuffer(first, dtype=np.int16).astype(np.int32)
            - np.frombuffer(second, dtype=np.int16).astype(np.int32)
        ).max()
    )


# ── synthesis ─────────────────────────────────────


async def test_a_phrase_is_synthesized_on_first_ask(synthesizer, tmp_path):
    played = await _collect(SpeechCache(directory=tmp_path))

    assert synthesizer.calls == [PHRASE]
    assert len(played) > 0


async def test_the_clip_is_playback_ready(synthesizer, tmp_path):
    """48 kHz stereo, which is the only thing Discord's player accepts."""
    played = await _collect(SpeechCache(directory=tmp_path))
    samples = np.frombuffer(played, dtype=np.int16)

    expected_frames = SOURCE_SAMPLES * audio_cfg.playback_sample_rate // SOURCE_RATE

    assert len(samples) // audio_cfg.playback_channels == expected_frames
    assert np.array_equal(samples[0::2], samples[1::2])


async def test_audio_arrives_before_synthesis_finishes(synthesizer, tmp_path):
    """The point of streaming: playback starts on the first chunk."""
    cache = SpeechCache(directory=tmp_path)

    chunks = [chunk async for chunk in cache.stream(PHRASE)]

    assert len(chunks) > 1


# ── memory ────────────────────────────────────────


async def test_a_second_ask_is_not_synthesized_again(synthesizer, tmp_path):
    cache = SpeechCache(directory=tmp_path)

    first = await _collect(cache)
    second = await _collect(cache)

    assert synthesizer.calls == [PHRASE]
    assert first == second


async def test_a_different_phrase_is_synthesized(synthesizer, tmp_path):
    cache = SpeechCache(directory=tmp_path)

    await _collect(cache, PHRASE)
    await _collect(cache, OTHER_PHRASE)

    assert synthesizer.calls == [PHRASE, OTHER_PHRASE]


async def test_the_oldest_clip_is_retired_when_memory_is_full(synthesizer, tmp_path):
    """A display name is not a closed set, so the cache has to have a ceiling."""
    cache = SpeechCache(directory=tmp_path, entries=KEEP_TWO)

    for phrase in ("one", "two", "three"):
        await _collect(cache, phrase)

    assert len(cache._memory) == KEEP_TWO


# ── disk ──────────────────────────────────────────


async def test_a_clip_is_written_to_disk(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path))

    assert len(_cached_files(tmp_path)) == 1


async def test_the_stored_clip_is_the_synthesizers_own_audio(synthesizer, tmp_path):
    """Mono at the source rate: a quarter the size, and playable by ear."""
    await _collect(SpeechCache(directory=tmp_path))

    with wave.open(str(_cached_files(tmp_path)[0]), "rb") as handle:
        assert handle.getframerate() == SOURCE_RATE
        assert handle.getnchannels() == 1
        assert handle.getnframes() == SOURCE_SAMPLES


async def test_a_new_process_reads_the_clip_off_disk(synthesizer, tmp_path):
    """
    A restart should not re-pay for what has already been said once.

    The clip off disk is filtered in one pass where the streamed one was
    filtered in chunks, so the two are equal to within a rounding step rather
    than byte for byte.
    """
    first = await _collect(SpeechCache(directory=tmp_path))

    second = await _collect(SpeechCache(directory=tmp_path))

    assert synthesizer.calls == [PHRASE]
    assert len(first) == len(second)
    assert _largest_difference(first, second) <= FILTER_TOLERANCE


async def test_changing_the_voice_does_not_serve_the_old_one(synthesizer, tmp_path, monkeypatch):
    cache = SpeechCache(directory=tmp_path)
    await _collect(cache)

    monkeypatch.setattr(cache_module, "tts_cfg", replace(tts_cfg, voice="someone-else"))
    await _collect(SpeechCache(directory=tmp_path))

    assert synthesizer.calls == [PHRASE, PHRASE]
    assert len(_cached_files(tmp_path)) == 2


async def test_an_unwritable_directory_costs_persistence_only(synthesizer, tmp_path, caplog):
    blocked = tmp_path / "file-not-a-directory"
    blocked.write_text("")

    with caplog.at_level("WARNING"):
        cache = SpeechCache(directory=blocked / "cache")

    played = await _collect(cache)

    assert len(played) > 0
    assert await _collect(cache) == played
    assert synthesizer.calls == [PHRASE]
    assert any("memory" in record.message for record in caplog.records)


async def test_an_unreadable_clip_is_re_synthesized(synthesizer, tmp_path, caplog):
    await _collect(SpeechCache(directory=tmp_path))
    _cached_files(tmp_path)[0].write_bytes(b"not a wav")

    with caplog.at_level("ERROR"):
        played = await _collect(SpeechCache(directory=tmp_path))

    assert len(played) > 0
    assert synthesizer.calls == [PHRASE, PHRASE]


# ── failure ───────────────────────────────────────


async def test_a_failed_synthesis_is_not_cached(monkeypatch, tmp_path, caplog):
    """A fragment cached is a fragment played forever."""
    failing = FakeSynthesizer(fail_after=2)
    monkeypatch.setattr(cache_module, "synthesize", failing)
    cache = SpeechCache(directory=tmp_path)

    with caplog.at_level("ERROR"):
        partial = await _collect(cache)

    assert len(partial) > 0
    assert cache._memory == {}
    assert _cached_files(tmp_path) == []


async def test_a_failed_synthesis_does_not_reach_the_caller(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "synthesize", FakeSynthesizer(fail_after=0))

    assert await _collect(SpeechCache(directory=tmp_path)) == b""


async def test_a_retry_after_a_failure_can_succeed(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "synthesize", FakeSynthesizer(fail_after=1))
    cache = SpeechCache(directory=tmp_path)
    await _collect(cache)

    working = FakeSynthesizer()
    monkeypatch.setattr(cache_module, "synthesize", working)

    assert len(await _collect(cache)) > 0
    assert working.calls == [PHRASE]


# ── laziness ──────────────────────────────────────


async def test_nothing_happens_until_the_stream_is_drained(synthesizer, tmp_path):
    """
    A caller queues the stream behind whatever is playing. Resolving the cache
    at that point rather than when it was handed over is what lets an identical
    phrase ahead of it fill the cache first.
    """
    cache = SpeechCache(directory=tmp_path)

    queued = cache.stream(PHRASE)
    assert synthesizer.calls == []

    await _collect(cache)
    assert synthesizer.calls == [PHRASE]

    assert b"".join([chunk async for chunk in queued]) != b""
    assert synthesizer.calls == [PHRASE]


# ── clips kept by hand ────────────────────────────


CLIP_NAME = "chime.wav"
STEREO_CHANNELS = 2
EIGHT_BIT_WIDTH = 1
PLAYBACK_BYTES_PER_FRAME = audio_cfg.playback_channels * audio_cfg.sample_width


def _write_clip(
    path,
    rate: int = SOURCE_RATE,
    channels: int = 1,
    width: int = audio_cfg.sample_width,
) -> bytes:
    """A WAV in the cache directory, as an operator would leave one."""
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


async def test_a_clip_is_read_and_made_playable(tmp_path):
    _write_clip(tmp_path / CLIP_NAME)
    cache = SpeechCache(directory=tmp_path)

    playback = await cache.clip(CLIP_NAME)

    expected = SOURCE_SAMPLES * audio_cfg.playback_sample_rate // SOURCE_RATE
    assert len(playback) // PLAYBACK_BYTES_PER_FRAME == pytest.approx(expected, abs=2)


async def test_a_clip_is_read_off_disk_only_once(tmp_path):
    path = tmp_path / CLIP_NAME
    _write_clip(path)
    cache = SpeechCache(directory=tmp_path)

    first = await cache.clip(CLIP_NAME)
    path.unlink()
    second = await cache.clip(CLIP_NAME)

    assert first == second


async def test_a_clip_is_not_evicted_to_make_room_for_speech(synthesizer, tmp_path):
    """A clip was put there deliberately; a phrase was said once."""
    _write_clip(tmp_path / CLIP_NAME)
    cache = SpeechCache(directory=tmp_path, entries=1)

    held = await cache.clip(CLIP_NAME)
    await _collect(cache, PHRASE)
    await _collect(cache, OTHER_PHRASE)

    assert await cache.clip(CLIP_NAME) == held


async def test_a_stereo_clip_is_folded_down(tmp_path):
    """Discord wants stereo, but the playback path widens mono to get there."""
    _write_clip(tmp_path / CLIP_NAME)
    mono = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    _write_clip(tmp_path / CLIP_NAME, channels=STEREO_CHANNELS)
    stereo = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    assert _largest_difference(mono, stereo) <= FILTER_TOLERANCE


async def test_a_clip_already_at_the_playback_rate_is_not_resampled(tmp_path):
    source = _write_clip(tmp_path / CLIP_NAME, rate=audio_cfg.playback_sample_rate)

    playback = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    assert len(playback) == len(source) * audio_cfg.playback_channels


async def test_a_missing_clip_plays_nothing(tmp_path, caplog):
    playback = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "No clip" in caplog.text


async def test_a_clip_that_is_not_a_wav_plays_nothing(tmp_path, caplog):
    (tmp_path / CLIP_NAME).write_bytes(b"ID3\x04\x00not actually a wav")

    playback = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "unplayable" in caplog.text


async def test_a_clip_that_is_not_16_bit_plays_nothing(tmp_path, caplog):
    _write_clip(tmp_path / CLIP_NAME, width=EIGHT_BIT_WIDTH)

    playback = await SpeechCache(directory=tmp_path).clip(CLIP_NAME)

    assert playback == b""
    assert "8-bit" in caplog.text


async def test_a_clip_may_live_in_a_subdirectory(tmp_path):
    _write_clip(tmp_path / "chimes" / CLIP_NAME)

    playback = await SpeechCache(directory=tmp_path).clip(f"chimes/{CLIP_NAME}")

    assert playback != b""


async def test_a_clip_above_the_cache_directory_is_refused(tmp_path, caplog):
    """A name from configuration is not a licence to read the host."""
    elsewhere = tmp_path / "elsewhere"
    _write_clip(elsewhere / CLIP_NAME)
    cache = SpeechCache(directory=tmp_path / "cache")

    playback = await cache.clip(f"../elsewhere/{CLIP_NAME}")

    assert playback == b""
    assert "resolves outside" in caplog.text


async def test_an_absolute_clip_path_is_refused(tmp_path, caplog):
    elsewhere = tmp_path / "elsewhere"
    _write_clip(elsewhere / CLIP_NAME)
    cache = SpeechCache(directory=tmp_path / "cache")

    playback = await cache.clip(str(elsewhere / CLIP_NAME))

    assert playback == b""
    assert "resolves outside" in caplog.text


# ── retention ─────────────────────────────────────


RETAIN_DAYS = 90
ONE_DAY_PAST_IT = RETAIN_DAYS + 1
LONG_ENOUGH_AGO = timedelta(days=ONE_DAY_PAST_IT).total_seconds()
RETENTION_OFF = 0


def _age(path, seconds: float = LONG_ENOUGH_AGO) -> None:
    """Backdate a file, as the passage of ninety days would."""
    aged = time.time() - seconds
    os.utime(path, (aged, aged))


async def test_playing_a_clip_keeps_it_alive(synthesizer, tmp_path):
    """
    A phrase said every day should not be reaped for having been rendered once.

    The point of the touch: the second ask is served out of memory and never
    opens the file, so nothing else would say the clip is still wanted.
    """
    cache = SpeechCache(directory=tmp_path, retention_days=RETENTION_OFF)
    await _collect(cache)

    stored = _cached_files(tmp_path)[0]
    _age(stored)
    await _collect(cache)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert stored.is_file()


async def test_a_clip_read_off_disk_is_kept_alive(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path, retention_days=RETENTION_OFF))
    stored = _cached_files(tmp_path)[0]
    _age(stored)

    await _collect(SpeechCache(directory=tmp_path, retention_days=RETENTION_OFF))
    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert stored.is_file()


async def test_a_clip_nobody_has_played_is_reaped_at_startup(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS))
    _age(_cached_files(tmp_path)[0])

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert _cached_files(tmp_path) == []


async def test_a_clip_inside_the_window_is_left_alone(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS))

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert len(_cached_files(tmp_path)) == 1


async def test_a_clip_kept_by_hand_is_never_reaped(synthesizer, tmp_path):
    """A chime was put there deliberately, and nothing here rendered it."""
    _write_clip(tmp_path / CLIP_NAME)
    _age(tmp_path / CLIP_NAME)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert (tmp_path / CLIP_NAME).is_file()


async def test_a_clip_in_a_subdirectory_is_never_reaped(synthesizer, tmp_path):
    held = tmp_path / "chimes" / CLIP_NAME
    _write_clip(held)
    _age(held)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert held.is_file()


async def test_retention_below_a_day_reaps_nothing(synthesizer, tmp_path):
    """So a mis-set variable cannot empty the cache."""
    await _collect(SpeechCache(directory=tmp_path, retention_days=RETENTION_OFF))
    _age(_cached_files(tmp_path)[0])

    SpeechCache(directory=tmp_path, retention_days=RETENTION_OFF)

    assert len(_cached_files(tmp_path)) == 1


async def test_a_reaped_clip_is_synthesized_again(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS))
    _age(_cached_files(tmp_path)[0])

    await _collect(SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS))

    assert synthesizer.calls == [PHRASE, PHRASE]


async def test_touching_a_clip_that_was_never_stored_creates_nothing(
    synthesizer, tmp_path
):
    """`touch` would leave an empty WAV behind for a later read to trip over."""
    cache = SpeechCache(directory=tmp_path)
    await _collect(cache)

    _cached_files(tmp_path)[0].unlink()
    await _collect(cache)

    assert _cached_files(tmp_path) == []
