"""
Builds each server's tools once, and dispatches to them.

Which handlers a tool has is settled at startup by inspecting the instance, so
the per-utterance path costs a dictionary lookup rather than a `hasattr` per
line. A tool that raises is logged and otherwise invisible: nothing a tool does
may cost an utterance or hold up a disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from config import ServerConfig, file_cfg
from tools.base import FinishedHandler, SilentSpeaker, Speaker, Tool, UtteranceHandler
from tools.registry import TOOLS
from transcript.writer import Transcript, TranscriptSession, Utterance
from utils.logging import get_logger

logger = get_logger(__name__)

UTTERANCE_MOMENT = "an utterance"
FINISHED_MOMENT = "a finished transcript"


class ToolRunner:
    """Holds every server's tool instances and routes events to them."""

    def __init__(
        self,
        servers: Mapping[int, ServerConfig] | None = None,
        registry: Mapping[str, type[Tool]] | None = None,
        speaker: Speaker | None = None,
    ) -> None:
        servers = file_cfg.servers if servers is None else servers
        registry = TOOLS if registry is None else registry

        self._speaker = SilentSpeaker() if speaker is None else speaker
        self._on_utterance: dict[int, list[Tool]] = {}
        self._on_finished: dict[int, list[Tool]] = {}
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
        for name, settings in server.tools.items():
            if not settings.enabled:
                continue

            tool_class = registry.get(name)
            if tool_class is None:
                self.problems.append(
                    f"Server '{server.alias}': no tool named '{name}'; skipping it."
                )
                continue

            try:
                tool = tool_class(
                    server=server.alias,
                    config=settings.config,
                    speaker=self._speaker,
                )
            except Exception as exc:
                self.problems.append(
                    f"Server '{server.alias}': tool '{name}' would not start: {exc}"
                )
                continue

            self._place(server_id, server.alias, name, tool)

    def _place(self, server_id: int, alias: str, name: str, tool: Tool) -> None:
        """File a built tool under the moments it handles."""
        handled = False

        if isinstance(tool, UtteranceHandler):
            self._on_utterance.setdefault(server_id, []).append(tool)
            handled = True

        if isinstance(tool, FinishedHandler):
            self._on_finished.setdefault(server_id, []).append(tool)
            handled = True

        if not handled:
            self.problems.append(
                f"Server '{alias}': tool '{name}' handles neither utterances nor "
                "finished transcripts, so it will never run."
            )
            return

        self._enabled.setdefault(alias, []).append(name)

    def describe(self) -> Mapping[str, Sequence[str]]:
        """Tool names in play, by server alias, for the startup report."""
        return {alias: tuple(sorted(names)) for alias, names in self._enabled.items()}

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
