"""
Audio → VAD → transcription orchestration.

Everything here runs on the bot's event loop. Frames arrive from the voice
receive thread via `submit`, VAD segments them per speaker, and each completed
utterance is dispatched as its own task so speakers never queue behind one
another — the serialization that made upstream drop frames under load.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from miss_quote.config import process_cfg, stt_cfg, vad_cfg
from miss_quote.stt.user_state import UserState, UserStateManager
from miss_quote.stt.vad import SileroVAD
from miss_quote.stt.wyoming_client import transcribe
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.writer import TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Speaker:
    """Display identity for a user, refreshed on every frame they send."""
    name: str
    session: TranscriptSession


class STTProcessor:
    """Segments per-speaker audio and turns each utterance into a transcript line."""

    def __init__(self, tools: ToolRunner) -> None:
        self._tools = tools
        self._vad = SileroVAD()
        self._users = UserStateManager(vad_iterator_factory=self._vad.create_iterator)
        self._speakers: dict[int, Speaker] = {}

        self._semaphore = asyncio.Semaphore(stt_cfg.max_concurrent)
        self._pending: set[asyncio.Task] = set()
        self._tool_work: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maintenance: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the running loop and begin periodic maintenance."""
        self._loop = loop
        if self._maintenance is None or self._maintenance.done():
            self._maintenance = loop.create_task(self._run_maintenance())
        logger.info(
            "STT processor ready (max %d concurrent transcriptions).",
            stt_cfg.max_concurrent,
        )

    async def stop(self) -> None:
        """Flush every speaker and wait for in-flight transcriptions to land."""
        if self._maintenance is not None:
            self._maintenance.cancel()
            self._maintenance = None

        self.flush_all("shutdown")
        await self.drain()

    async def drain(self) -> None:
        """
        Wait for all dispatched transcriptions, and then for the tools.

        In that order, because a transcription still in flight has a tool call
        ahead of it. A tool may be mid-sentence when this is called, and cutting
        it off is only worth doing once nothing more is going to reach it.
        """
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

        if self._tool_work:
            await asyncio.gather(*tuple(self._tool_work), return_exceptions=True)

    # ── ingest ────────────────────────────────────

    def submit(
        self, user_id: int, name: str, session: TranscriptSession, pcm: bytes
    ) -> None:
        """
        Hand one resampled frame to the event loop.

        Called from the voice receive thread, so the work is only scheduled here
        — never performed.
        """
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._feed, user_id, name, session, pcm)

    # ── event-loop side ───────────────────────────

    def _feed(
        self, user_id: int, name: str, session: TranscriptSession, pcm: bytes
    ) -> None:
        self._speakers[user_id] = Speaker(name=name, session=session)

        state = self._users.get_or_create(user_id)
        state.raw_buffer.extend(pcm)

        while len(state.raw_buffer) >= vad_cfg.frame_bytes:
            frame = bytes(state.raw_buffer[: vad_cfg.frame_bytes])
            del state.raw_buffer[: vad_cfg.frame_bytes]
            self._process_frame(state, frame)

    def _process_frame(self, state: UserState, frame: bytes) -> None:
        state.vad_iterator(self._vad.frame_to_array(frame), return_seconds=True)

        if state.vad_iterator.triggered:
            # On speech onset, prepend the ring-buffer context so the first
            # syllable is not clipped.
            if not state.is_speaking:
                for previous in state.ring_buffer.drain():
                    state.speech_buffer.extend(previous)
            state.speech_buffer.extend(frame)
            return

        if state.is_speaking:
            self._dispatch(state)

        state.ring_buffer.append(frame)

    # ── flushing ──────────────────────────────────

    def flush_user(self, user_id: int, reason: str) -> None:
        state = self._users.remove(user_id)
        if state is not None:
            self._dispatch(state, reason)

    def flush_all(self, reason: str) -> None:
        for state in self._users.remove_all():
            self._dispatch(state, reason)

    def _dispatch(self, state: UserState, reason: str | None = None) -> None:
        """Detach the buffered utterance and send it for transcription."""
        if not state.is_speaking:
            return

        audio = bytes(state.reset_speech())

        # A forced flush interrupts the VAD mid-utterance; without a reset the
        # iterator stays triggered and the next onset skips its pre-roll.
        if reason is not None:
            self._reset_vad(state)
            logger.info("Flushed user %s speech due to %s.", state.user_id, reason)

        speaker = self._speakers.get(state.user_id)
        if speaker is None:
            return

        task = asyncio.create_task(self._transcribe(state.user_id, speaker, audio))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @staticmethod
    def _reset_vad(state: UserState) -> None:
        reset = getattr(state.vad_iterator, "reset_states", None)
        if callable(reset):
            reset()

    # ── transcription ─────────────────────────────

    async def _transcribe(self, user_id: int, speaker: Speaker, audio: bytes) -> None:
        async with self._semaphore:
            text = await transcribe(audio)

        if not text:
            return

        utterance = await asyncio.to_thread(
            speaker.session.write, user_id, speaker.name, text
        )
        logger.info("📝 [%s] %s", speaker.name, text)

        self._notify_tools(speaker.session, utterance)

    def _notify_tools(self, session: TranscriptSession, utterance: Utterance) -> None:
        """
        Hand a line to the tools without waiting for them.

        Started after the line is on disk, so a tool that reads the file rather
        than the utterance handed to it still sees the same thing, but not
        awaited: a tool that answers out loud takes as long as the announcement
        does, and holding the transcription task open for it would put anything
        the transcription path does next behind an audio clip.
        """
        work = asyncio.create_task(self._tools.dispatch_utterance(session, utterance))
        self._tool_work.add(work)
        work.add_done_callback(self._tool_work.discard)

    # ── maintenance ───────────────────────────────

    async def _run_maintenance(self) -> None:
        """Flush speech that stopped arriving, and retire idle speakers."""
        while True:
            await asyncio.sleep(process_cfg.maintenance_interval_seconds)
            try:
                for state in self._users.stale_speech_states():
                    self._dispatch(state, "speech inactivity")
                for state in self._users.cleanup_inactive():
                    self._dispatch(state, "user timeout")
                    self._speakers.pop(state.user_id, None)
            except Exception as exc:
                logger.error("Maintenance pass failed: %s", exc, exc_info=True)
