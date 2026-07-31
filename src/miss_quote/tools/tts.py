"""
The tool that speaks, and the only thing that plays anything.

Every other tool answers out loud by finding this one in its `Toolbox` and
handing it a sentence. That is the whole point of it being a tool rather than a
module: what a server can hear is something the config file elects into, a tool
that is missing is reported where every other missing tool is, and the cache, the
chime library, the volume and the voice connection are behind one door instead of
three imports and a composition step in whoever happened to need it first.

`play` decides how a clip reaches Discord, and the decision is worth stating
because it is the difference between free and not. A phrase with nothing in front
of it and nothing to be done to it goes out exactly as it was stored — Opus
packets, no decode, no encode, no resample. A chime, or any volume below the
channel's own, means samples: a chime is a hand-placed WAV that has to be joined
onto the front, and a gain is a multiplication, and neither is a thing you can do
to an encoded packet. The speaker makes that call from the gain and the type it
is given, so this decides only whether to hand over a phrase or a stream.

`enqueue` and `run` are the other half. A tool that can work out at startup what
it will have to say says so, and this renders it in the background while the bot
is already in the channel — rather than each tool blocking the start-up on its
own synthesizer round trips. Rendering is serial process-wide, which the cache
enforces; see `SpeechCache.warm`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from miss_quote.audio.chimes import shared_chimes
from miss_quote.config import UNITY_VOLUME, tts_cfg
from miss_quote.tools.base import Tool, ToolContext
from miss_quote.transcript.writer import Source
from miss_quote.tts.cache import shared_cache
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# How often the renderer says what it has been doing. A warm-up is a few dozen
# phrases and a line per phrase is a log nobody reads; one line per batch is the
# same information at a length somebody will.
REPORT_EVERY = 25


class Tts(Tool):
    """Says what another tool has decided to say, and plays it where it belongs."""

    name = "tts"

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)

        self._speaker = context.speaker
        self._speech = shared_cache()
        self._chimes = shared_chimes()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._rendered = 0
        self._held = 0
        self._reported = 0

    # ── speaking ──────────────────────────────────

    async def play(
        self,
        source: Source,
        text: str,
        *,
        scale: float = UNITY_VOLUME,
        chime: str | None = None,
        keep: bool = True,
    ) -> None:
        """
        Say one thing, where it was said to, at the loudness it asked for.

        Returns once the clip has finished, so a tool that says two things in a
        row gets them in that order rather than on top of each other.

        `scale` is relative to the deployment's own loudness rather than
        absolute: 1.0 is however loud the channel asked to be interrupted, and
        0.5 is half of that. A tool with a reason to be quieter than usual has no
        business knowing what usual is.

        `chime` names a clip in the chime directory, without its extension, to
        play ahead of the words. A chime that is missing costs the chime and not
        the announcement.

        `keep` is False for a sentence composed for one moment and never said
        again — the account of one evening, read out once. The cache is for
        phrases that come round again; see `SpeechCache.stream`.
        """
        if chime is None:
            # A phrase on its own, which the speaker can send as it was stored
            # if nothing has to be done to it first.
            await self._speaker.play(
                source, self._speech.stream(text, keep=keep), scale
            )
            return

        await self._speaker.play(source, self._announce(text, chime, keep), scale)

    async def _announce(
        self, text: str, chime: str, keep: bool = True
    ) -> AsyncIterator[bytes]:
        """
        The chime and then the words, as one clip rather than two.

        Samples rather than the packets the cache holds, and unavoidably: the
        chime is a hand-placed WAV, and there is nothing to join an encoded
        packet onto. The decode that costs is a few milliseconds on a clip that
        is about to be spoken over a channel.

        Two calls to the speaker would play in order — it holds one lock per
        server — but each arms the player afresh, and the gap between them is
        audible. Chaining them puts the chime in front of the same stream.

        The words are given a head start before the chime is handed over. A
        synthesizer is free to render a phrase whole before sending any of it,
        and the chime is short; starting it the moment it is read would leave the
        player waiting between the flourish and the sentence it introduces.
        Waiting first spends that time before anything is playing.
        """
        opening = await self._chimes.clip(chime)
        words = self._speech.stream(text, keep=keep).pcm()
        lead = await _lead(words, tts_cfg.lead_bytes)

        if opening:
            yield opening

        for chunk in lead:
            yield chunk

        async for chunk in words:
            yield chunk

    def locate(self, chime: str | None) -> str | None:
        """
        The name of a chime a tool has been configured with, if it is usable.

        Looked for when a tool asks rather than when it is built, so a name that
        is not there is a line in the log on the way up instead of a discovery
        made the first time somebody sets one off. Reported rather than raised
        on, and kept either way: a missing chime should cost the chime, and the
        file may yet arrive in a directory that is usually a mounted volume.
        """
        if chime is None:
            return None

        name = str(chime).strip()
        if not name:
            return None

        path = self._chimes.path(name)
        if path is None or not path.is_file():
            logger.warning(
                "[%s] No chime at '%s'; it will be left out of whatever plays it.",
                self.server,
                path or name,
            )

        return name

    # ── rendering ─────────────────────────────────

    def enqueue(self, phrases: Iterable[str]) -> int:
        """
        Line up phrases to be rendered before anybody asks for them, saying how
        many are new.

        Cheap and synchronous, so a tool's warm-up is the list it can think of
        rather than the time a synthesizer takes to say it. What happens to the
        list is `run`'s business.

        A phrase already queued is dropped here. Two servers with a name in
        common ask for the same sentence, and the second one is a filesystem
        check and a queue slot for something the first has already dealt with.
        """
        queued = 0

        for phrase in phrases:
            if phrase in self._queued:
                continue

            self._queued.add(phrase)
            self._queue.put_nowait(phrase)
            queued += 1

        return queued

    async def drained(self) -> None:
        """
        Return once everything queued so far has been rendered.

        Rendering in advance is work nobody waits on, so this is for the two
        things that do: a shutdown that would rather not walk away from a
        half-finished backlog, and anything checking that the queue reached the
        end of itself.
        """
        await self._queue.join()

    async def run(self) -> None:
        """
        Render whatever has been lined up, for as long as the process is.

        A loop rather than a pass over a list, because the list is not complete
        when this starts: every tool's warm-up runs as a background task
        alongside this one, and a tool is free to think of something later.

        One phrase at a time, and the cache holds that to one across the whole
        process. Nothing is waiting on any of it — the point of rendering in
        advance is that the channel is not — so a phrase that will not synthesize
        is a line in the log and the next phrase, never the end of the run.
        """
        while True:
            phrase = await self._queue.get()

            try:
                if await self._speech.warm(phrase):
                    self._rendered += 1
                else:
                    self._held += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[%s] Could not render %r: %s", self.server, phrase, exc)
            finally:
                self._queue.task_done()

            self._report()

    def _report(self) -> None:
        """
        Say how the warm-up is going, on a batch and at the end of the queue.

        Both, because neither is enough on its own: without the batch a long
        warm-up is silent for minutes, and without the drain a queue that ends
        one short of a batch never says how it went. Nothing is said twice for
        the same work, so a queue that is empty most of the time — a tool
        thinking of a phrase now and then — does not narrate each one.
        """
        done = self._rendered + self._held
        outstanding = done - self._reported

        if outstanding < REPORT_EVERY and not (outstanding and self._queue.empty()):
            return

        self._reported = done

        logger.info(
            "[%s] Rendered %d phrase(s) in advance; %d were already cached.",
            self.server,
            self._rendered,
            self._held,
        )


async def _lead(speech: AsyncIterator[bytes], wanted: int) -> list[bytes]:
    """
    Pull from a stream until it has given up `wanted` bytes or run out.

    The chunks are handed back rather than joined, so nothing is copied and a
    short phrase that ends inside the head start is not padded out to it. The
    stream is left where it stopped for the caller to finish draining.
    """
    if wanted <= 0:
        return []

    lead: list[bytes] = []
    held = 0

    async for chunk in speech:
        lead.append(chunk)
        held += len(chunk)
        if held >= wanted:
            break

    return lead
