"""
Playing a tool's audio back into the voice channel it came from.

Discord's player is a thread. It asks an `AudioSource` for exactly one frame
every 20 ms and stops the moment it gets anything short of one, so a clip that
is still being synthesized cannot simply be handed over as a file. `PCMStream`
is the buffer between the two: filled from the event loop as chunks arrive,
drained by the player thread a frame at a time, so playback starts on the first
chunk rather than waiting for the last.

No ffmpeg is involved. The audio is already the 48 kHz stereo PCM Discord wants,
and the Opus encoder that libopus provides for receiving encodes it on the way
back out.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import discord

from miss_quote.audio.gain import scaled
from miss_quote.config import UNITY_VOLUME, audio_cfg, tts_cfg
from miss_quote.transcript.writer import Source
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

SILENCE = b"\x00"
NOTHING_LEFT = b""


class PCMStream(discord.AudioSource):
    """
    An audio source fed from the event loop while the player drains it.

    `read` is called on the player thread and blocks it when the buffer is short
    of a frame, which is the point: returning early would be read as the end of
    the clip. The block is bounded, so a synthesizer that stalls costs the tail
    of one announcement rather than a thread and a voice connection.

    Volume is applied as audio is fed rather than as it is read, so the buffer
    holds what will be played and framing stays framing.
    """

    def __init__(self, stall_seconds: float, volume: float = UNITY_VOLUME) -> None:
        self._stall_seconds = stall_seconds
        self._volume = volume
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._fed = threading.Event()
        self._complete = False

    def is_opus(self) -> bool:
        """The clip is PCM; discord.py encodes it."""
        return False

    def feed(self, pcm: bytes) -> None:
        quietened = scaled(pcm, self._volume)

        with self._lock:
            self._buffer.extend(quietened)
        self._fed.set()

    def finish(self) -> None:
        """Say that no more audio is coming, so the player can drain and stop."""
        with self._lock:
            self._complete = True
        self._fed.set()

    def read(self) -> bytes:
        frame_bytes = audio_cfg.playback_frame_bytes

        while True:
            with self._lock:
                if len(self._buffer) >= frame_bytes:
                    frame = bytes(self._buffer[:frame_bytes])
                    del self._buffer[:frame_bytes]
                    return frame

                if self._complete:
                    return self._final_frame(frame_bytes)

                # Cleared under the lock and waited on outside it, so a feed
                # landing in between sets the event rather than being missed.
                self._fed.clear()

            if not self._fed.wait(self._stall_seconds):
                logger.warning(
                    "No audio for %.0fs; ending the clip early.", self._stall_seconds
                )
                return NOTHING_LEFT

    def _final_frame(self, frame_bytes: int) -> bytes:
        """
        Whatever is left, padded out to a whole frame.

        A clip rarely ends on a frame boundary, and the player treats a short
        read as the end. Padding with silence keeps the last few milliseconds —
        usually the end of a word — rather than dropping them.
        """
        if not self._buffer:
            return NOTHING_LEFT

        frame = bytes(self._buffer).ljust(frame_bytes, SILENCE)
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
        self, source: Source, audio: AsyncIterator[bytes], scale: float = UNITY_VOLUME
    ) -> None:
        async with self._lock_for(source.guild_id):
            voice_client = self._voice_client_for(source)
            if voice_client is None:
                return

            await self._play(voice_client, audio, scale)

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

    @staticmethod
    async def _play(
        voice_client: discord.VoiceClient,
        audio: AsyncIterator[bytes],
        scale: float = UNITY_VOLUME,
    ) -> None:
        """
        Feed one clip to the player, at the deployment's loudness times `scale`.

        The two are multiplied here rather than anywhere a tool can see, so
        `PLAYBACK_VOLUME` remains the only thing that says how loud a channel
        wants to be interrupted and a tool only says how much quieter than that
        this particular clip should be.
        """
        stream = PCMStream(tts_cfg.stall_seconds, audio_cfg.playback_volume * scale)
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
