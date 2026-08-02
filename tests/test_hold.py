"""Music under a wait: the loop, the envelope over it, and the pace it is fed at."""

import asyncio
import time

import numpy as np
import pytest

from miss_quote.audio.gain import LOUDEST_SAMPLE, LOUDNESS_EXPONENT
from miss_quote.audio.hold import HoldMusic
from miss_quote.config import audio_cfg

FRAME_BYTES = audio_cfg.playback_frame_bytes
FRAME_MS = audio_cfg.playback_frame_ms

# A flat clip, and as loud a one as int16 holds, so the peak of a frame says
# where the envelope was when that frame went out. Near full scale because the
# quiet end of a fade is where the resolution runs out: the envelope is a volume
# and the samples are an amplitude, and reading the first back off the second
# magnifies whatever the truncation lost. A tone would need a window to measure
# and would say nothing extra — what is under test is the envelope, not the
# resampler.
FULL_SCALE = LOUDEST_SAMPLE

HOLD_VOLUME = 0.15
FADE_IN_MS = 500.0
FADE_OUT_MS = 2000.0

FADE_IN_FRAMES = int(FADE_IN_MS / FRAME_MS)
FADE_OUT_FRAMES = int(FADE_OUT_MS / FRAME_MS)

# Far more audio than any test hands over, so nothing waits on the pace unless
# it is the pace being tested.
UNPACED_MS = 600_000.0

# Where the envelope was, read back off samples that were scaled in float and
# truncated to int16. Comfortably finer than any step these tests assert on, and
# loose enough for the last frame of a fade, which is quieter than int16 has a
# value for and therefore reads as the silence it is.
VOLUME_TOLERANCE = 5e-3


def _clip(frames: int, level: int = FULL_SCALE) -> bytes:
    """A flat clip some whole number of playback frames long."""
    samples = frames * FRAME_BYTES // audio_cfg.sample_width

    return np.full(samples, level, dtype=np.int16).tobytes()


def _music(clip: bytes | None = None, **overrides) -> HoldMusic:
    """One performance, unpaced unless a test says otherwise."""
    settings = {
        "volume": HOLD_VOLUME,
        "fade_in_ms": FADE_IN_MS,
        "fade_out_ms": FADE_OUT_MS,
        "head_start_ms": UNPACED_MS,
    }
    settings.update(overrides)

    return HoldMusic(_clip(FADE_IN_FRAMES) if clip is None else clip, **settings)


def _volume(frame: bytes) -> float:
    """
    Where the envelope was for one frame, as a position on the knob.

    The samples carry an amplitude and the envelope is a volume, so this is the
    curve in `audio.gain` run backwards — which is what lets a fade be asserted
    on in the terms it is written in.
    """
    peak = float(np.abs(np.frombuffer(frame, dtype=np.int16)).max()) / FULL_SCALE

    return peak ** (1 / LOUDNESS_EXPONENT)


async def _sustain(music: HoldMusic, frames: int) -> list[bytes]:
    """Exactly `frames` of the loop, by ending the wait once they have arrived."""
    waiting = asyncio.get_running_loop().create_future()
    played: list[bytes] = []

    async for frame in music.until(waiting):
        played.append(frame)
        if len(played) >= frames:
            waiting.set_result(None)

    return played


async def _drain(music: HoldMusic) -> list[bytes]:
    return [frame async for frame in music.fading_out()]


# ── the loop ──────────────────────────────────────


async def test_the_clip_repeats_once_it_reaches_the_end():
    first, second = _clip(1, level=100), _clip(1, level=200)
    music = _music(first + second, volume=1.0, fade_in_ms=0.0)

    played = await _sustain(music, 5)

    assert played == [first, second, first, second, first]


async def test_a_wait_outlasting_the_clip_is_still_covered():
    music = _music(_clip(2), fade_in_ms=0.0)

    played = await _sustain(music, 50)

    assert len(played) == 50
    assert all(len(frame) == FRAME_BYTES for frame in played)


async def test_the_tail_of_a_clip_that_does_not_fill_a_frame_is_dropped():
    music = _music(_clip(2) + b"\x00" * (FRAME_BYTES // 2), volume=1.0, fade_in_ms=0.0)

    played = await _sustain(music, 4)

    assert played == [_clip(1)] * 4


async def test_a_clip_shorter_than_a_frame_is_no_clip_at_all():
    music = _music(b"\x00" * (FRAME_BYTES - 1))

    assert not music.playable
    assert await _sustain(music, 1) == []
    assert await _drain(music) == []


# ── the envelope ──────────────────────────────────


async def test_the_music_arrives_rather_than_starting():
    music = _music()
    halfway = FADE_IN_FRAMES // 2

    played = await _sustain(music, FADE_IN_FRAMES)

    assert _volume(played[0]) == pytest.approx(0.0, abs=VOLUME_TOLERANCE)
    assert _volume(played[halfway]) == pytest.approx(
        HOLD_VOLUME * halfway / FADE_IN_FRAMES, abs=VOLUME_TOLERANCE
    )


async def test_the_fade_in_stops_at_the_loudness_it_was_asked_for():
    music = _music()

    played = await _sustain(music, FADE_IN_FRAMES * 2)

    assert _volume(played[FADE_IN_FRAMES]) == pytest.approx(
        HOLD_VOLUME, abs=VOLUME_TOLERANCE
    )
    assert all(
        _volume(frame) == pytest.approx(HOLD_VOLUME, abs=VOLUME_TOLERANCE)
        for frame in played[FADE_IN_FRAMES:]
    )


async def test_the_fade_in_spans_both_halves_of_a_wait():
    """
    A wait is two of them — the model, then the synthesizer — and the music
    should not start over in between.
    """
    music = _music()

    first = await _sustain(music, FADE_IN_FRAMES // 2)
    second = await _sustain(music, FADE_IN_FRAMES)

    assert _volume(second[0]) > _volume(first[-1])
    assert _volume(second[-1]) == pytest.approx(HOLD_VOLUME, abs=VOLUME_TOLERANCE)


async def test_the_music_leaves_over_the_span_it_was_given():
    music = _music()
    await _sustain(music, FADE_IN_FRAMES + 1)

    leaving = await _drain(music)

    assert len(leaving) == FADE_OUT_FRAMES
    assert _volume(leaving[0]) == pytest.approx(HOLD_VOLUME, abs=VOLUME_TOLERANCE)
    assert _volume(leaving[-1]) == pytest.approx(
        HOLD_VOLUME / FADE_OUT_FRAMES, abs=VOLUME_TOLERANCE
    )


async def test_the_fade_out_starts_from_wherever_the_music_got_to():
    """A wait that ended inside the fade-in should not jump up to fade down."""
    music = _music()
    rising = await _sustain(music, FADE_IN_FRAMES // 2)

    leaving = await _drain(music)

    assert _volume(leaving[0]) == pytest.approx(
        _volume(rising[-1]), abs=VOLUME_TOLERANCE
    )
    assert _volume(leaving[0]) < HOLD_VOLUME


async def test_music_that_barely_arrived_leaves_at_the_same_rate_it_would_have():
    """
    Otherwise a wait that ended almost immediately is followed by two seconds of
    something too quiet to hear, which is a pause rather than a fade.
    """
    music = _music()
    reached = FADE_IN_FRAMES // 5

    rising = await _sustain(music, reached)
    leaving = await _drain(music)

    proportionate = FADE_OUT_FRAMES * _volume(rising[-1]) / HOLD_VOLUME

    assert len(leaving) == pytest.approx(proportionate, abs=1)


async def test_there_is_nothing_left_once_the_music_has_gone():
    music = _music()
    await _sustain(music, FADE_IN_FRAMES)
    await _drain(music)

    assert await _drain(music) == []


async def test_a_wait_that_ended_before_the_music_started_fades_nothing():
    music = _music()

    assert await _drain(music) == []


# ── the pace ──────────────────────────────────────


async def test_the_loop_is_fed_at_the_rate_it_is_played_rather_than_as_fast_as_it_runs():
    """
    The regression that matters most, because it is a leak rather than a sound.
    The player takes one frame every 20 ms and never more, so a loop covering a
    completion that runs for minutes would otherwise buffer minutes of audio
    that nobody has heard yet.
    """
    head_start_ms = 100.0
    covering_ms = 300.0

    music = _music(head_start_ms=head_start_ms)
    began = time.monotonic()

    waiting = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_later(covering_ms / 1000, waiting.set_result, None)

    played = [frame async for frame in music.until(waiting)]

    handed_over_ms = len(played) * FRAME_MS
    elapsed_ms = (time.monotonic() - began) * 1000

    assert handed_over_ms == pytest.approx(
        covering_ms + head_start_ms, abs=FRAME_MS * 3
    )
    assert handed_over_ms - elapsed_ms < head_start_ms + FRAME_MS * 3
