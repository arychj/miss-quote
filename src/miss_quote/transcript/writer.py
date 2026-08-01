"""
Per-session JSONL transcript writer.

One file per connection to a voice channel, one JSON object per utterance,
appended and flushed as produced. The file is named for the moment the bot
joined and keeps that name until it leaves, so a session spanning midnight stays
in one file and a reconnect starts a new one.

Files are filed under `<guild>/<channel>/`, so the path carries the origin of
every utterance and the lines themselves do not have to repeat it.

Whether a session is written down at all is qualified by `transcript.schedule`,
which is how a deployment says when an evening may start being recorded. The
question is asked once, when the session opens, and its answer holds until the
session seals: an evening that started on the record stays on it until everybody
disconnects. One opened outside every window is transcribed and answered like
any other and writes nothing down, so it seals as an empty session and takes its
own file away again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from miss_quote.config import file_cfg, transcript_cfg
from miss_quote.transcript.schedule import ALWAYS, Schedule
from miss_quote.utils.logging import get_logger
from miss_quote.utils.slugs import slugify

logger = get_logger(__name__)

# Distinguishes sessions that opened in the same second; the first needs none.
SESSION_ORDINAL_SEPARATOR = "-"
FIRST_REPEATED_SESSION = 2

TIMESTAMP_FIELD = "ts"
USER_ID_FIELD = "user_id"
USER_FIELD = "user"
TEXT_FIELD = "text"


def date_from_filename(path: Path) -> date | None:
    """
    The day a session was taken, from the front of its name.

    Only the date prefix is read, so an ordinal on the end of a name does not
    exempt that session from retention. Both trees are named the same way and
    both age on this, which is why it is one function rather than a copy each.
    """
    taken = _prefix(
        path, transcript_cfg.filename_date_length, transcript_cfg.filename_date_format
    )

    return None if taken is None else taken.date()


def opened_from_filename(path: Path) -> datetime | None:
    """
    The moment a session opened, from the front of its name.

    Aware, in the timezone the name was written in, because the only other time
    a session has on disk is the offset-carrying `ts` of its last line and the
    two are subtracted from each other to decide whether they are one evening.
    """
    opened = _prefix(
        path,
        transcript_cfg.filename_timestamp_length,
        transcript_cfg.filename_timestamp_format,
    )

    if opened is None:
        return None

    return opened.replace(tzinfo=ZoneInfo(transcript_cfg.timezone))


def _prefix(path: Path, length: int, layout: str) -> datetime | None:
    """One fixed-width prefix of a filename, parsed, or nothing if it will not."""
    try:
        return datetime.strptime(path.stem[:length], layout)
    except ValueError:
        return None


@dataclass(frozen=True)
class Source:
    """The guild and channel an utterance came from."""

    guild_id: int
    guild_alias: str
    channel_id: int
    channel: str

    @property
    def relative_directory(self) -> Path:
        """
        Directory this source's transcripts live in, relative to the root.

        The guild is named by its configured alias alone. The alias is fixed in
        configuration rather than read from Discord, so it cannot change
        underneath the tree and needs no ID to stay identifiable.

        Channels are named the same way, without their ID. Renaming one starts a
        new directory with nothing tying it to the old, which is accepted: the
        alternative puts an ID in every path to serve a rare event.
        """
        return Path(slugify(self.guild_alias), slugify(self.channel))


@dataclass(frozen=True)
class Utterance:
    """One transcribed line, as written and as handed to a tool."""

    timestamp: datetime
    user_id: int
    user: str
    text: str

    def as_line(self) -> str:
        return json.dumps(
            {
                TIMESTAMP_FIELD: self.timestamp.isoformat(),
                USER_ID_FIELD: self.user_id,
                USER_FIELD: self.user,
                TEXT_FIELD: self.text,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_line(cls, line: str) -> "Utterance":
        parsed = json.loads(line)
        return cls(
            timestamp=datetime.fromisoformat(parsed[TIMESTAMP_FIELD]),
            user_id=int(parsed[USER_ID_FIELD]),
            user=str(parsed[USER_FIELD]),
            text=str(parsed[TEXT_FIELD]),
        )


def last_spoken(path: Path) -> datetime | None:
    """
    When the last thing in a transcript was said, or nothing if none of it was.

    This is the closest a sealed session comes to recording when it ended: the
    filename is the moment it opened, and nothing on disk is the moment it
    closed. A file that is missing, empty, or unreadable answers nothing rather
    than raising, and the caller decides what an unknown ending means.

    A line that will not parse is skipped, on `Transcript.read`'s rule: one bad
    line should cost one utterance, not the answer.
    """
    spoken: datetime | None = None

    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    spoken = Utterance.from_line(line).timestamp
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError as exc:
        logger.error("Could not read %s to find when it ended: %s", path, exc)
        return None

    return spoken


@dataclass(frozen=True)
class Transcript:
    """
    A closed session: the file it produced, and what that file covers.

    Handed to tools that want a whole conversation rather than a running
    commentary. The lines are not carried in memory — `read` parses them back
    off disk, so a tool sees what was actually written.
    """

    path: Path
    source: Source
    opened: datetime
    closed: datetime
    utterances: int

    @property
    def duration(self) -> timedelta:
        return self.closed - self.opened

    @property
    def empty(self) -> bool:
        """Whether nobody spoke. One that nobody did leaves no file behind."""
        return self.utterances == 0

    def read(self) -> list[Utterance]:
        """
        Every utterance in the file, in the order it was spoken.

        A line that will not parse is skipped rather than raised on: one bad
        line should cost one utterance, not the whole transcript.
        """
        if not self.path.is_file():
            return []

        utterances: list[Utterance] = []
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    utterances.append(Utterance.from_line(line))
                except (ValueError, KeyError, TypeError) as exc:
                    logger.error("Skipping %s line %d: %s", self.path, number, exc)

        return utterances


class TranscriptSession:
    """
    One connection to one voice channel, and the file it appends to.

    The file is created when the session opens rather than on the first
    utterance, so nothing writing to a session that is still going has to handle
    a path that is not there. A session nobody ever spoke in takes it away again
    when it seals; see `close`.

    Whether it writes anything down is settled here, against the moment it
    opened, and does not change afterwards. A window says when an evening may
    start being recorded rather than how long it may run for, so a session that
    opens inside one keeps writing until everybody disconnects — an evening does
    not stop being the evening at midnight, and a transcript cut off
    mid-conversation is worse than either the whole of it or none of it.
    """

    def __init__(
        self,
        path: Path,
        source: Source,
        opened: datetime,
        zone: ZoneInfo,
        schedule: Schedule = ALWAYS,
    ) -> None:
        self._path = path
        self._source = source
        self._opened = opened
        self._zone = zone
        self._capturing = schedule.covers(opened)
        self._utterances = 0
        self._suspended: datetime | None = None
        self._closed: datetime | None = None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def source(self) -> Source:
        return self._source

    @property
    def opened(self) -> datetime:
        return self._opened

    @property
    def capturing(self) -> bool:
        """Whether this session is on the record, as the schedule decided at open."""
        return self._capturing

    def start_capturing(self) -> bool:
        """
        Put a session on the record from here on, reporting whether it moved.

        From here on and no earlier: nothing said while off the record was kept
        anywhere to be written down now, so this starts a transcript rather than
        completing one. What comes back is whether anything changed, so a caller
        can tell somebody who asked for this twice.
        """
        return self._capture(True)

    def stop_capturing(self) -> bool:
        """
        Take a session off the record, reporting whether it moved.

        What is already written stays written. Stopping is a decision about what
        happens next, not a retraction, and a session that has anything in it
        seals with what it had rather than being taken away.
        """
        return self._capture(False)

    def _capture(self, capturing: bool) -> bool:
        moved = self._capturing != capturing
        self._capturing = capturing

        return moved

    @property
    def utterances(self) -> int:
        return self._utterances

    def write(self, user_id: int, user: str, text: str) -> Utterance:
        """
        Note one utterance, appending it if this session is on the record.

        The utterance comes back either way. What the schedule decides is
        whether the line is written down, not whether it happened: the tools
        that read one at a time are handed this object rather than the file, so
        a channel off the record is still transcribed, still fined, and still
        answered out loud.

        `utterances` counts what reached disk, on the same reasoning. It is what
        `close` reads to decide whether a session left anything behind, and a
        file of no lines is not a record that the bot was there.
        """
        utterance = Utterance(
            timestamp=datetime.now(self._zone),
            user_id=user_id,
            user=user,
            text=text,
        )

        if not self._capturing:
            return utterance

        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(utterance.as_line() + "\n")
            handle.flush()

        self._utterances += 1
        return utterance

    def suspend(self) -> None:
        """
        Note that the connection ended, without sealing the transcript.

        A suspended session may still be resumed, and if it is not, it ended
        when the connection did rather than whenever that was noticed.
        """
        self._suspended = datetime.now(self._zone)

    def resume(self) -> None:
        """Take a suspended session back off the clock."""
        self._suspended = None

    def close(self) -> Transcript:
        """
        Seal the session and describe what it produced.

        Idempotent, and the end time is fixed by the first call: a session can
        be closed by the channel emptying, by the bot being disconnected, or by
        the pod terminating, and more than one of those can land.

        A session nobody spoke in leaves nothing behind. A file of no lines is
        not a record that the bot was there, it is a session every reader
        downstream has to recognize and discount — and one of them will forget.
        `summary.store` chains an evening together out of both trees, and an
        empty session an hour after the last real one is a session on that day
        with nothing in it: near enough to be taken for the evening somebody
        asked about, and nothing to tell them once it has been.
        """
        if self._closed is None:
            self._closed = self._suspended or datetime.now(self._zone)

        if self._utterances == 0:
            self._discard()

        return Transcript(
            path=self._path,
            source=self._source,
            opened=self._opened,
            closed=self._closed,
            utterances=self._utterances,
        )

    def _discard(self) -> None:
        """Take the file away, reporting a failure rather than raising on it."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not remove the empty transcript %s: %s", self._path, exc)


class TranscriptWriter:
    """Opens sessions under `<root>/<guild>/<channel>/`, pruning by age."""

    def __init__(
        self,
        directory: Path | None = None,
        timezone: str | None = None,
        retention_days: int | None = None,
        schedules: Callable[[int, str], Schedule] | None = None,
    ) -> None:
        self._directory = Path(directory or transcript_cfg.directory)
        self._zone = ZoneInfo(timezone or transcript_cfg.timezone)
        self._retention_days = (
            transcript_cfg.retention_days if retention_days is None else retention_days
        )
        # A resolver rather than one schedule: which rooms are on the record is
        # per server and per channel, and the writer serves every one of them.
        self._schedules = file_cfg.schedule_for if schedules is None else schedules

        self._directory.mkdir(parents=True, exist_ok=True)
        self.prune()

    @property
    def retention_enabled(self) -> bool:
        return self._retention_days >= 1

    def open(self, source: Source) -> TranscriptSession:
        """
        Start a session for a channel the bot has just joined.

        Pruning runs here. Sessions are the only recurring event the writer sees
        now that files no longer roll over on a date, and a bot that joins
        nothing for a week has nothing worth pruning anyway.

        This is the only moment the schedule is consulted, so it is also the
        only moment at which a session being off the record is worth saying: the
        alternative is finding out by looking for a transcript that was never
        going to be written.
        """
        self.prune()

        opened = datetime.now(self._zone)
        session = TranscriptSession(
            path=self._path_for(source, opened),
            source=source,
            opened=opened,
            zone=self._zone,
            schedule=self._schedules(source.guild_id, source.channel),
        )

        if not session.capturing:
            logger.info(
                "Session in %s opened off the record; the capture schedule covers "
                "nothing at %s, so nothing said will be written down.",
                source.relative_directory,
                opened.strftime(transcript_cfg.filename_timestamp_format),
            )

        return session

    def prune(self) -> list[Path]:
        """
        Delete transcripts older than the retention window.

        Age comes from the filename, not mtime: the filename is the
        authoritative record of when a transcript was taken, while mtime
        misjudges a file appended to late or restored from backup.
        """
        if not self.retention_enabled:
            return []

        cutoff = datetime.now(self._zone).date() - timedelta(days=self._retention_days)
        removed: list[Path] = []

        for path in self._directory.rglob(f"*{transcript_cfg.filename_suffix}"):
            file_date = date_from_filename(path)
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

    def _path_for(self, source: Source, opened: datetime) -> Path:
        """
        A name for a session, distinct from every session already filed.

        Names have one-second resolution and say nothing about which channel
        they came from, so two sessions can want the same one: two channels
        whose names reduce to the same slug, or two servers sharing an alias,
        opening within a second of each other. Without the ordinal the second
        would append to the first, and a tool handed that transcript would read
        an unrelated conversation as part of its own.

        Rejoining one channel does not reach here — an open session is reused by
        channel, whether or not the connection to it dropped.
        """
        directory = self._directory / source.relative_directory
        stem = opened.strftime(transcript_cfg.filename_timestamp_format)

        path = directory / f"{stem}{transcript_cfg.filename_suffix}"
        ordinal = FIRST_REPEATED_SESSION

        while path.exists():
            name = f"{stem}{SESSION_ORDINAL_SEPARATOR}{ordinal}"
            path = directory / f"{name}{transcript_cfg.filename_suffix}"
            ordinal += 1

        return path
