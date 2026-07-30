"""
Playing a tool's audio back into the voice channel it came from.

Discord's player is a thread. It asks an `AudioSource` for exactly one frame
every 20 ms and stops the moment it gets anything short of one, so a clip that
is still being synthesized cannot simply be handed over as a file. The sources
here are the buffer between the two: filled from the event loop as chunks
arrive, drained by the player thread a frame at a time, so playback starts on
the first chunk rather than waiting for the last.

There are two of them because there are two forms a clip can arrive in.
`OpusStream` carries packets the cache already holds encoded, and says so
through `is_opus`, which is discord.py's signal to send them untouched — no
encoder is even constructed. `PCMStream` carries samples, which is what anything
that has to be *changed* on the way out needs: a gain is a multiplication, and
there is nothing to multiply in an encoded packet.

Which one a clip gets is decided in `play` and turns on the volume alone. At
full volume there is nothing to do to the audio, so it goes out as it was
stored; below it, the clip is decoded, scaled, and re-encoded by discord.py as
it always was.

No ffmpeg is involved in either. The audio is already what Discord wants, and
libopus is present for receiving regardless.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

import discord

from miss_quote.audio.gain import scaled
from miss_quote.config import UNITY_VOLUME, audio_cfg, tts_cfg
from miss_quote.transcript.writer import Source
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

SILENCE = b"\x00"
NOTHING_LEFT = b""


@runtime_checkable
class Encodable(Protocol):
    """
    A clip that can be had either way, so the speaker can pick.

    Declared here rather than beside the cache that satisfies it, because it
    describes what this module is willing to accept rather than what any one
    thing produces. A plain iterator of PCM is still accepted and is what a tool
    assembling a clip of its own hands over.
    """

    def packets(self) -> AsyncIterator[bytes]:
        """The clip already encoded, one Opus packet per frame."""

    def pcm(self) -> AsyncIterator[bytes]:
        """The clip as playback samples."""


class _FedSource(discord.AudioSource):
    """
    What the two sources share: a buffer filled on one thread and drained on
    another.

    `read` is called on the player thread and blocks it when the buffer is short
    of a frame, which is the point: returning early would be read as the end of
    the clip. The block is bounded, so a synthesizer that stalls costs the tail
    of one announcement rather than a thread and a voice connection.
    """

    def __init__(self, stall_seconds: float) -> None:
        self._stall_seconds = stall_seconds
        self._lock = threading.Lock()
        self._fed = threading.Event()
        self._complete = False

    def finish(self) -> None:
        """Say that no more audio is coming, so the player can drain and stop."""
        with self._lock:
            self._complete = True
        self._fed.set()

    def read(self) -> bytes:
        while True:
            with self._lock:
                frame = self._take()
                if frame is not None:
                    return frame

                if self._complete:
                    return self._drained()

                # Cleared under the lock and waited on outside it, so a feed
                # landing in between sets the event rather than being missed.
                self._fed.clear()

            if not self._fed.wait(self._stall_seconds):
                logger.warning(
                    "No audio for %.0fs; ending the clip early.", self._stall_seconds
                )
                return NOTHING_LEFT

    def _take(self) -> bytes | None:
        """One frame if there is a whole one, else None. Called holding the lock."""
        raise NotImplementedError

    def _drained(self) -> bytes:
        """What to send once nothing more is coming. Called holding the lock."""
        return NOTHING_LEFT


class OpusStream(_FedSource):
    """
    Packets the cache already holds encoded, handed to the player as they are.

    `is_opus` is what makes this worth having: discord.py builds no encoder for
    a source that says yes, so a cached phrase costs nothing to play beyond the
    send itself. One packet is one frame, which is what the encoder guarantees
    and what the player assumes.

    Nothing here pads or splits. A packet is atomic — half of one is not audio —
    and the encoder has already padded the only frame that could have been
    short.
    """

    def __init__(self, stall_seconds: float) -> None:
        super().__init__(stall_seconds)
        self._packets: deque[bytes] = deque()

    def is_opus(self) -> bool:
        """The clip is already encoded; discord.py sends it untouched."""
        return True

    def feed(self, packet: bytes) -> None:
        with self._lock:
            self._packets.append(packet)
        self._fed.set()

    def _take(self) -> bytes | None:
        return self._packets.popleft() if self._packets else None


class PCMStream(_FedSource):
    """
    Samples, for a clip that has to be changed on the way out.

    Volume is applied as audio is fed rather than as it is read, so the buffer
    holds what will be played and framing stays framing.
    """

    def __init__(self, stall_seconds: float, volume: float = UNITY_VOLUME) -> None:
        super().__init__(stall_seconds)
        self._volume = volume
        self._buffer = bytearray()

    def is_opus(self) -> bool:
        """The clip is PCM; discord.py encodes it."""
        return False

    def feed(self, pcm: bytes) -> None:
        quietened = scaled(pcm, self._volume)

        with self._lock:
            self._buffer.extend(quietened)
        self._fed.set()

    def _take(self) -> bytes | None:
        frame_bytes = audio_cfg.playback_frame_bytes
        if len(self._buffer) < frame_bytes:
            return None

        frame = bytes(self._buffer[:frame_bytes])
        del self._buffer[:frame_bytes]

        return frame

    def _drained(self) -> bytes:
        """
        Whatever is left, padded out to a whole frame.

        A clip rarely ends on a frame boundary, and the player treats a short
        read as the end. Padding with silence keeps the last few milliseconds —
        usually the end of a word — rather than dropping them.
        """
        if not self._buffer:
            return NOTHING_LEFT

        frame = bytes(self._buffer).ljust(audio_cfg.playback_frame_bytes, SILENCE)
        self._buffer.clear()

        return frame


class DiscordSpeaker:
    """
    Plays a tool's audio in the voice channel the utterance came from.

    One clip at a time per server. A bot holds one voice connection per guild
    and `play` refuses to start over itself, so simultaneous announcements queue
    rather than collide — two people swearing at once are fined one after the
    other.
    """

    def __init__(self, guilds: Callable[[int], Any | None]) -> None:
        # Resolved through a callable because the speaker is built before the
        # bot it plays through exists.
        self._guilds = guilds
        self._locks: dict[int, asyncio.Lock] = {}

    async def play(
        self,
        source: Source,
        audio: Encodable | AsyncIterator[bytes],
        scale: float = UNITY_VOLUME,
    ) -> None:
        """
        Play one clip, encoded if nothing has to be done to it first.

        The gain is the deployment's loudness times whatever the caller asked
        for, and it is the whole of the decision: at unity there is nothing to
        do to the audio, so a clip that can be had already encoded is sent as it
        was stored. Anything quieter has to be multiplied, and multiplying means
        samples.
        """
        async with self._lock_for(source.guild_id):
            voice_client = self._voice_client_for(source)
            if voice_client is None:
                return

            gain = audio_cfg.playback_volume * scale

            if isinstance(audio, Encodable):
                if gain == UNITY_VOLUME:
                    await self._play_encoded(voice_client, audio.packets())
                    return

                audio = audio.pcm()

            await self._play(voice_client, audio, gain)

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock

        return lock

    def _voice_client_for(self, source: Source) -> discord.VoiceClient | None:
        """
        The connection to play through, if the bot is still where it was.

        A clip is queued behind whatever is already playing and synthesized
        before that, so by the time it is ready the bot may have moved or left.
        Playing it into wherever the bot ended up would be worse than silence.
        """
        guild = self._guilds(source.guild_id)
        voice_client = getattr(guild, "voice_client", None)

        if voice_client is None or not voice_client.is_connected():
            logger.debug("Not connected to %s; dropping a clip.", source.guild_alias)
            return None

        if getattr(voice_client.channel, "id", None) != source.channel_id:
            logger.debug(
                "No longer in '%s'; dropping a clip.", source.channel
            )
            return None

        if voice_client.is_playing():
            logger.warning(
                "Already playing in '%s'; dropping a clip.", source.channel
            )
            return None

        return voice_client

    @classmethod
    async def _play(
        cls,
        voice_client: discord.VoiceClient,
        audio: AsyncIterator[bytes],
        gain: float = UNITY_VOLUME,
    ) -> None:
        """
        Feed one clip to the player as samples, at the gain `play` settled on.

        The deployment's loudness and the caller's scale are multiplied before
        they arrive here, so `PLAYBACK_VOLUME` remains the only thing that says
        how loud a channel wants to be interrupted and a tool only says how much
        quieter than that this particular clip should be.
        """
        await cls._drive(voice_client, PCMStream(tts_cfg.stall_seconds, gain), audio)

    @classmethod
    async def _play_encoded(
        cls, voice_client: discord.VoiceClient, packets: AsyncIterator[bytes]
    ) -> None:
        """Feed one clip to the player already encoded, which it sends untouched."""
        await cls._drive(voice_client, OpusStream(tts_cfg.stall_seconds), packets)

    @staticmethod
    async def _drive(
        voice_client: discord.VoiceClient,
        stream: OpusStream | PCMStream,
        audio: AsyncIterator[bytes],
    ) -> None:
        """Arm the player, fill the source until the clip runs out, and wait it out."""
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_finished(error: Exception | None) -> None:
            # Called on the player thread once the source runs dry.
            if error is not None:
                logger.error("Playback failed: %s", error, exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        voice_client.play(stream, after=on_finished)

        try:
            async for chunk in audio:
                stream.feed(chunk)
        finally:
            # Both unconditional. Without the first, a failed synthesis leaves
            # the player thread waiting on audio that is never coming; without
            # the second, the caller releases its turn while the player is still
            # draining, and the clip queued behind it is dropped for arriving
            # over one already playing.
            stream.finish()
            await finished.wait()
