"""
Daily-rollover JSONL transcript writer.

One file per local calendar day per voice channel, one JSON object per
utterance, appended and flushed as produced. The current date is re-derived per
utterance rather than cached, so a long-running pod rolls over at midnight
without a restart.

Files are filed under `<guild>/<channel>/`, so the path carries the origin of
every utterance and the lines themselves do not have to repeat it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import transcript_cfg
from utils.logging import get_logger

logger = get_logger(__name__)

SLUG_DISALLOWED = re.compile(r"[^a-z0-9_-]+")
SLUG_EDGE_CHARACTERS = "-"
SLUG_FALLBACK = "unnamed"


def slugify(name: str) -> str:
    """
    Reduce a Discord display name to something safe to use as a path segment.

    Dots and separators are dropped rather than escaped, so a name like
    `../../etc` cannot express a traversal no matter where in the string it
    appears. Runs of disallowed characters collapse to a single dash so a name
    cannot expand into a long run of separators.
    """
    slug = SLUG_DISALLOWED.sub("-", name.casefold()).strip(SLUG_EDGE_CHARACTERS)
    return slug or SLUG_FALLBACK


@dataclass(frozen=True)
class Source:
    """The guild and channel an utterance came from."""

    guild_id: int
    guild: str
    channel_id: int
    channel: str

    @property
    def relative_directory(self) -> Path:
        """
        Directory this source's transcripts live in, relative to the root.

        The ID leads so a renamed guild or channel stays greppable by identity;
        the slug follows so the tree is readable without looking anything up.
        """
        return Path(
            f"{self.guild_id}-{slugify(self.guild)}",
            f"{self.channel_id}-{slugify(self.channel)}",
        )


class TranscriptWriter:
    """Appends to `<root>/<guild>/<channel>/<YYYY-MM-DD>.jsonl`, pruning by age."""

    def __init__(
        self,
        directory: Path | None = None,
        timezone: str | None = None,
        retention_days: int | None = None,
    ) -> None:
        self._directory = Path(directory or transcript_cfg.directory)
        self._zone = ZoneInfo(timezone or transcript_cfg.timezone)
        self._retention_days = (
            transcript_cfg.retention_days if retention_days is None else retention_days
        )
        self._current_date: date | None = None

        self._directory.mkdir(parents=True, exist_ok=True)
        self.prune()

    @property
    def retention_enabled(self) -> bool:
        return self._retention_days >= 1

    def write(self, source: Source, user_id: int, user: str, text: str) -> Path:
        """Append one utterance and return the file it landed in."""
        now = datetime.now(self._zone)
        self._roll_over(now.date())

        line = json.dumps(
            {
                "ts": now.isoformat(),
                "user_id": user_id,
                "user": user,
                "text": text,
            },
            ensure_ascii=False,
        )

        path = self._path_for(source, now.date())
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

        return path

    def prune(self) -> list[Path]:
        """
        Delete transcripts older than the retention window.

        Age comes from the filename date, not mtime: the filename is the
        authoritative record of the day a transcript covers, while mtime
        misjudges a file appended to late or restored from backup.
        """
        if not self.retention_enabled:
            return []

        cutoff = datetime.now(self._zone).date() - timedelta(days=self._retention_days)
        removed: list[Path] = []

        for path in self._directory.rglob(f"*{transcript_cfg.filename_suffix}"):
            file_date = self._date_from_filename(path)
            if file_date is None or file_date >= cutoff:
                continue

            try:
                path.unlink()
            except OSError as exc:
                logger.error("Could not prune %s: %s", path, exc)
                continue

            removed.append(path)
            logger.info(
                "Pruned transcript %s (older than %d days).",
                path.relative_to(self._directory),
                self._retention_days,
            )

        return removed

    def _roll_over(self, today: date) -> None:
        if today == self._current_date:
            return

        previous = self._current_date
        self._current_date = today

        if previous is None:
            return

        logger.info("Transcript rolled over from %s to %s.", previous, today)
        self.prune()

    def _path_for(self, source: Source, day: date) -> Path:
        name = day.strftime(transcript_cfg.filename_date_format)
        return (
            self._directory
            / source.relative_directory
            / f"{name}{transcript_cfg.filename_suffix}"
        )

    @staticmethod
    def _date_from_filename(path: Path) -> date | None:
        try:
            return datetime.strptime(
                path.stem, transcript_cfg.filename_date_format
            ).date()
        except ValueError:
            return None
