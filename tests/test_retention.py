from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from transcript.writer import TranscriptWriter

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
DISABLED_BY_ZERO = 0
KEEP_A_WEEK = 7


def _today() -> date:
    """
    Resolve today in TIMEZONE, the clock the writer prunes against.

    date.today() reads the host zone, which disagrees with TIMEZONE for part
    of every day and makes the retention boundary depend on when the suite runs.
    """
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _seed(directory, days_ago: int) -> None:
    day = _today() - timedelta(days=days_ago)
    (directory / f"{day.isoformat()}.jsonl").write_text("{}\n")


def _names(directory) -> set[str]:
    return {path.name for path in directory.glob("*.jsonl")}


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
    today = _today()

    assert f"{(today - timedelta(days=3)).isoformat()}.jsonl" in survivors
    assert f"{today.isoformat()}.jsonl" in survivors
    assert f"{(today - timedelta(days=30)).isoformat()}.jsonl" not in survivors
    assert f"{(today - timedelta(days=8)).isoformat()}.jsonl" not in survivors


def test_age_comes_from_filename_not_mtime(tmp_path) -> None:
    """A stale file touched recently must still be pruned."""
    old = tmp_path / f"{(_today() - timedelta(days=90)).isoformat()}.jsonl"
    old.write_text("{}\n")
    old.touch()  # mtime is now; the filename says otherwise

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    assert not old.exists()


def test_unrecognised_filenames_are_left_alone(tmp_path) -> None:
    stray = tmp_path / "notes.jsonl"
    stray.write_text("{}\n")

    TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=KEEP_A_WEEK
    )

    assert stray.exists()
