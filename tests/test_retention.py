from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from config import transcript_cfg
from transcript.writer import Source, TranscriptWriter

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
DISABLED_BY_ZERO = 0
KEEP_A_WEEK = 7

# Any time of day will do; retention is decided by the date part alone.
SEEDED_TIME = (14, 30, 0)

SOURCE = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=456123, channel="general-voice"
)
OTHER_CHANNEL = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=999888, channel="side-room"
)


def _today() -> date:
    """
    Resolve today in TIMEZONE, the clock the writer prunes against.

    date.today() reads the host zone, which disagrees with TIMEZONE for part
    of every day and makes the retention boundary depend on when the suite runs.
    """
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _name(days_ago: int) -> str:
    """The filename a session opened that many days ago would have."""
    day = _today() - timedelta(days=days_ago)
    moment = datetime.combine(day, datetime.min.time()).replace(
        hour=SEEDED_TIME[0], minute=SEEDED_TIME[1], second=SEEDED_TIME[2]
    )
    return f"{moment.strftime(transcript_cfg.filename_timestamp_format)}{transcript_cfg.filename_suffix}"


def _seed(directory, days_ago: int, source=SOURCE) -> None:
    channel_directory = directory / source.relative_directory
    channel_directory.mkdir(parents=True, exist_ok=True)
    (channel_directory / _name(days_ago)).write_text("{}\n")


def _names(directory) -> set[str]:
    return {path.name for path in directory.rglob("*.jsonl")}


@pytest.mark.parametrize("retention_days", [KEEP_FOREVER, DISABLED_BY_ZERO])
def test_pruning_disabled_keeps_everything(tmp_path, retention_days: int) -> None:
    _seed(tmp_path, days_ago=365)
    _seed(tmp_path, days_ago=1)

    writer = TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=retention_days
    )
    removed = writer.prune()

    assert removed == []
    assert len(_names(tmp_path)) == 2


def test_positive_retention_removes_only_old_files(tmp_path) -> None:
    _seed(tmp_path, days_ago=30)
    _seed(tmp_path, days_ago=8)
    _seed(tmp_path, days_ago=3)
    _seed(tmp_path, days_ago=0)

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    survivors = _names(tmp_path)

    assert _name(3) in survivors
    assert _name(0) in survivors
    assert _name(30) not in survivors
    assert _name(8) not in survivors


def test_age_comes_from_filename_not_mtime(tmp_path) -> None:
    """A stale file touched recently must still be pruned."""
    old = tmp_path / SOURCE.relative_directory / _name(90)
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("{}\n")
    old.touch()  # mtime is now; the filename says otherwise

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    assert not old.exists()


def test_unrecognised_filenames_are_left_alone(tmp_path) -> None:
    stray = tmp_path / SOURCE.relative_directory / "notes.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}\n")

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    assert stray.exists()


def test_pruning_reaches_every_channel(tmp_path) -> None:
    """Retention walks the whole tree, not just the root."""
    _seed(tmp_path, days_ago=30, source=SOURCE)
    _seed(tmp_path, days_ago=30, source=OTHER_CHANNEL)
    _seed(tmp_path, days_ago=1, source=OTHER_CHANNEL)

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    assert _names(tmp_path) == {_name(1)}


def test_opening_a_session_prunes(tmp_path) -> None:
    """Sessions are the only recurring event the writer sees now."""
    writer = TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )
    _seed(tmp_path, days_ago=30)

    session = writer.open(SOURCE)

    assert _name(30) not in _names(tmp_path)
    assert session.path.is_file()
