"""
What a transcript tool is.

A tool is handed a server's transcripts and does something with them. It runs at
one or more of four moments, and it says which by defining the matching method:

    async def handle_utterance(self, utterance, session) -> None
    async def handle_finished(self, transcript) -> None
    async def handle_joined(self, source) -> None
    async def run(self) -> None

None of them is defined on `Tool`, so their absence is meaningful; the runner
inspects each instance once at startup and only calls what is there. A tool that
defines none of them is configured but inert, which the runner reports.

The first three are dispatched: something was said, a conversation ended, or the
bot took up a channel. The last is the tool's own, started once after the bot has
connected and left going for as long as the process is — a tally published on an
interval is the one that exists. A tool that only runs never sees a transcript,
which is fine: it is still that server's tool, built with that server's settings
and roster.

The join is the odd one out, in that nothing was said and nothing is being read.
It is for a tool whose output lives *on* the channel rather than in a transcript:
the room the bot is addressing has just changed, and anything already put where
that room can see it is now hanging under somewhere else. A tool with nothing on
a channel has no use for it.

All four are coroutines. Anything blocking — a model call, a large read, a
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

A tool is handed a `Topic`, which is how it puts one line where the channel can
read it, an `Announcer`, which is how it posts something longer somewhere it will
still be later, and a `Ticker`, which is how it keeps one message and goes on
rewriting it. Nothing in this package imports discord: all three are somewhere to
put words, and the bot supplies them against a voice channel and a named text
channel. A `Speaker` — somewhere to play audio — is handed over on the same
terms, but only the tool that owns playback reads it; everything else answers out
loud by asking that tool.

It is also handed a `Toolbox` — the other tools its server has enabled — so that
the tool which counts something and the tool which hears it can be two tools. A
tool says which of its neighbours it uses in `requires`, and the box it is given
serves those and nothing else. See `Toolbox` for when to look in it.
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
        0.5 is half as loud as that. A tool with a reason to be quieter than
        usual has no business knowing what usual is.
        """
        ...


class SilentSpeaker:
    """
    A speaker with nowhere to play.

    The runner's default, so the tool that plays audio always has one and never
    has to check. The audio is left unconsumed rather than drained: on a cache
    miss, draining it would pay a synthesizer to render something nobody can
    hear.
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


@runtime_checkable
class Announcer(Protocol):
    """Somewhere a tool can post something worth reading later."""

    async def post(self, server: str, channel: str, text: str) -> bool:
        """
        Put a body of text in one named channel, saying whether it landed.

        The other half of `Topic`, and a different thing from it: a topic is one
        line that replaces the last one, and this is a message that joins them.
        A summary is written once and read afterwards, which is a post rather
        than a status.

        The channel is named rather than identified, because the tool that asks
        holds a server alias and a channel name and nothing that could resolve
        an ID. Whoever implements this decides what a name means.

        False is worth reporting to whoever asked; nothing retries on its own,
        since what would be sent again is a summary somebody may have already
        read half of.
        """
        ...


@runtime_checkable
class Ticker(Protocol):
    """Somewhere a tool can keep one message that goes on changing."""

    async def show(self, server: str, channel: str, text: str) -> bool:
        """
        Put text in a channel and keep rewriting the same message with it.

        The third of the three, and the one the other two cannot be: `Topic` is
        one line that replaces the last one and holds no history, `Announcer`
        adds a message that joins the ones before it, and this holds one message
        and edits it in place. What it is for is text worth reading while it is
        current and not worth a channel full of messages afterwards — a running
        transcript being the one that exists.

        Whoever implements this owns the message: which one it is, when a new
        one has to be posted because the old one is gone, and what happens to it
        across a restart. The caller says what it should say now.

        False is worth reporting to whoever asked and nothing else; nothing
        retries on its own, since what would be sent again is about to be
        replaced by the next thing to say anyway.
        """
        ...


class SilentTicker:
    """
    A ticker with nowhere to show anything.

    The runner's default, so a tool that has something to show always has one
    and never has to check. False, because nothing was shown.
    """

    async def show(self, server: str, channel: str, text: str) -> bool:
        logger.debug("Nowhere to show %d characters for %s.", len(text), server)

        return False


@runtime_checkable
class Finder(Protocol):
    """An announcer that can say whether a channel name points anywhere."""

    def resolve(self, server: str, channel: str) -> Any | None:
        """
        Whatever the name names, or None if it names nothing.

        Separate from `Announcer` because it is a different kind of thing: one
        posts and the other only looks, and a tool wants to look at startup so
        that a typo in a channel name is a line in the report rather than a
        discovery made when there is finally something to post. An announcer
        with nothing to look through does not have to answer this at all.
        """
        ...


class SilentAnnouncer:
    """
    An announcer with nowhere to post.

    The runner's default, so a tool that posts always has one and never has to
    check. False, because nothing was posted.
    """

    async def post(self, server: str, channel: str, text: str) -> bool:
        logger.debug("Nowhere to post %d characters for %s.", len(text), server)

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

    What each tool is given is a `view` of the box bound to its own class, which
    serves only what that class declared in `requires`. The declaration is what
    the cycle check reads, and a declaration nothing enforces is one that drifts
    away from the call sites it is supposed to describe — at which point the
    check is reading a graph the process does not have.
    """

    def __init__(
        self, tools: Iterable[Tool] = (), owner: type[Tool] | None = None
    ) -> None:
        self._tools: list[Tool] = list(tools)
        self._owner = owner

    def view(self, owner: type[Tool]) -> Toolbox:
        """
        The same box, answering only what one tool said it uses.

        The list is shared rather than copied. A box is filled as its server's
        tools are built, and a view taken at construction would otherwise hold
        whatever had been built by then — which for the first tool is nothing.
        """
        bound = Toolbox(owner=owner)
        bound._tools = self._tools

        return bound

    def add(self, tool: Tool) -> None:
        self._tools.append(tool)

    def find(self, kind: type[Found]) -> Found | None:
        """
        The server's instance of one kind of tool, or None if it has none.

        None is also what an undeclared kind gets, with a line saying so: a tool
        reaching for a neighbour it never said it wanted is a `requires` that has
        gone stale, and serving it would leave the startup cycle check walking a
        graph that is missing an edge.
        """
        if self._owner is not None and kind not in self._owner.requires:
            logger.error(
                "Tool '%s' asked for %s without declaring it in 'requires'; "
                "refusing to serve it.",
                self._owner.name,
                kind.__name__,
            )
            return None

        for tool in self._tools:
            if isinstance(tool, kind):
                return tool

        return None


def cycles(classes: Iterable[type[Tool]]) -> list[tuple[type[Tool], ...]]:
    """
    Every circle of tools that require each other, among the ones given.

    A tool resolves its neighbours through the box and calls them, so a circle is
    a stack that does not end. It is worth finding at startup, where it is a line
    in the report, rather than at the moment somebody speaks, where it is the
    process.

    Only the classes handed over are walked. An edge pointing at a tool the
    server did not enable is not a cycle: nothing is in the box to be called, and
    the tool that reaches for it gets the same None it would get anyway.

    Each circle comes back once, starting from whichever of its members was
    reached first, so a report names a cycle rather than one per tool in it.
    Walked in the order the classes were given, so two runs of the same
    configuration report the same thing.
    """
    ordered = list(dict.fromkeys(classes))
    enabled = set(ordered)
    found: list[tuple[type[Tool], ...]] = []
    walked: set[type[Tool]] = set()

    def walk(tool: type[Tool], path: list[type[Tool]]) -> None:
        if tool in path:
            found.append(tuple(path[path.index(tool) :]))
            return

        if tool in walked:
            return

        for required in tool.requires:
            if required in enabled:
                walk(required, [*path, tool])

        # Marked once the whole of it has been walked, so a tool reached again
        # by another route is skipped rather than re-reporting a circle behind
        # it, and a tool still on the path is not mistaken for a finished one.
        walked.add(tool)

    for tool in ordered:
        walk(tool, [])

    return found


@dataclass(frozen=True)
class ToolContext:
    """
    Everything a tool is built with.

    One object rather than a parameter list, because all but one field of it is
    the same for every tool on a server, and a tool that wants none of them
    should not have to name them all to reach the one it does. Everything except
    the server has a default that does nothing, so a test can build a tool from
    the part it is about.

    `speaker` is carried for every tool and read by one. It is somewhere to play
    audio, which is the business of whichever tool owns playback; the rest reach
    that tool through `tools` and never touch this.
    """

    server: str
    config: Mapping[str, Any] = field(default_factory=dict)
    speaker: Speaker = field(default_factory=SilentSpeaker)
    users: Mapping[int, str] = field(default_factory=dict)
    tools: Toolbox = field(default_factory=Toolbox)
    topic: Topic = field(default_factory=SilentTopic)
    announcer: Announcer = field(default_factory=SilentAnnouncer)
    ticker: Ticker = field(default_factory=SilentTicker)


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

    `requires` is the other tools this one calls, as classes. It is what the box
    a tool is given will serve and the only thing the startup cycle check has to
    read, so a neighbour that is used and not declared is refused rather than
    quietly handed over. Declaring one does not make it present: a server is free
    to enable this tool and not that one, and the lookup still comes back None.

    There is no `speaker` here. Playing audio is one tool's job and everything
    else asks it, so a tool that has nothing to say never holds the thing that
    would let it.
    """

    name: str = ""
    requires: tuple[type[Tool], ...] = ()

    def __init__(self, context: ToolContext) -> None:
        self.server = context.server
        self.config = context.config
        self.users = context.users
        self.tools = context.tools
        self.topic = context.topic
        self.announcer = context.announcer
        self.ticker = context.ticker

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
class JoinHandler(Protocol):
    """A tool that wants to know when the bot has taken up a channel."""

    async def handle_joined(self, source: Source) -> None: ...


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
