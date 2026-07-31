"""
Where an account of a session is kept, and how the last one is found.

The tree is the transcripts' tree with a different root: the same guild and
channel directories, from the same `Source.relative_directory`, and a file named
for the transcript it describes rather than for the moment it was written. A
summary and its conversation are therefore found from each other by changing one
path segment, and a session that took an ordinal to avoid a collision keeps it
here — two sessions that could not share a transcript name cannot share a
summary name either.

`latest` is what answers "what happened last session", and it reads the
**filename** rather than the mtime. The name is the moment the session opened;
the mtime is the moment the summary happened to be written, which is a different
thing the instant anything is ever regenerated or restored from a backup.
Retention ages files the same way and for the same reason, which is the rule
`TranscriptWriter.prune` already follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from miss_quote.config import summary_cfg, transcript_cfg
from miss_quote.transcript.writer import Source, Transcript
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

FILE_ENCODING = "utf-8"

# Written to and renamed over, so a process killed mid-write leaves something
# nothing will ever read as a summary rather than half of one.
PARTIAL_SUFFIX = ".partial"


@dataclass(frozen=True)
class Summary:
    """One stored account, and the session it describes."""

    path: Path
    text: str

    @property
    def session(self) -> str:
        """The session's name, which is when it opened."""
        return self.path.stem


class SummaryStore:
    """Files summaries beside the transcripts they came from, and finds them."""

    def __init__(
        self, directory: Path | None = None, retention_days: int | None = None
    ) -> None:
        self._directory = Path(directory or summary_cfg.directory)
        self._retention_days = (
            summary_cfg.retention_days if retention_days is None else retention_days
        )

    @property
    def retention_enabled(self) -> bool:
        return self._retention_days >= 1

    def path_for(self, transcript: Transcript) -> Path:
        """
        Where one transcript's summary belongs.

        Named from the transcript's own stem rather than from the clock, so the
        pairing survives a summary written days late — by a backfill, or by a
        deployment that was pointed at a working endpoint after the fact.
        """
        return (
            self._directory
            / transcript.source.relative_directory
            / f"{transcript.path.stem}{summary_cfg.filename_suffix}"
        )

    def write(self, transcript: Transcript, text: str) -> Path | None:
        """
        Store one summary, reporting where it went.

        Through a temporary file and a rename, so a process killed partway
        through leaves no half-written summary to be read back as a whole one.
        A directory that cannot be written to costs the summary and is reported;
        the transcript it came from is untouched and can be summarized again.
        """
        path = self._path_prepared(transcript)
        if path is None:
            return None

        partial = path.parent / (path.name + PARTIAL_SUFFIX)

        try:
            partial.write_text(text, encoding=FILE_ENCODING)
            partial.replace(path)
        except OSError as exc:
            logger.error("Could not write the summary at %s: %s", path, exc)
            partial.unlink(missing_ok=True)
            return None

        return path

    def latest(self, source: Source) -> Summary | None:
        """
        The most recent summary for one channel, if it has any.

        By filename, which is when the session opened. A session still in
        progress has no summary yet — it is written when the transcript seals —
        so this is the previous conversation even when it is asked for in the
        middle of one, which is exactly what "last session" means.
        """
        directory = self._directory / source.relative_directory
        if not directory.is_dir():
            return None

        stored = sorted(directory.glob(f"*{summary_cfg.filename_suffix}"))
        if not stored:
            return None

        path = stored[-1]

        try:
            return Summary(path=path, text=path.read_text(encoding=FILE_ENCODING))
        except OSError as exc:
            logger.error("Could not read the summary at %s: %s", path, exc)
            return None

    def prune(self) -> list[Path]:
        """
        Delete summaries older than the retention window.

        Aged by the date at the front of the filename, on the same reasoning as
        the transcripts: the name says when the session was, and the mtime says
        when a file was last touched, which is not the same question.
        """
        if not self.retention_enabled or not self._directory.is_dir():
            return []

        # The same clock the session was named by, so a summary is not kept or
        # dropped a day early for having been taken in a different timezone from
        # the one the process is reading it in.
        today = datetime.now(ZoneInfo(transcript_cfg.timezone)).date()
        cutoff = today - timedelta(days=self._retention_days)
        removed: list[Path] = []

        for path in self._directory.rglob(f"*{summary_cfg.filename_suffix}"):
            taken = _date_from_filename(path)
            if taken is None or taken >= cutoff:
                continue

            try:
                path.unlink()
            except OSError as exc:
                logger.error("Could not prune %s: %s", path, exc)
                continue

            removed.append(path)
            logger.info(
                "Pruned summary %s (older than %d days).",
                path.relative_to(self._directory),
                self._retention_days,
            )

        return removed

    def _path_prepared(self, transcript: Transcript) -> Path | None:
        """Where a summary goes, with somewhere to put it."""
        path = self.path_for(transcript)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Could not make %s to summarize into: %s", path.parent, exc)
            return None

        return path


def _date_from_filename(path: Path) -> date | None:
    """
    The day a session was taken, from the front of its name.

    The same prefix the transcript carries, read the same way, so an ordinal on
    the end of a name does not exempt that summary from retention.
    """
    try:
        return datetime.strptime(
            path.stem[: transcript_cfg.filename_date_length],
            transcript_cfg.filename_date_format,
        ).date()
    except ValueError:
        return None
