"""Rendering a phrase once and keeping it."""

import os
import time
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from miss_quote.audio import opus
from miss_quote.config import audio_cfg, tts_cfg
from miss_quote.tts import cache as cache_module
from miss_quote.tts.cache import SpeechCache
from miss_quote.tts.client import Speech, SynthesisError

PHRASE = "you are fined one credit"
OTHER_PHRASE = "and another one"
THIRD_PHRASE = "and one more"

SOURCE_RATE = 24_000
SOURCE_SECONDS = 0.5
SOURCE_SAMPLES = int(SOURCE_RATE * SOURCE_SECONDS)

CHUNKS = 4

# What an Ogg file opens with, so a test can say the container is the container
# without parsing it.
OGG_MAGIC = b"OggS"

# A loose floor for the encoded-versus-samples check. The real ratio on speech
# is about ten; the test only has to fail if the clip stopped being encoded.
SMALLEST_WORTHWHILE_SAVING = 4

# Least-significant bits of a 16-bit sample. Chunked and one-pass filtering
# round differently; at this magnitude the difference is around -78 dB, which is
# inaudible, and the bound is loose so a soxr release cannot fail the suite over
# a rounding change.
FILTER_TOLERANCE = 8

# How far apart the two channels of a widened mono clip may come back. Opus
# codes stereo as mid and side rather than as two independent channels, so a
# duplicated channel survives a round trip as very nearly itself rather than
# exactly. Against a source amplitude of 10,000 this is under 3%, which a real
# stereo mix would clear by an order of magnitude.
CHANNEL_TOLERANCE = 256


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
    """The phrase as samples, which is what most of these assert against."""
    return b"".join([chunk async for chunk in cache.stream(text).pcm()])


async def _packets(cache: SpeechCache, text: str = PHRASE) -> list[bytes]:
    return [packet async for packet in cache.stream(text).packets()]


def _cached_files(directory) -> list:
    return sorted(directory.glob("*.opus"))


def _channel_difference(samples: np.ndarray) -> int:
    """How far apart the left and right of an interleaved clip are."""
    return int(
        np.abs(
            samples[0::2].astype(np.int32) - samples[1::2].astype(np.int32)
        ).max()
    )


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
    """
    48 kHz stereo, which is the only thing Discord's player accepts.

    Length is checked to within a frame rather than exactly. A clip is stored as
    whole 20 ms packets, so the last one is padded out with silence, and half a
    second of audio does not divide into 20 ms any more neatly than any other
    phrase does.
    """
    played = await _collect(SpeechCache(directory=tmp_path))
    samples = np.frombuffer(played, dtype=np.int16)

    expected_frames = SOURCE_SAMPLES * audio_cfg.playback_sample_rate // SOURCE_RATE
    frames = len(samples) // audio_cfg.playback_channels

    assert abs(frames - expected_frames) <= opus.SAMPLES_PER_FRAME
    assert _channel_difference(samples) <= CHANNEL_TOLERANCE


async def test_audio_arrives_before_synthesis_finishes(synthesizer, tmp_path):
    """The point of streaming: playback starts on the first chunk."""
    cache = SpeechCache(directory=tmp_path)

    chunks = [chunk async for chunk in cache.stream(PHRASE).packets()]

    assert len(chunks) > 1


# ── not synthesizing twice ────────────────────────


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








# ── disk ──────────────────────────────────────────


async def test_a_clip_is_written_to_disk(synthesizer, tmp_path):
    await _collect(SpeechCache(directory=tmp_path))

    assert len(_cached_files(tmp_path)) == 1


async def test_the_stored_clip_is_ogg_opus(synthesizer, tmp_path):
    """A container, so the cache directory stays something you can listen to."""
    cache = SpeechCache(directory=tmp_path)
    packets = await _packets(cache)

    stored = _cached_files(tmp_path)[0]

    assert stored.read_bytes().startswith(OGG_MAGIC)
    assert opus.read(stored) == packets


async def test_the_stored_clip_is_a_fraction_of_the_samples(synthesizer, tmp_path):
    """The whole point of storing it encoded."""
    cache = SpeechCache(directory=tmp_path)
    played = await _collect(cache)

    stored = _cached_files(tmp_path)[0].stat().st_size

    assert stored < len(played) // SMALLEST_WORTHWHILE_SAVING


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


async def test_an_unwritable_directory_still_plays_but_caches_nothing(
    synthesizer, tmp_path, caplog
):
    """
    The directory is the only place a clip is kept, so losing it costs the cache.

    Every phrase is synthesized again every time it is said. That is worth
    saying out loud at startup rather than leaving somebody to notice the
    synthesizer is busy — hence an error rather than a warning.
    """
    blocked = tmp_path / "file-not-a-directory"
    blocked.write_text("")

    with caplog.at_level("ERROR"):
        cache = SpeechCache(directory=blocked / "cache")

    played = await _collect(cache)

    assert len(played) > 0
    assert await _collect(cache) == played
    assert synthesizer.calls == [PHRASE, PHRASE]
    assert any("every time it is said" in record.message for record in caplog.records)


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

    assert b"".join([chunk async for chunk in queued.pcm()]) != b""
    assert synthesizer.calls == [PHRASE]


# ── warming ───────────────────────────────────────


async def test_warming_renders_a_phrase_nobody_has_said(synthesizer, tmp_path):
    rendered = await SpeechCache(directory=tmp_path).warm(PHRASE)

    assert rendered
    assert synthesizer.calls == [PHRASE]
    assert len(_cached_files(tmp_path)) == 1


async def test_a_warmed_phrase_is_not_synthesized_when_it_is_said(synthesizer, tmp_path):
    """The point of the exercise: the wait was paid before anyone was waiting."""
    cache = SpeechCache(directory=tmp_path)
    await cache.warm(PHRASE)

    assert len(await _collect(cache)) > 0
    assert synthesizer.calls == [PHRASE]


async def test_warming_what_has_just_been_said_synthesizes_nothing(
    synthesizer, tmp_path
):
    cache = SpeechCache(directory=tmp_path)
    await _collect(cache)

    assert not await cache.warm(PHRASE)
    assert synthesizer.calls == [PHRASE]


async def test_warming_what_is_already_on_disk_synthesizes_nothing(
    synthesizer, tmp_path
):
    """A restart should not re-render what the last process left behind."""
    await SpeechCache(directory=tmp_path).warm(PHRASE)

    assert not await SpeechCache(directory=tmp_path).warm(PHRASE)
    assert synthesizer.calls == [PHRASE]


async def test_warming_twice_synthesizes_once(synthesizer, tmp_path):
    cache = SpeechCache(directory=tmp_path)

    await cache.warm(PHRASE)
    await cache.warm(PHRASE)

    assert synthesizer.calls == [PHRASE]




async def test_warming_with_nowhere_to_keep_it_synthesizes_nothing(
    synthesizer, tmp_path
):
    """Nowhere to keep it makes it a synthesis nobody is ever served."""
    blocked = tmp_path / "file-not-a-directory"
    blocked.write_text("")

    assert not await SpeechCache(directory=blocked / "cache").warm(PHRASE)
    assert synthesizer.calls == []


# ── retention ─────────────────────────────────────


RETAIN_DAYS = 90
ONE_DAY_PAST_IT = RETAIN_DAYS + 1
LONG_ENOUGH_AGO = timedelta(days=ONE_DAY_PAST_IT).total_seconds()
RETENTION_OFF = 0

# A name this cache produces, for standing in for a clip it wrote.
DIGEST = "a" * 64


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


async def test_a_stale_file_the_cache_did_not_write_is_reaped(synthesizer, tmp_path):
    """
    The directory is the cache's, so everything in it is on the same clock.

    Nothing else is supposed to be here — chimes have their own directory — and
    a file that ended up here anyway is one nothing will ever read.
    """
    stray = tmp_path / "left-here-somehow"
    stray.write_bytes(b"whatever this is")
    _age(stray)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert not stray.exists()


async def test_an_orphaned_partial_is_reaped(synthesizer, tmp_path):
    """A process killed mid-write leaves one, and nothing else collects it."""
    orphan = tmp_path / f"{DIGEST}.partial"
    orphan.write_bytes(b"half an ogg container")
    _age(orphan)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert not orphan.exists()


async def test_a_subdirectory_is_left_alone(synthesizer, tmp_path):
    """The scan does not descend, so a directory here is one you still have."""
    nested = tmp_path / "somebodys-directory"
    nested.mkdir()
    held = nested / "kept"
    held.write_bytes(b"not the reaper's business")
    _age(held)
    _age(nested)

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


async def test_a_warmed_clip_nobody_plays_is_still_reaped(synthesizer, tmp_path):
    """Warmed is not the same as wanted, and the reaper is right to take it."""
    await SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS).warm(PHRASE)
    _age(_cached_files(tmp_path)[0])

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert _cached_files(tmp_path) == []


async def test_touching_a_clip_that_was_never_stored_creates_nothing(
    synthesizer, tmp_path
):
    """
    `touch` would leave an empty file behind for a later read to trip over.

    `os.utime` is what keeps it from doing that, and it is asserted directly
    rather than through a play: nothing else would notice, since a read that
    found the empty file would report it and re-synthesize anyway.
    """
    cache = SpeechCache(directory=tmp_path)

    await cache._touch(cache._key(PHRASE))

    assert _cached_files(tmp_path) == []


# ── what an earlier version left behind ───────────


async def test_a_clip_from_the_wav_era_is_reaped(synthesizer, tmp_path):
    """
    Nothing can read it any more, and nothing else would ever delete it.

    A directory filled by a version that stored WAVs should empty on the usual
    clock rather than sit there for the life of the volume.
    """
    stale = tmp_path / f"{DIGEST}.wav"
    stale.write_bytes(b"whatever a wav is")
    _age(stale)

    SpeechCache(directory=tmp_path, retention_days=RETAIN_DAYS)

    assert not stale.exists()


async def test_a_truncated_clip_is_re_synthesized(synthesizer, tmp_path):
    """
    A half-written container is refused rather than played as a short clip.

    The cache writes atomically, so this is a torn volume rather than anything
    this process did — but a clip that simply stops would be cached in memory
    and played that way forever.
    """
    await _collect(SpeechCache(directory=tmp_path))
    stored = _cached_files(tmp_path)[0]
    stored.write_bytes(stored.read_bytes()[:200])

    cache = SpeechCache(directory=tmp_path)
    played = await _collect(cache)

    assert len(played) > 0
    assert synthesizer.calls == [PHRASE, PHRASE]


async def test_a_clip_played_quieter_streams_too(synthesizer, tmp_path):
    """
    The decode must not gate the first frame on the last packet.

    A clip below full volume has to be decoded, and decoding it all before
    yielding any of it would make every such announcement wait for its own end —
    a whole synthesis on a miss, and the whole decode even off disk.
    """
    await _collect(SpeechCache(directory=tmp_path))
    cache = SpeechCache(directory=tmp_path)

    chunks = [chunk async for chunk in cache.stream(PHRASE).pcm()]

    assert len(chunks) > 1


async def test_a_clip_played_quieter_is_decoded_in_batches(synthesizer, tmp_path):
    """
    Neither a packet at a time nor the whole clip at once.

    The decode runs in a thread, so the batch is what decides how often the loop
    is handed back: one hop per packet would cost more in scheduling than the
    decode, and one hop for the clip would put the whole thing in front of the
    first frame.
    """
    await _collect(SpeechCache(directory=tmp_path))
    cache = SpeechCache(directory=tmp_path)

    chunks = [chunk async for chunk in cache.stream(PHRASE).pcm()]
    batch = cache_module.DECODE_BATCH_PACKETS * opus.FRAME_BYTES

    assert all(len(chunk) <= batch for chunk in chunks)
    assert len(chunks) < SOURCE_SAMPLES  # nowhere near one per packet
