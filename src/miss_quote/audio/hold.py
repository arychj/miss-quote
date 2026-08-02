"""
Music under a wait, for a tool that has already said it will be a moment.

The bot answers a question it has to go and think about by saying so and then
going quiet. The announcement covers the first second or two of that; this
covers the rest of it, so a channel hears something being worked on rather than
a room nobody is in.

It is an envelope over a loop rather than a clip that is played. Nothing knows
in advance how long the wait is — that is the entire reason there is a wait —
so the audio has to be able to go on indefinitely and then stop on a signal
that arrives from somewhere else. What that means in practice is three things
this module has to get right.

**It loops.** The clip is read once and yielded frame by frame, wrapping at the
end. A seam that clicks is a property of the file rather than of this, so what
belongs in the chime directory for this purpose is a short passage that meets
itself, not a track.

**It fades.** Up quickly, because the silence it is covering has already begun
by the time it starts; down slowly, because what replaces it is a sentence, and
music that stops dead a beat before somebody starts talking sounds like a fault.
The envelope is linear in amplitude and constant within a frame — a fade of two
seconds is a hundred steps of a hundred and fiftieth of full scale, which is
nowhere near coarse enough to hear as stepping.

**It waits.** Every other clip in this process is finite and is fed to the
player as fast as it can be produced, because the end of it is never far away.
An indefinite one cannot be: the player takes exactly one frame every 20 ms and
never more, so a loop that yielded as fast as it could would fill memory with
audio nobody has heard yet for as long as the model is thinking. Frames are
handed over against a wall clock instead, kept a fixed head start ahead of what
the player will have consumed. That is a stand-in for asking the player how full
it is, and an accurate one, because its rate is fixed and it only ever falls
behind.
"""

from __future__ import annotations

import asyncio
import time
from asyncio import Future
from collections.abc import AsyncIterator
from typing import Any

from miss_quote.audio.gain import scaled
from miss_quote.config import (
    MILLISECONDS_PER_SECOND,
    SILENT_VOLUME,
    audio_cfg,
    tts_cfg,
)

# How loud music under a wait is, as a fraction of however loud the channel
# asked to be interrupted. Present rather than prominent: it is there so the
# channel does not sound dead, and anything louder is competing with the
# sentence it was put there to cover.
DEFAULT_HOLD_VOLUME = 0.15

# A whole fade, as a fraction of one.
COMPLETE = 1.0

# A fade of this or less is no fade, and a head start of it is no head start.
IMMEDIATELY = 0.0


class HoldMusic:
    """
    One clip, played under a wait for as long as the wait lasts.

    Built per wait rather than shared, because the envelope and the position in
    the loop are the state of one performance. The samples behind it are not:
    they come from the chime library, which reads a clip once and holds it for
    the life of the process.

    A clip too short to fill a single frame is no clip at all. It is reported by
    whoever read it, and here it simply produces nothing — the wait is silent,
    which is what it was before anybody configured music, and the answer behind
    it is unaffected.
    """

    def __init__(
        self,
        clip: bytes,
        volume: float = DEFAULT_HOLD_VOLUME,
        fade_in_ms: float | None = None,
        fade_out_ms: float | None = None,
        head_start_ms: float | None = None,
    ) -> None:
        frame_bytes = audio_cfg.playback_frame_bytes

        # Trimmed to whole frames so the loop wraps on a frame boundary and a
        # slice is never short. It costs under 20 ms off the end of the loop,
        # which is less than the accuracy anybody authoring one is working to.
        self._clip = clip[: len(clip) - len(clip) % frame_bytes]

        self._volume = volume
        self._fade_in_ms = (
            tts_cfg.hold_fade_in_ms if fade_in_ms is None else fade_in_ms
        )
        self._fade_out_ms = (
            tts_cfg.hold_fade_out_ms if fade_out_ms is None else fade_out_ms
        )

        # How far ahead of the player to stay. The same span the synthesizer is
        # given before a clip starts, and for the same reason: it is how much
        # audio this deployment considers a comfortable cushion.
        self._head_start_ms = (
            tts_cfg.lead_ms if head_start_ms is None else head_start_ms
        )

        self._position = 0
        self._gain = SILENT_VOLUME
        self._fed_ms = 0.0
        self._started: float | None = None

    @property
    def playable(self) -> bool:
        """Whether there is enough of a clip to loop."""
        return bool(self._clip)

    async def until(self, finished: Future[Any]) -> AsyncIterator[bytes]:
        """
        Frames for as long as something else is still going.

        Watched rather than awaited, which is why this takes a future and not
        any awaitable: the point is to keep playing *while* it runs, and the
        caller is the one that wants its result.

        Re-enterable, and meant to be. A wait is usually two of them back to
        back — a model thinking, and then a synthesizer starting up — and the
        fade-in should span the pair of them rather than restarting halfway
        through. The envelope and the position in the loop are the instance's,
        so a second call carries on from where the first stopped.
        """
        while self.playable and not finished.done():
            yield await self._frame(self._rising())

    async def fading_out(self) -> AsyncIterator[bytes]:
        """
        The way out, from wherever the music currently is down to nothing.

        From the current gain rather than from full, so a wait that ended inside
        the fade-in does not jump up to be faded down from. The configured span
        is what it takes from full, and what is held constant below that is the
        *rate* rather than the duration: a wait short enough that the music
        barely arrived should not be followed by two seconds of something too
        quiet to hear, which is a pause rather than a fade.

        Once this has run there is nothing left to play; a caller that asks
        again gets nothing.
        """
        if self._gain <= SILENT_VOLUME or self._fade_out_ms <= IMMEDIATELY:
            return

        opening = self._gain
        began = self._fed_ms
        span = self._fade_out_ms * opening / self._volume

        while True:
            gone = (self._fed_ms - began) / span
            if gone >= COMPLETE:
                self._gain = SILENT_VOLUME
                return

            yield await self._frame(opening * (COMPLETE - gone))

    def _rising(self) -> float:
        """How loud the music is this far into the fade-in."""
        if self._fade_in_ms <= IMMEDIATELY:
            return self._volume

        return self._volume * min(COMPLETE, self._fed_ms / self._fade_in_ms)

    async def _frame(self, gain: float) -> bytes:
        """
        One frame of the loop at a given loudness, no sooner than it is wanted.

        The pacing comes first and the clock starts on the first frame rather
        than at construction: what this is staying ahead of is the player, and
        the player is armed and reading by the time anything is asked of this.
        """
        await self._paced()

        frame = self._next()

        self._gain = gain
        self._fed_ms += audio_cfg.playback_frame_ms

        return scaled(frame, gain)

    def _next(self) -> bytes:
        """The next frame of the loop, starting the clip over at the end of it."""
        frame_bytes = audio_cfg.playback_frame_bytes
        frame = self._clip[self._position : self._position + frame_bytes]

        self._position += frame_bytes
        if self._position >= len(self._clip):
            self._position = 0

        return frame

    async def _paced(self) -> None:
        """
        Hold off until the player is ready for another frame.

        Measured against what has been handed over rather than against what has
        been played, because nothing here can see the player. The two only ever
        differ by the head start and by however far the player has fallen
        behind, and falling behind is what the head start is for.
        """
        if self._started is None:
            self._started = time.monotonic()
            return

        elapsed = (time.monotonic() - self._started) * MILLISECONDS_PER_SECOND
        ahead = self._fed_ms - elapsed - self._head_start_ms

        if ahead > IMMEDIATELY:
            await asyncio.sleep(ahead / MILLISECONDS_PER_SECOND)
