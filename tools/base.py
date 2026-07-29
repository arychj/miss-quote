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
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from transcript.writer import Transcript, TranscriptSession, Utterance


class Tool:
    """
    Base for a transcript tool.

    Constructed once per server that elects into it, so a tool instance may hold
    state for the length of the process, but must expect its handlers to be
    entered concurrently: utterances are transcribed in parallel and dispatched
    as they land, not in the order they were spoken.
    """

    name: str = ""

    def __init__(self, server: str, config: Mapping[str, Any]) -> None:
        self.server = server
        self.config = config

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
