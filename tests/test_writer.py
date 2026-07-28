import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import transcript.writer as writer_module
from transcript.writer import Source, TranscriptWriter, slugify

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
USER_ID = 1234567890
USER = "someone"

SOURCE = Source(
    guild_id=987654321, guild="ste.haus", channel_id=456123, channel="general-voice"
)
OTHER_CHANNEL = Source(
    guild_id=987654321, guild="ste.haus", channel_id=999888, channel="side-room"
)
OTHER_GUILD = Source(
    guild_id=111222333, guild="somewhere else", channel_id=456123, channel="general-voice"
)


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
    first = writer.write(SOURCE, USER_ID, USER, "before midnight")

    frozen_clock(after_midnight)
    second = writer.write(SOURCE, USER_ID, USER, "after midnight")

    assert first.name == "2026-07-26.jsonl"
    assert second.name == "2026-07-27.jsonl"
    assert first.parent == second.parent


def test_each_line_is_json_with_the_expected_fields(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 21, 14, 3, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    path = writer.write(SOURCE, USER_ID, USER, "that should work")

    line = json.loads(path.read_text().strip())

    assert line["user_id"] == USER_ID
    assert line["user"] == USER
    assert line["text"] == "that should work"
    assert datetime.fromisoformat(line["ts"]).utcoffset() is not None


def test_origin_lives_in_the_path_not_the_line(tmp_path, frozen_clock):
    """The directory carries guild and channel, so the line does not repeat them."""
    frozen_clock(datetime(2026, 7, 26, 21, 14, 3, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    path = writer.write(SOURCE, USER_ID, USER, "that should work")
    line = json.loads(path.read_text().strip())

    assert "guild" not in line
    assert "guild_id" not in line
    assert "channel" not in line
    assert "channel_id" not in line

    assert path.relative_to(tmp_path).parts == (
        "987654321-ste-haus",
        "456123-general-voice",
        "2026-07-26.jsonl",
    )


def test_appends_accumulate_within_a_day(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    writer.write(SOURCE, USER_ID, USER, "first")
    path = writer.write(SOURCE, USER_ID, USER, "second")

    lines = path.read_text().strip().splitlines()

    assert [json.loads(line)["text"] for line in lines] == ["first", "second"]


def test_channels_and_guilds_are_kept_apart(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    here = writer.write(SOURCE, USER_ID, USER, "general")
    next_door = writer.write(OTHER_CHANNEL, USER_ID, USER, "side room")
    elsewhere = writer.write(OTHER_GUILD, USER_ID, USER, "other guild")

    assert here != next_door
    assert next_door != elsewhere

    assert here.parent.parent == next_door.parent.parent
    assert here.parent.parent != elsewhere.parent.parent

    assert json.loads(here.read_text().strip())["text"] == "general"
    assert json.loads(next_door.read_text().strip())["text"] == "side room"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ste.haus", "ste-haus"),
        ("general-voice", "general-voice"),
        ("Erik's Server", "erik-s-server"),
        ("🎮 Gaming / Chat", "gaming-chat"),
        ("...hidden", "hidden"),
        ("../../escape", "escape"),
        ("a/../b", "a-b"),
        ("🎉", "unnamed"),
        ("", "unnamed"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_slug_cannot_escape_the_root(tmp_path, frozen_clock):
    """A guild named to look like a traversal stays inside the root."""
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    hostile = Source(
        guild_id=13, guild="../../etc", channel_id=14, channel="../../passwd"
    )
    path = writer.write(hostile, USER_ID, USER, "nice try")

    assert tmp_path in path.parents
    assert path.relative_to(tmp_path).parts == (
        "13-etc",
        "14-passwd",
        "2026-07-26.jsonl",
    )


@pytest.mark.parametrize(
    "hostile",
    ["../../etc", "..", ".", "a/../b", "./.././x", "nested/path"],
)
def test_slug_yields_no_dots_or_separators(hostile: str) -> None:
    """No slug can contain a character that means anything to a path."""
    slug = slugify(hostile)

    assert "." not in slug
    assert "/" not in slug
    assert "\\" not in slug
    assert slug not in {"..", "."}
