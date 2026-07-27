import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import transcript.writer as writer_module
from transcript.writer import TranscriptWriter

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
USER_ID = 195623847
USER = "erik"
CHANNEL = "general-voice"


class FrozenDatetime(datetime):
    """datetime whose `now` returns a value the test controls."""
    current: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(writer_module, "datetime", FrozenDatetime)

    def move_to(moment: datetime) -> None:
        FrozenDatetime.current = moment

    return move_to


def _writer(tmp_path, retention_days: int = KEEP_FOREVER) -> TranscriptWriter:
    return TranscriptWriter(
        directory=tmp_path, timezone=TIMEZONE, retention_days=retention_days
    )


def test_utterances_either_side_of_midnight_land_in_two_files(tmp_path, frozen_clock):
    zone = ZoneInfo(TIMEZONE)
    before_midnight = datetime(2026, 7, 26, 23, 59, 30, tzinfo=zone)
    after_midnight = datetime(2026, 7, 27, 0, 0, 30, tzinfo=zone)

    frozen_clock(before_midnight)
    writer = _writer(tmp_path)
    first = writer.write(USER_ID, USER, CHANNEL, "before midnight")

    frozen_clock(after_midnight)
    second = writer.write(USER_ID, USER, CHANNEL, "after midnight")

    assert first.name == "2026-07-26.jsonl"
    assert second.name == "2026-07-27.jsonl"
    assert first != second


def test_each_line_is_json_with_the_expected_fields(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 21, 14, 3, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    path = writer.write(USER_ID, USER, CHANNEL, "that should work")

    line = json.loads(path.read_text().strip())

    assert line["user_id"] == USER_ID
    assert line["user"] == USER
    assert line["channel"] == CHANNEL
    assert line["text"] == "that should work"
    assert datetime.fromisoformat(line["ts"]).utcoffset() is not None


def test_appends_accumulate_within_a_day(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    writer.write(USER_ID, USER, CHANNEL, "first")
    path = writer.write(USER_ID, USER, CHANNEL, "second")

    lines = path.read_text().strip().splitlines()

    assert [json.loads(line)["text"] for line in lines] == ["first", "second"]
