"""
What a transcript tool is.

A tool is handed a server's transcripts and does something with them. It runs at
one or more of three moments, and it says which by defining the matching method:

    async def handle_utterance(self, utterance, session) -> None
    async def handle_finished(self, transcript) -> None
    async def run(self) -> None

None of them is defined on `Tool`, so their absence is meaningful; the runner
inspects each instance once at startup and only calls what is there. A tool that
defines none of them is configured but inert, which the runner reports.

The first two are dispatched: something was said, or a conversation ended. The
third is the tool's own, started once after the bot has connected and left going
for as long as the process is — a tally published on an interval is the one that
exists. A tool that only runs never sees a transcript, which is fine: it is still
that server's tool, built with that server's settings and roster.

All three are coroutines. Anything blocking — a model call, a large read, a
database round trip — is the tool's own business to push onto a thread; the
handlers run on the bot's event loop, and one that blocks stops audio being
received.

A tool may also define:

    async def prewarm(self) -> None
    async def close(self) -> None

`prewarm` the runner calls once, in the background, after the bot has connected.
It is for work a tool can do before anybody asks anything of it, rendering what
it already knows it will have to say being the one that exists. It is also the
first moment at which every tool on a server exists, so it is where to complain
about one that is missing. `close` the runner calls on the way down, after the
services have been cancelled, for whatever has to outlive the process. Neither is
a moment: a tool defining only these handles nothing, and is still reported as
inert.

A tool is handed a `Speaker`, which is how it answers out loud, and a `Topic`,
which is how it puts one line where the channel can read it. Nothing in this
package imports discord: a speaker is somewhere to play audio and a topic is
somewhere to put a line, and the bot supplies both against a voice channel.

It is also handed a `Toolbox` — the other tools its server has enabled — so that
the tool which counts something and the tool which hears it can be two tools. See
`Toolbox` for when to look in it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from miss_quote.config import UNITY_VOLUME
from miss_quote.transcript.writer import Source, Transcript, TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger

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


@runtime_checkable
class Topic(Protocol):
    """Somewhere a tool can put one line where a server can read it."""

    async def publish(self, server: str, line: str) -> bool:
        """
        Put a line up for one server, reporting whether it can be considered up.

        True covers both the line landing and a refusal that will not come out
        differently for being sent again; False is worth another go later. A
        caller is expected to hold a line back until it changes on a True and to
        offer the same one again on a False, so answering True to something that
        was never published silently loses it.
        """
        ...


class SilentTopic:
    """
    A topic with nowhere to put anything.

    The runner's default. False rather than True, so a tool holding a line back
    until it changes keeps holding it: nothing has been published, and saying
    otherwise would lose the line if somewhere to put it ever appeared.
    """

    async def publish(self, server: str, line: str) -> bool:
        logger.debug("Nowhere to publish '%s' for %s.", line, server)

        return False


Found = TypeVar("Found", bound="Tool")


class Toolbox:
    """
    The other tools one server has enabled.

    One box per server, handed to every tool built for it and filled as each is
    built. **Look in it at the moment you need something, not in `__init__`**: a
    server's tools are built in whatever order its config file happens to list
    them, so a tool that resolves a neighbour at construction finds it or does
    not depending on alphabetical luck. By the time anybody has spoken they are
    all in the box.

    Lookup is by class rather than by name, so what a tool depends on is an
    import a reader can follow and a checker can see, rather than a string that
    has to go on matching a registry entry.
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: list[Tool] = list(tools)

    def add(self, tool: Tool) -> None:
        self._tools.append(tool)

    def find(self, kind: type[Found]) -> Found | None:
        """The server's instance of one kind of tool, or None if it has none."""
        for tool in self._tools:
            if isinstance(tool, kind):
                return tool

        return None


@dataclass(frozen=True)
class ToolContext:
    """
    Everything a tool is built with.

    One object rather than a parameter list, because all but one field of it is
    the same for every tool on a server, and a tool that wants none of them
    should not have to name them all to reach the one it does. Everything except
    the server has a default that does nothing, so a test can build a tool from
    the part it is about.
    """

    server: str
    config: Mapping[str, Any] = field(default_factory=dict)
    speaker: Speaker = field(default_factory=SilentSpeaker)
    users: Mapping[int, str] = field(default_factory=dict)
    tools: Toolbox = field(default_factory=Toolbox)
    topic: Topic = field(default_factory=SilentTopic)


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

    def __init__(self, context: ToolContext) -> None:
        self.server = context.server
        self.config = context.config
        self.speaker = context.speaker
        self.users = context.users
        self.tools = context.tools
        self.topic = context.topic

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
class Service(Protocol):
    """A tool with something of its own to do, for as long as the bot is up."""

    async def run(self) -> None: ...


@runtime_checkable
class Warmer(Protocol):
    """A tool with something to prepare before anyone asks it for anything."""

    async def prewarm(self) -> None: ...


@runtime_checkable
class Closer(Protocol):
    """A tool with something to finish before the process goes away."""

    async def close(self) -> None: ...
