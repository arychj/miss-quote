"""
Builds each server's tools once, and dispatches to them.

Which moments a tool has is settled at startup by inspecting the instance, so
the per-utterance path costs a dictionary lookup rather than a `hasattr` per
line. A tool that raises is logged and otherwise invisible: nothing a tool does
may cost an utterance, hold up a disconnect, or stop another tool.

Every tool a server has elected into shares one `Toolbox`, which is what lets one
of them call another. The box is handed over at construction and filled as each
tool is built, so it is complete by the time anything is dispatched and only
partly filled while the building is going on — which is why a tool looks in it
when it needs something rather than when it is made. What each tool is given is
its own view of that box, serving what its class declared in `requires`.

Those declarations are walked before anything is built, because a tool that
requires another which requires it back is a stack that does not end. Found here
it is a line in the startup report; found later it is the process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from miss_quote.config import ServerConfig, file_cfg
from miss_quote.tools.base import (
    Closer,
    FinishedHandler,
    Service,
    SilentSpeaker,
    SilentTopic,
    Speaker,
    Tool,
    Toolbox,
    ToolContext,
    Topic,
    UtteranceHandler,
    Warmer,
    cycles,
)
from miss_quote.tools.registry import TOOLS
from miss_quote.transcript.writer import Transcript, TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

UTTERANCE_MOMENT = "an utterance"
FINISHED_MOMENT = "a finished transcript"
PREWARM_MOMENT = "a pre-warm"
SERVICE_MOMENT = "a run of its own"
CLOSE_MOMENT = "a shutdown"

CYCLE_ARROW = " → "


class ToolRunner:
    """Holds every server's tool instances and routes events to them."""

    def __init__(
        self,
        servers: Mapping[int, ServerConfig] | None = None,
        registry: Mapping[str, type[Tool]] | None = None,
        speaker: Speaker | None = None,
        topic: Topic | None = None,
    ) -> None:
        servers = file_cfg.servers if servers is None else servers
        registry = TOOLS if registry is None else registry

        self._speaker = SilentSpeaker() if speaker is None else speaker
        self._topic = SilentTopic() if topic is None else topic
        self._on_utterance: dict[int, list[Tool]] = {}
        self._on_finished: dict[int, list[Tool]] = {}
        self._warming: list[Tool] = []
        self._serving: list[Tool] = []
        self._closing: list[Tool] = []
        self._running: list[asyncio.Task] = []
        self._enabled: dict[str, list[str]] = {}
        self.problems: list[str] = []

        for server_id, server in servers.items():
            self._build_server(server_id, server, registry)

    # ── startup ───────────────────────────────────

    def _build_server(
        self,
        server_id: int,
        server: ServerConfig,
        registry: Mapping[str, type[Tool]],
    ) -> None:
        """
        Build one server's tools, into one box they all share.

        The box is what a tool reaches its neighbours through, so it is made
        before any of them and handed to every one of them — including the ones
        built before whatever they will eventually go looking for. Each gets its
        own view of it, which serves only what that tool's class declared.

        Tools caught in a circle are left unbuilt. The alternative is a server
        that starts and then hangs the first time one of them calls the other,
        which is a worse way to find out and a harder one to read.
        """
        toolbox = Toolbox()
        wanted = self._enabled_classes(server, registry)
        circular = self._circular(server.alias, wanted)

        for name, tool_class in wanted.items():
            if name in circular:
                continue

            settings = server.tools[name]

            try:
                tool = tool_class(
                    ToolContext(
                        server=server.alias,
                        config=settings.config,
                        speaker=self._speaker,
                        users=server.users,
                        tools=toolbox.view(tool_class),
                        topic=self._topic,
                    )
                )
            except Exception as exc:
                self.problems.append(
                    f"Server '{server.alias}': tool '{name}' would not start: {exc}"
                )
                continue

            if self._place(server_id, server.alias, name, tool):
                toolbox.add(tool)

    def _enabled_classes(
        self, server: ServerConfig, registry: Mapping[str, type[Tool]]
    ) -> dict[str, type[Tool]]:
        """
        The classes behind the names one server switched on, in the order it
        listed them.

        Resolved before any of them is built, because the cycle check reads
        classes and it has to read all of them at once. A name nothing answers to
        is reported here rather than where the building happens, so it is
        reported once.
        """
        wanted: dict[str, type[Tool]] = {}

        for name, settings in server.tools.items():
            if not settings.enabled:
                continue

            tool_class = registry.get(name)
            if tool_class is None:
                self.problems.append(
                    f"Server '{server.alias}': no tool named '{name}'; skipping it."
                )
                continue

            wanted[name] = tool_class

        return wanted

    def _circular(self, alias: str, wanted: Mapping[str, type[Tool]]) -> set[str]:
        """
        The names of the tools caught in a circle, reporting each circle once.

        A circle is named in the order it was walked and closed back to where it
        started, so the line reads as the call that would not have returned.
        """
        circular: set[str] = set()

        for circle in cycles(wanted.values()):
            named = [tool.name for tool in circle]
            circular.update(named)

            self.problems.append(
                f"Server '{alias}': tools "
                f"{CYCLE_ARROW.join([*named, named[0]])} require each other in a "
                "circle; none of them will be built."
            )

        return circular

    def _place(self, server_id: int, alias: str, name: str, tool: Tool) -> bool:
        """
        File a built tool under its moments, reporting whether it has any.

        A tool with none is left out of the box as well: it will never do
        anything itself, and it should not be what another tool finds when it
        goes looking for something that works.
        """
        handled = False

        if isinstance(tool, UtteranceHandler):
            self._on_utterance.setdefault(server_id, []).append(tool)
            handled = True

        if isinstance(tool, FinishedHandler):
            self._on_finished.setdefault(server_id, []).append(tool)
            handled = True

        if isinstance(tool, Service):
            self._serving.append(tool)
            handled = True

        if not handled:
            self.problems.append(
                f"Server '{alias}': tool '{name}' handles no moment and has nothing "
                "of its own to run, so it will never run."
            )
            return False

        # After the check, so nothing is prepared for, or awaited on behalf of, a
        # tool that can never use it.
        if isinstance(tool, Warmer):
            self._warming.append(tool)

        if isinstance(tool, Closer):
            self._closing.append(tool)

        self._enabled.setdefault(alias, []).append(name)

        return True

    def describe(self) -> Mapping[str, Sequence[str]]:
        """Tool names in play, by server alias, for the startup report."""
        return {alias: tuple(sorted(names)) for alias, names in self._enabled.items()}

    async def prewarm(self) -> None:
        """
        Let every tool prepare whatever it can prepare in advance.

        Serial rather than concurrent, unlike dispatch: nothing is waiting on
        this, and the tools that have anything to warm are all talking to one
        synthesizer. One at a time leaves that server free for whatever is
        actually being said while this runs.

        A tool that raises is logged and the rest still get their turn, on the
        same terms as the moments: nothing a tool does at startup may cost
        another tool its own.
        """
        for tool in self._warming:
            try:
                await tool.prewarm()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Tool '%s' failed on %s: %s",
                    tool.name,
                    PREWARM_MOMENT,
                    exc,
                    exc_info=exc,
                )

    def start(self) -> Sequence[asyncio.Task]:
        """
        Set going every tool that has something of its own to do.

        The tasks are handed back for the caller to cancel on the way down, and
        cancelling them is what has to happen before `close`: a tool asked to
        write itself out while its own loop is still going would be racing itself
        for the file.

        Once per process, however many readies the gateway sends.
        """
        if self._running:
            return self._running

        self._running = [
            asyncio.create_task(self._serve(tool)) for tool in self._serving
        ]

        return self._running

    async def _stop(self) -> None:
        """
        Bring every running tool to a halt, and wait until it has stopped.

        Cancelling is a request; a task is only over once it has been let go of.
        Gathered rather than awaited in turn so one that takes a moment to unwind
        does not hold up the rest.
        """
        if not self._running:
            return

        for task in self._running:
            task.cancel()

        await asyncio.gather(*self._running, return_exceptions=True)
        self._running = []

    @staticmethod
    async def _serve(tool: Tool) -> None:
        """
        Run one tool's loop, and say so if it stops.

        A service that returns has decided it has nothing to do, which is
        ordinary — a tally with saving switched off says so and stops. One that
        raises has not, and the failure is otherwise silent: nothing is waiting
        on this task, so nobody would ever collect the exception.
        """
        try:
            await tool.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Tool '%s' stopped on %s: %s",
                tool.name,
                SERVICE_MOMENT,
                exc,
                exc_info=exc,
            )

    async def close(self) -> None:
        """
        Let every tool finish whatever has to outlive the process.

        The services are stopped first, and waited for rather than merely
        cancelled: a tool writing itself out while its own loop is still going
        would be racing itself for a file. The caller has usually cancelled them
        already, which this makes an ordering guarantee rather than a hope.

        Serial after that: what is left to do at this point is small and mostly a
        write, and running them together would buy nothing but a log that is
        harder to read when one of them fails.
        """
        await self._stop()

        for tool in self._closing:
            try:
                await tool.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Tool '%s' failed on %s: %s",
                    tool.name,
                    CLOSE_MOMENT,
                    exc,
                    exc_info=exc,
                )

    # ── dispatch ──────────────────────────────────

    async def dispatch_utterance(
        self, session: TranscriptSession, utterance: Utterance
    ) -> None:
        await self._run(
            self._on_utterance.get(session.source.guild_id),
            lambda tool: tool.handle_utterance(utterance, session),
            UTTERANCE_MOMENT,
        )

    async def dispatch_finished(self, transcript: Transcript) -> None:
        await self._run(
            self._on_finished.get(transcript.source.guild_id),
            lambda tool: tool.handle_finished(transcript),
            FINISHED_MOMENT,
        )

    @staticmethod
    async def _run(
        tools: Sequence[Tool] | None,
        call: Callable[[Tool], Awaitable[None]],
        moment: str,
    ) -> None:
        """
        Run every tool for one event, letting each fail on its own.

        Concurrent rather than serial: a tool's latency is its own, and a slow
        one should not delay the rest. Cancellation is re-raised so shutdown is
        not mistaken for a tool failing.
        """
        if not tools:
            return

        results = await asyncio.gather(
            *(call(tool) for tool in tools), return_exceptions=True
        )

        for tool, result in zip(tools, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error(
                    "Tool '%s' failed on %s: %s",
                    tool.name,
                    moment,
                    result,
                    exc_info=result,
                )
