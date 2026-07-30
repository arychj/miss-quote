"""
What a transcript tool is.

A tool is handed a server's transcripts and does something with them. It runs at
one or both of two moments, and it says which by defining the matching method:

    async def handle_utterance(self, utterance, session) -> None
    async def handle_finished(self, transcript) -> None

Neither is defined on `Tool`, so their absence is meaningful; the runner inspects
each instance once at startup and only calls what is there. A tool that defines
neither is configured but inert, which the runner reports.

Both are coroutines. Anything blocking — a model call, a large read, a database
round trip — is the tool's own business to push onto a thread; the handlers run
on the bot's event loop, and one that blocks stops audio being received.

A tool may also define:

    async def prewarm(self) -> None

which the runner calls once, in the background, after the bot has connected. It
is for work a tool can do before anybody asks anything of it, rendering what it
already knows it will have to say being the one that exists. It is not one of the
moments: a tool defining only this handles nothing, and is still reported as
inert.

A tool is also handed a `Speaker`, which is how it answers out loud. Nothing in
this package imports discord: a speaker is somewhere to play audio, and the bot
supplies one that happens to be a voice channel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, runtime_checkable

from config import UNITY_VOLUME
from transcript.writer import Source, Transcript, TranscriptSession, Utterance
from utils.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class Speaker(Protocol):
    """Somewhere a tool can play audio."""

    async def play(
        self, source: Source, audio: AsyncIterator[bytes], scale: float = UNITY_VOLUME
    ) -> None:
        """
        Play one clip of 48 kHz stereo PCM back where it came from.

        Returns once the clip has finished, so a tool that plays two in a row
        gets them in that order rather than on top of each other.

        `scale` is relative to the deployment's own loudness rather than
        absolute: 1.0 is however loud the channel asked to be interrupted, and
        0.5 is half of that. A tool with a reason to be quieter than usual has
        no business knowing what usual is.
        """
        ...


class SilentSpeaker:
    """
    A speaker with nowhere to play.

    The runner's default, so a tool always has one and never has to check. The
    audio is left unconsumed rather than drained: on a cache miss, draining it
    would pay a synthesizer to render something nobody can hear.
    """

    async def play(
        self, source: Source, audio: AsyncIterator[bytes], scale: float = UNITY_VOLUME
    ) -> None:
        logger.debug("Nothing to play through for %s; dropping a clip.", source.channel)


class Tool:
    """
    Base for a transcript tool.

    Constructed once per server that elects into it, so a tool instance may hold
    state for the length of the process, but must expect its handlers to be
    entered concurrently: utterances are transcribed in parallel and dispatched
    as they land, not in the order they were spoken.

    `users` is that server's roster, by ID, which is the same one the transcript
    labels a speaker from. It is what a tool has that is knowable about who might
    speak before anybody does; it is empty for a server that has not written one,
    and it never covers everybody, since a speaker who is not on it is known by
    whatever Discord reports.
    """

    name: str = ""

    def __init__(
        self,
        server: str,
        config: Mapping[str, Any],
        speaker: Speaker,
        users: Mapping[int, str] | None = None,
    ) -> None:
        self.server = server
        self.config = config
        self.speaker = speaker
        self.users: Mapping[int, str] = {} if users is None else users

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} for {self.server!r}>"


@runtime_checkable
class UtteranceHandler(Protocol):
    """A tool that wants each line as it is transcribed."""

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None: ...


@runtime_checkable
class FinishedHandler(Protocol):
    """A tool that wants the whole conversation once the bot has left."""

    async def handle_finished(self, transcript: Transcript) -> None: ...


@runtime_checkable
class Warmer(Protocol):
    """A tool with something to prepare before anyone asks it for anything."""

    async def prewarm(self) -> None: ...
