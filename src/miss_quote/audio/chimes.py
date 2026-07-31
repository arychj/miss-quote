"""
Clips nobody synthesized — a flourish a tool plays ahead of what it has to say.

Audio the synthesizer had no part in, kept in its own directory because it is
the operator's rather than the process's. Nothing here writes, and nothing here
deletes: there are a handful of these, each was put there deliberately, and none
of them should ever be dropped to make room for a phrase somebody said once.
That is the whole reason they are not in the speech cache, where every file is a
digest on a retention clock.

WAV only, and deliberately. These are chained into a clip that is being scaled
anyway, so there is nothing to be gained from storing them the way Discord takes
them — and nothing in the image can decode anything else, which is the point of
a playback path with no ffmpeg in it.

Names arrive from configuration and are resolved against the directory rather
than taken at their word, so a setting cannot be pointed at an arbitrary file on
the host and have the bot read it out.

A clip is read once and held for the life of the process. One that is missing or
will not parse costs the flourish and not the announcement behind it.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from miss_quote.audio.resampler import PlaybackResampler, to_mono
from miss_quote.config import audio_cfg, speech_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

WAVE_READ = "rb"
BITS_PER_BYTE = 8
NOTHING = b""


class ChimeLibrary:
    """
    The clips kept by hand, read on first ask and held thereafter.

    One instance serves the whole process. A chime is the same audio wherever it
    is played, and the directory it comes from is a deployment's rather than a
    server's.

    The directory is not created and never has to exist. Nothing writes here, so
    an absent one is a chime that is missing — reported by whoever asked for it —
    rather than a degradation the process has to announce on the way up.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = Path(
            speech_cfg.chime_directory if directory is None else directory
        )
        self._clips: dict[str, bytes] = {}

    def path(self, name: str) -> Path | None:
        """Where a named clip lives, if it lives inside the chime directory."""
        root = self._directory.resolve()
        path = (root / name).resolve()

        if not path.is_relative_to(root):
            logger.error("Clip '%s' resolves outside %s; ignoring it.", name, root)
            return None

        return path

    async def clip(self, name: str) -> bytes:
        """
        Playback-ready PCM for a named WAV.

        A clip that is missing or will not parse returns nothing playable rather
        than raising. It is the opening flourish; whatever it was going to
        introduce is the part that matters.
        """
        remembered = self._clips.get(name)
        if remembered is not None:
            return remembered

        path = self.path(name)
        if path is None or not path.is_file():
            logger.error("No clip at '%s'; carrying on without it.", path or name)
            return NOTHING

        try:
            rate, pcm = await asyncio.to_thread(self._read, path)
        except (OSError, wave.Error) as exc:
            logger.error("Ignoring unplayable clip %s: %s", path, exc)
            return NOTHING

        playback = self._to_playback(rate, pcm)
        self._clips[name] = playback
        return playback

    @staticmethod
    def _to_playback(rate: int, pcm: bytes) -> bytes:
        """
        One clip at the rate and width the player takes.

        Rendered speech is stored as Discord takes it and never needs
        converting, but a WAV somebody dropped in the directory is whatever they
        authored it as.
        """
        resampler = PlaybackResampler(rate)

        return resampler.feed(pcm) + resampler.flush()

    @staticmethod
    def _read(path: Path) -> tuple[int, bytes]:
        """
        One WAV off disk as mono, whatever layout it was authored in.

        Sample rate and channel count are the file's own business — soxr covers
        the first and a downmix the second — but sample width is not. Anything
        other than int16 is a different format rather than a different
        arrangement of this one, and is refused with a line saying so instead
        of played as noise.
        """
        with wave.open(str(path), WAVE_READ) as handle:
            width = handle.getsampwidth()
            if width != audio_cfg.sample_width:
                raise wave.Error(
                    f"{width * BITS_PER_BYTE}-bit audio, but only "
                    f"{audio_cfg.sample_width * BITS_PER_BYTE}-bit can be played"
                )

            frames = handle.readframes(handle.getnframes())
            return handle.getframerate(), to_mono(frames, handle.getnchannels())


_shared: ChimeLibrary | None = None


def shared_chimes() -> ChimeLibrary:
    """
    The one library in the process.

    Tools are built per server, but a clip read for one is the same samples for
    another, and holding them once is the whole point. Built on first use rather
    than at import so nothing touches the filesystem for a tool nobody enabled.
    """
    global _shared

    if _shared is None:
        _shared = ChimeLibrary()

    return _shared
