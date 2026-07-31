"""
Rendered speech, kept so a phrase is only ever synthesized once.

One layer: Opus packets in an Ogg container under `SPEECH_DIR/cache`, one packet
per 20 ms, exactly as Discord takes them. About a tenth the size of the samples
they came from, and playable, so you can hear what the bot actually said.

Storing what Discord wants rather than what the synthesizer produced is what
makes a cached phrase free to play. `stream` hands the packets over and
`is_opus` tells discord.py to send them untouched: no resample, no encode, no
decode, nothing per play at all. The cost of that is that the stored bitrate is
the delivered bitrate — see `audio.opus` — and that a clip which has to be
*changed* on the way out has to be decoded first, which is what `Phrase.pcm` is
for and what any volume below full needs.

There was a second layer in front of this one, holding the same packets in
memory. It was measured at a quarter of a millisecond off the way to playback,
against a frame of twenty, and it did not even save a filesystem round trip: the
reaper ages clips by mtime, so every hit calls `os.utime` whether or not it read
the file. What it cost was an eviction policy, a bound, and a setting to tune
the bound with. It is gone, and a hit is now a file read.

Which makes the directory load-bearing rather than optional. An unwritable or
absent one no longer costs only the persistence — with nowhere to put a clip,
every phrase is synthesized again every time it is said, and `warm` declines to
render anything at all rather than paying a synthesizer for audio that would be
thrown away.

Only rendered speech lives here. Clips nobody synthesized — a chime a tool plays
ahead of what it has to say — are the operator's rather than this process's, and
live in their own directory; see `audio.chimes`.

Rendered speech is reaped at startup once it has gone unplayed for long enough.
See `_reap`.

A caller that can work out in advance what it will have to say can render it
before it is needed with `warm`, which costs nothing for a phrase already held.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from discord.oggparse import OggError

from miss_quote.audio import opus
from miss_quote.audio.resampler import PlaybackResampler
from miss_quote.config import speech_cfg, tts_cfg
from miss_quote.tts.client import SynthesisError, synthesize
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

CACHE_SUFFIX = ".opus"

# What a clip is written as before it is whole. An orphan means a process killed
# mid-write; the reaper collects it on the usual clock along with everything
# else, since nothing will ever read it.
PARTIAL_SUFFIX = ".partial"

# How much audio is decoded per hop into a thread, for a clip being played
# quieter. A packet takes microseconds, so one hop each would cost more in
# scheduling than the decode; a hop for the whole clip would make the first
# frame wait for the last packet, which on a miss is the whole synthesis. A
# tenth of a second is short enough that neither happens.
DECODE_BATCH_MS = 100
DECODE_BATCH_PACKETS = DECODE_BATCH_MS // opus.FRAME_MILLISECONDS

KEY_SEPARATOR = "\n"
BYTES_PER_KIB = 1024

# Below this the reaper does nothing, so a mis-set variable cannot empty the
# cache and 0 is a no-op rather than "delete everything".
MINIMUM_RETENTION_DAYS = 1


class Phrase:
    """
    One phrase, in whichever form the player can take.

    Handed to the speaker instead of a stream, so that the choice between
    sending a clip as it was stored and decoding it first is made where the
    volume is known. Nothing is read, synthesized, or decoded until one of the
    two is asked for, and only one of them ever is.

    Named for what it is rather than what it holds, because `tts.client.Speech`
    is already a chunk of audio coming off the synthesizer and two things called
    the same in one pipeline is how the wrong one gets imported.
    """

    __slots__ = ("_cache", "_text")

    def __init__(self, cache: SpeechCache, text: str) -> None:
        self._cache = cache
        self._text = text

    def packets(self) -> AsyncIterator[bytes]:
        """The phrase encoded, which is how it is stored and how Discord takes it."""
        return self._cache.encoded(self._text)

    def pcm(self) -> AsyncIterator[bytes]:
        """The phrase as samples, for a clip that has to be changed on the way out."""
        return self._cache.samples(self._text)


class SpeechCache:
    """
    Speech for a phrase, synthesized on first ask and kept thereafter.

    One instance serves the whole process. Nothing here is per server: the same
    words in the same voice are the same audio wherever they were asked for.
    """

    def __init__(
        self,
        directory: Path | None = None,
        retention_days: int | None = None,
    ) -> None:
        self._retention_days = (
            tts_cfg.cache_retention_days if retention_days is None else retention_days
        )
        self._directory = self._prepare(
            Path(speech_cfg.cache_directory if directory is None else directory)
        )

        self._reap()

    def stream(self, text: str) -> Phrase:
        """
        A phrase, for the speaker to take whichever way it needs.

        Cheap and synchronous: it names the phrase rather than rendering it, and
        nothing happens until the speaker pulls on one of the two forms. That is
        load-bearing rather than incidental — a clip is queued behind whatever is
        already playing, and by the time it is drained an identical phrase ahead
        of it may already have filled the cache.
        """
        return Phrase(self, text)

    async def encoded(self, text: str) -> AsyncIterator[bytes]:
        """
        Opus packets for a phrase, from disk or synthesized.

        The path that costs nothing. What is stored is what Discord is sent, so
        a hit is a file read and a hand-over.

        There is no layer in front of this one. There was, holding the same
        packets in memory, and it saved a quarter of a millisecond on the way to
        playback — against a frame of twenty. What it cost was an eviction
        policy, a bound, and a setting to tune the bound with, none of which
        anybody could hear.
        """
        key = self._key(text)

        stored = await self._read(key)
        if stored is not None:
            await self._touch(key)
            for packet in stored:
                yield packet
            return

        async for packet in self._synthesize(key, text):
            yield packet

    async def samples(self, text: str) -> AsyncIterator[bytes]:
        """
        Playback PCM for a phrase, for a clip that cannot be sent as it is.

        Decoded from the same packets rather than kept a second way, because a
        second copy of every clip in the other form would give back the memory
        that storing them encoded just saved.

        Decoded as the packets arrive rather than once they all have, which is
        what keeps the first frame close behind the first packet. Collecting
        them first would be simpler and would mean a clip played quieter waited
        for its own last packet before its first could be played — a whole
        synthesis on a miss, and the whole decode even on a hit.

        Decoded a batch at a time, and in a thread. A packet costs microseconds
        and there are fifty a second, so decoding each one where it arrives put
        the whole clip's worth on the event loop as a single stall — measured at
        11.7 ms for three seconds of audio, against the 32 ms in which every
        speaker's next VAD frame is due. Batching keeps the number of thread
        hops down to a handful; the thread keeps the loop free for the channel
        the clip is about to play into.
        """
        decoder = opus.Decoder()
        batch: list[bytes] = []

        async for packet in self.encoded(text):
            batch.append(packet)

            if len(batch) >= DECODE_BATCH_PACKETS:
                yield await asyncio.to_thread(decoder.decode, batch)
                batch = []

        if batch:
            yield await asyncio.to_thread(decoder.decode, batch)

    async def warm(self, text: str) -> bool:
        """
        Render a phrase now so that nothing waits for it later.

        Reports whether it had to be synthesized, for a caller counting how much
        work warming turned out to be.

        A phrase already stored is left exactly as it was found, not touched, so
        warming does not make an old clip look recently wanted. Warmed is not the
        same as wanted — a warm-up renders whatever it can think of, in an order
        that means nothing — and a phrase nobody ever earns should age out on the
        usual terms.
        """
        # Nowhere to keep it makes this a synthesis nobody will ever be served.
        if self._directory is None:
            return False

        key = self._key(text)
        if self._stored(key):
            return False

        async for _ in self._synthesize(key, text):
            pass

        return True

    # ── synthesis ─────────────────────────────────

    async def _synthesize(self, key: str, text: str) -> AsyncIterator[bytes]:
        """
        Speak a phrase for the first time, keeping it on the way past.

        Resampled and encoded as it arrives rather than at the end, so the first
        packet reaches the player while the synthesizer is still working on the
        rest. Both are streaming for that reason, and both carry state between
        chunks, which is why each belongs to one clip.

        A clip is only stored once the synthesizer says it is whole. A failure
        partway through has already played whatever arrived, which is harmless;
        caching that fragment would make it permanent.
        """
        resampler: PlaybackResampler | None = None
        encoder = opus.Encoder()
        packets: list[bytes] = []

        try:
            async for speech in synthesize(text):
                if resampler is None:
                    resampler = PlaybackResampler(speech.rate)

                for packet in encoder.feed(resampler.feed(speech.pcm)):
                    packets.append(packet)
                    yield packet

            if resampler is not None:
                for packet in encoder.feed(resampler.flush()):
                    packets.append(packet)
                    yield packet

            for packet in encoder.flush():
                packets.append(packet)
                yield packet
        except SynthesisError as exc:
            logger.error("Could not synthesize %r: %s", text, exc)
            return

        stored = tuple(packets)
        await self._write(key, stored)

        logger.info(
            "Synthesized and cached %r (%.1fs, %d KiB).",
            text,
            opus.seconds(stored),
            sum(len(packet) for packet in stored) // BYTES_PER_KIB,
        )

    # ── disk ──────────────────────────────────────

    @staticmethod
    def _prepare(directory: Path) -> Path | None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Speech cache directory %s is unusable (%s), and it is the only place "
                "rendered speech is kept. Every phrase will be synthesized again every "
                "time it is said, and pre-warming will do nothing. Mount a writable "
                "volume there.",
                directory,
                exc,
            )
            return None

        return directory

    def _path(self, key: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{key}{CACHE_SUFFIX}"

    def _stored(self, key: str) -> bool:
        path = self._path(key)

        return path is not None and path.is_file()

    async def _touch(self, key: str) -> None:
        """
        Say that a stored clip is still in use.

        The reaper ages clips by mtime, and a hit in memory never opens the
        file, so without this the phrase the bot says most often is the one that
        looks least used: written once, read once after the restart that
        followed, and untouched for the ninety days after.

        A clip with no file behind it — never written, or written to a directory
        that turned out to be unusable — is not created here. `os.utime` is what
        does that rather than `touch`, which would leave an empty WAV for a
        later read to trip over.
        """
        path = self._path(key)
        if path is None:
            return

        try:
            await asyncio.to_thread(os.utime, path, None)
        except OSError as exc:
            logger.debug("Could not touch %s: %s", path, exc)

    def _reap(self) -> list[Path]:
        """
        Delete anything in the cache nothing has played in a long while.

        The cache is a directory that only grows: a display name goes into the
        key, so every person who has ever been announced leaves a file behind
        and none of them are ever asked for again once they leave the server.

        Everything in the directory is fair game, because everything in it is
        this cache's. There is no hand-placed clip to spare any more — those
        live in `audio.chimes` — so a file here is either a clip this process
        wrote, one an earlier version wrote in a format nothing can read now, or
        a `.partial` orphaned by a process killed mid-write. All three want the
        same thing, which is to be gone.

        Subdirectories are left alone. The scan does not descend into them and
        does not remove them, so a directory an operator makes here is a
        directory they still have.

        Age is the mtime, where a transcript is aged by its filename: what
        matters here is when a clip was last wanted rather than when it was
        first rendered, and `_touch` keeps that current.
        """
        if self._directory is None or self._retention_days < MINIMUM_RETENTION_DAYS:
            return []

        cutoff = datetime.now() - timedelta(days=self._retention_days)
        reaped: list[Path] = []

        for path in self._directory.iterdir():
            if not path.is_file():
                continue

            try:
                if datetime.fromtimestamp(path.stat().st_mtime) >= cutoff:
                    continue
                path.unlink()
            except OSError as exc:
                logger.error("Could not reap %s: %s", path, exc)
                continue

            reaped.append(path)

        if reaped:
            logger.info(
                "Reaped %d cached clips nothing has played in %d days.",
                len(reaped),
                self._retention_days,
            )

        return reaped

    async def _read(self, key: str) -> tuple[bytes, ...] | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None

        try:
            packets = await asyncio.to_thread(opus.read, path)
        except (OSError, OggError, ValueError, struct.error) as exc:
            logger.error("Ignoring unreadable cached clip %s: %s", path, exc)
            return None

        return tuple(packets) or None

    async def _write(self, key: str, packets: Sequence[bytes]) -> None:
        path = self._path(key)
        if path is None:
            return

        try:
            await asyncio.to_thread(self._write_ogg, path, packets)
        except OSError as exc:
            logger.error("Could not cache a clip at %s: %s", path, exc)

    @staticmethod
    def _write_ogg(path: Path, packets: Sequence[bytes]) -> None:
        """
        Write a clip whole or not at all.

        A reader can arrive at any time, including the next process after this
        one is killed mid-write, and a truncated container would be cached
        forever.
        """
        partial = path.with_suffix(PARTIAL_SUFFIX)
        opus.write(partial, packets)
        partial.replace(path)

    # ── keys ──────────────────────────────────────

    @staticmethod
    def _key(text: str) -> str:
        """
        A filename for a phrase.

        The voice is part of the key because changing `TTS_VOICE` should not
        serve back clips in the old one, and a hash because the phrase names a
        speaker and speakers name themselves.
        """
        return hashlib.sha256(
            f"{tts_cfg.voice}{KEY_SEPARATOR}{text}".encode()
        ).hexdigest()


_shared: SpeechCache | None = None


def shared_cache() -> SpeechCache:
    """
    The one cache in the process.

    Tools are built per server, but a clip rendered for one server is the same
    audio for another, and synthesis is the expensive part. Built on first use
    rather than at import so nothing touches the filesystem for a tool nobody
    enabled.
    """
    global _shared

    if _shared is None:
        _shared = SpeechCache()

    return _shared
