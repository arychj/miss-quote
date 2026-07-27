"""
Daily-rollover JSONL transcript writer.

One file per local calendar day, one JSON object per utterance, appended and
flushed as produced. The current date is re-derived per utterance rather than
cached, so a long-running pod rolls over at midnight without a restart.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import transcript_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


class TranscriptWriter:
    """Appends utterances to `<directory>/<YYYY-MM-DD>.jsonl`, pruning by age."""

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

    def write(self, user_id: int, user: str, channel: str, text: str) -> Path:
        """Append one utterance and return the file it landed in."""
        now = datetime.now(self._zone)
        self._roll_over(now.date())

        line = json.dumps(
            {
                "ts": now.isoformat(),
                "user_id": user_id,
                "user": user,
                "channel": channel,
                "text": text,
            },
            ensure_ascii=False,
        )

        path = self._path_for(now.date())
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

        for path in self._directory.glob(f"*{transcript_cfg.filename_suffix}"):
            file_date = self._date_from_filename(path)
            if file_date is None or file_date >= cutoff:
                continue

            try:
                path.unlink()
            except OSError as exc:
                logger.error("Could not prune %s: %s", path, exc)
                continue

            removed.append(path)
            logger.info("Pruned transcript %s (older than %d days).", path.name, self._retention_days)

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

    def _path_for(self, day: date) -> Path:
        name = day.strftime(transcript_cfg.filename_date_format)
        return self._directory / f"{name}{transcript_cfg.filename_suffix}"

    @staticmethod
    def _date_from_filename(path: Path) -> date | None:
        try:
            return datetime.strptime(
                path.stem, transcript_cfg.filename_date_format
            ).date()
        except ValueError:
            return None
