"""
Rendered speech, kept so a phrase is only ever synthesized once.

Two layers, each holding the form that suits it:

  memory — playback-ready 48 kHz stereo PCM, so a hit costs a dictionary lookup
  disk   — the synthesizer's own mono WAV, a quarter the size, and playable, so
           you can hear what the bot actually said

The first hit after a restart therefore pays one resample and nothing else. The
disk layer is optional: an unwritable or absent directory costs the persistence,
not the feature.
"""

from __future__ import annotations

import asyncio
import hashlib
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from audio.resampler import PlaybackResampler
from config import audio_cfg, tts_cfg
from tts.client import SynthesisError, synthesize
from utils.logging import get_logger

logger = get_logger(__name__)

CACHE_SUFFIX = ".wav"
PARTIAL_SUFFIX = ".partial"
KEY_SEPARATOR = "\n"
MONO_CHANNELS = 1

WAVE_READ = "rb"
WAVE_WRITE = "wb"


class SpeechCache:
    """
    Speech for a phrase, synthesized on first ask and kept thereafter.

    One instance serves the whole process. Nothing here is per server: the same
    words in the same voice are the same audio wherever they were asked for.
    """

    def __init__(self, directory: Path | None = None, entries: int | None = None) -> None:
        self._entries = tts_cfg.cache_entries if entries is None else entries
        self._memory: dict[str, bytes] = {}
        self._directory = self._prepare(
            Path(tts_cfg.cache_directory if directory is None else directory)
        )

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """
        Playback-ready PCM for a phrase, from memory, from disk, or synthesized.

        An async generator, so none of this runs until the first chunk is pulled.
        That is load-bearing rather than incidental: callers hand the stream to
        something that plays one clip at a time, and by the time a queued stream
        is drained an identical phrase ahead of it may already have filled the
        cache.
        """
        key = self._key(text)

        remembered = self._memory.get(key)
        if remembered is not None:
            yield remembered
            return

        stored = await self._read(key)
        if stored is not None:
            playback = self._to_playback(*stored)
            self._remember(key, playback)
            yield playback
            return

        async for chunk in self._synthesize(key, text):
            yield chunk

    # ── synthesis ─────────────────────────────────

    async def _synthesize(self, key: str, text: str) -> AsyncIterator[bytes]:
        """
        Speak a phrase for the first time, keeping it on the way past.

        A clip is only stored once the synthesizer says it is whole. A failure
        partway through has already played whatever arrived, which is harmless;
        caching that fragment would make it permanent.
        """
        resampler: PlaybackResampler | None = None
        rate = audio_cfg.playback_sample_rate
        source = bytearray()
        playback = bytearray()

        try:
            async for speech in synthesize(text):
                if resampler is None:
                    rate = speech.rate
                    resampler = PlaybackResampler(rate)

                source.extend(speech.pcm)
                converted = resampler.feed(speech.pcm)
                playback.extend(converted)
                if converted:
                    yield converted

            if resampler is not None:
                tail = resampler.flush()
                playback.extend(tail)
                if tail:
                    yield tail
        except SynthesisError as exc:
            logger.error("Could not synthesize %r: %s", text, exc)
            return

        self._remember(key, bytes(playback))
        await self._write(key, rate, bytes(source))
        logger.info("Synthesized and cached %r (%.1fs).", text, self._seconds(rate, source))

    @staticmethod
    def _seconds(rate: int, source: bytes | bytearray) -> float:
        return len(source) / (rate * audio_cfg.sample_width)

    @staticmethod
    def _to_playback(rate: int, pcm: bytes) -> bytes:
        resampler = PlaybackResampler(rate)
        return resampler.feed(pcm) + resampler.flush()

    # ── memory ────────────────────────────────────

    def _remember(self, key: str, playback: bytes) -> None:
        """
        Hold a clip, retiring the oldest once the cache is full.

        Insertion order is eviction order. There is no recency to exploit here:
        the entries are one phrase per speaker, and a speaker who has gone quiet
        is not coming back sooner than one who has not.
        """
        if not tts_cfg.caching_enabled:
            return

        while len(self._memory) >= self._entries:
            self._memory.pop(next(iter(self._memory)))

        self._memory[key] = playback

    # ── disk ──────────────────────────────────────

    @staticmethod
    def _prepare(directory: Path) -> Path | None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Speech cache directory %s is unusable (%s); "
                "clips will be kept in memory only and re-synthesized after a restart.",
                directory,
                exc,
            )
            return None

        return directory

    def _path(self, key: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{key}{CACHE_SUFFIX}"

    async def _read(self, key: str) -> tuple[int, bytes] | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None

        try:
            return await asyncio.to_thread(self._read_wave, path)
        except (OSError, wave.Error) as exc:
            logger.error("Ignoring unreadable cached clip %s: %s", path, exc)
            return None

    @staticmethod
    def _read_wave(path: Path) -> tuple[int, bytes]:
        with wave.open(str(path), WAVE_READ) as handle:
            return handle.getframerate(), handle.readframes(handle.getnframes())

    async def _write(self, key: str, rate: int, pcm: bytes) -> None:
        path = self._path(key)
        if path is None:
            return

        try:
            await asyncio.to_thread(self._write_wave, path, rate, pcm)
        except (OSError, wave.Error) as exc:
            logger.error("Could not cache a clip at %s: %s", path, exc)

    @staticmethod
    def _write_wave(path: Path, rate: int, pcm: bytes) -> None:
        """
        Write a clip whole or not at all.

        A reader can arrive at any time, including the next process after this
        one is killed mid-write, and a truncated WAV would be cached forever.
        """
        partial = path.with_suffix(PARTIAL_SUFFIX)

        with wave.open(str(partial), WAVE_WRITE) as handle:
            handle.setnchannels(MONO_CHANNELS)
            handle.setsampwidth(audio_cfg.sample_width)
            handle.setframerate(rate)
            handle.writeframes(pcm)

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
