import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import miss_quote.transcript.writer as writer_module
from miss_quote.transcript.schedule import ALWAYS, Schedule
from miss_quote.transcript.writer import Source, TranscriptWriter, slugify

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
USER_ID = 1234567890
USER = "someone"

SOURCE = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=456123, channel="general-voice"
)
OTHER_CHANNEL = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=999888, channel="side-room"
)
OTHER_GUILD = Source(
    guild_id=111222333, guild_alias="somewhere-else", channel_id=456123, channel="general-voice"
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


def _writer(
    tmp_path, retention_days: int = KEEP_FOREVER, schedule: Schedule = ALWAYS
) -> TranscriptWriter:
    return TranscriptWriter(
        directory=tmp_path,
        timezone=TIMEZONE,
        retention_days=retention_days,
        schedule=schedule,
    )


# ── sessions ──────────────────────────────────────


def test_a_session_spanning_midnight_stays_in_one_file(tmp_path, frozen_clock):
    """The file is named when the bot joins, and keeps that name until it leaves."""
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 26, 23, 59, 30, tzinfo=zone))
    writer = _writer(tmp_path)
    session = writer.open(SOURCE)
    session.write(USER_ID, USER, "before midnight")

    frozen_clock(datetime(2026, 7, 27, 0, 0, 30, tzinfo=zone))
    session.write(USER_ID, USER, "after midnight")

    assert session.path.name == "2026-07-26T23-59-30.jsonl"
    assert [json.loads(line)[writer_module.TEXT_FIELD] for line in
            session.path.read_text().strip().splitlines()] == [
        "before midnight",
        "after midnight",
    ]


def test_reconnecting_starts_a_new_transcript(tmp_path, frozen_clock):
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=zone))
    writer = _writer(tmp_path)
    first = writer.open(SOURCE)
    first.write(USER_ID, USER, "first visit")
    first.close()

    frozen_clock(datetime(2026, 7, 26, 14, 30, 0, tzinfo=zone))
    second = writer.open(SOURCE)
    second.write(USER_ID, USER, "second visit")

    assert first.path != second.path
    assert first.path.parent == second.path.parent
    assert second.path.name == "2026-07-26T14-30-00.jsonl"


def test_a_session_with_nobody_speaking_leaves_no_file(tmp_path, frozen_clock):
    """
    A file of no lines is not a record that the bot was there, it is a session
    every reader downstream has to recognize and discount.
    """
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)

    assert session.path.is_file()

    transcript = session.close()

    assert transcript.empty
    assert not transcript.path.exists()
    assert transcript.utterances == 0
    assert transcript.read() == []


def test_a_session_somebody_spoke_in_keeps_its_file(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)
    session.write(USER_ID, USER, "something")

    transcript = session.close()

    assert not transcript.empty
    assert transcript.path.is_file()


def test_discarding_an_empty_session_twice_is_not_an_error(tmp_path, frozen_clock):
    """More than one thing can end a session, and the first took the file away."""
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)

    first = session.close()
    second = session.close()

    assert first == second
    assert not second.path.exists()


def test_closing_twice_reports_the_same_transcript(tmp_path, frozen_clock):
    """More than one thing can end a session; the second must not move the end time."""
    zone = ZoneInfo(TIMEZONE)
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=zone))
    session = _writer(tmp_path).open(SOURCE)
    session.write(USER_ID, USER, "something")

    first = session.close()
    frozen_clock(datetime(2026, 7, 26, 11, 0, 0, tzinfo=zone))
    second = session.close()

    assert first == second


def test_a_closed_transcript_describes_what_it_covers(tmp_path, frozen_clock):
    zone = ZoneInfo(TIMEZONE)
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=zone))
    session = _writer(tmp_path).open(SOURCE)
    session.write(USER_ID, USER, "first")
    session.write(USER_ID, USER, "second")

    frozen_clock(datetime(2026, 7, 26, 10, 45, 0, tzinfo=zone))
    transcript = session.close()

    assert transcript.source == SOURCE
    assert transcript.utterances == 2
    assert transcript.duration.total_seconds() == 45 * 60
    assert [utterance.text for utterance in transcript.read()] == ["first", "second"]


def test_a_transcript_survives_a_line_it_cannot_parse(tmp_path, frozen_clock):
    """One bad line costs one utterance, not the whole conversation."""
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)
    session.write(USER_ID, USER, "good")

    with session.path.open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")

    session.write(USER_ID, USER, "also good")

    assert [utterance.text for utterance in session.close().read()] == [
        "good",
        "also good",
    ]


# ── the capture schedule ──────────────────────────

# 2026-07-29 is a Wednesday.
EVENING = Schedule.parse(["Wed 17:00-00:00"])


def test_a_session_opening_inside_the_window_is_written_down(tmp_path, frozen_clock):
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 18, 0, 0, tzinfo=zone))
    session = _writer(tmp_path, schedule=EVENING).open(SOURCE)
    session.write(USER_ID, USER, "on the record")

    assert session.capturing
    assert session.utterances == 1
    assert "on the record" in session.path.read_text()


def test_a_session_opening_outside_the_window_is_handed_over_and_not_written_down(
    tmp_path, frozen_clock
):
    """
    The tools read one utterance at a time, and are handed this object rather
    than the file: a channel off the record is still fined and still answered.
    """
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 11, 0, 0, tzinfo=zone))
    session = _writer(tmp_path, schedule=EVENING).open(SOURCE)
    utterance = session.write(USER_ID, USER, "off the record")

    assert not session.capturing
    assert utterance.text == "off the record"
    assert utterance.user == USER
    assert session.utterances == 0
    assert session.path.read_text() == ""


def test_a_session_keeps_writing_past_the_end_of_its_window(tmp_path, frozen_clock):
    """
    A window says when an evening may start being recorded, not how long it may
    run for. An evening does not stop being the evening at midnight, and a
    transcript cut off mid-conversation is worse than the whole of it or none.
    """
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 23, 59, 0, tzinfo=zone))
    session = _writer(tmp_path, schedule=EVENING).open(SOURCE)
    session.write(USER_ID, USER, "before midnight")

    frozen_clock(datetime(2026, 7, 30, 3, 0, 30, tzinfo=zone))
    session.write(USER_ID, USER, "long after midnight")

    assert session.utterances == 2
    assert [json.loads(line)[writer_module.TEXT_FIELD] for line in
            session.path.read_text().strip().splitlines()] == [
        "before midnight",
        "long after midnight",
    ]


def test_a_session_opened_early_does_not_start_writing_when_the_window_arrives(
    tmp_path, frozen_clock
):
    """The decision is taken once, at open. The other half of the rule above."""
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 16, 59, 0, tzinfo=zone))
    session = _writer(tmp_path, schedule=EVENING).open(SOURCE)

    frozen_clock(datetime(2026, 7, 29, 20, 0, 0, tzinfo=zone))
    session.write(USER_ID, USER, "well inside the window")

    assert not session.capturing
    assert session.utterances == 0


def test_rejoining_after_the_window_opens_a_session_that_is_written_down(
    tmp_path, frozen_clock
):
    """Which is what makes the decision recoverable: leave, and come back."""
    zone = ZoneInfo(TIMEZONE)
    writer = _writer(tmp_path, schedule=EVENING)

    frozen_clock(datetime(2026, 7, 29, 16, 59, 0, tzinfo=zone))
    early = writer.open(SOURCE)
    early.close()

    frozen_clock(datetime(2026, 7, 29, 17, 0, 0, tzinfo=zone))
    later = writer.open(SOURCE)
    later.write(USER_ID, USER, "on the record")

    assert not early.capturing
    assert later.capturing
    assert later.utterances == 1


def test_a_session_off_the_record_leaves_no_file(tmp_path, frozen_clock):
    """It seals as an empty session, which is what it is: nothing was written down."""
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 11, 0, 0, tzinfo=zone))
    session = _writer(tmp_path, schedule=EVENING).open(SOURCE)
    session.write(USER_ID, USER, "off the record")

    transcript = session.close()

    assert transcript.empty
    assert not transcript.path.exists()


def test_no_schedule_writes_every_session_down(tmp_path, frozen_clock):
    zone = ZoneInfo(TIMEZONE)

    frozen_clock(datetime(2026, 7, 29, 4, 0, 0, tzinfo=zone))
    session = _writer(tmp_path).open(SOURCE)
    session.write(USER_ID, USER, "at an unreasonable hour")

    assert session.capturing
    assert session.utterances == 1


# ── format and layout ─────────────────────────────


def test_each_line_is_json_with_the_expected_fields(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 21, 14, 3, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)

    session.write(USER_ID, USER, "that should work")
    line = json.loads(session.path.read_text().strip())

    assert line["user_id"] == USER_ID
    assert line["user"] == USER
    assert line["text"] == "that should work"
    assert datetime.fromisoformat(line["ts"]).utcoffset() is not None


def test_origin_lives_in_the_path_not_the_line(tmp_path, frozen_clock):
    """The directory carries guild and channel, so the line does not repeat them."""
    frozen_clock(datetime(2026, 7, 26, 21, 14, 3, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)

    session.write(USER_ID, USER, "that should work")
    line = json.loads(session.path.read_text().strip())

    assert "guild" not in line
    assert "guild_id" not in line
    assert "channel" not in line
    assert "channel_id" not in line

    assert session.path.relative_to(tmp_path).parts == (
        "first-server",
        "general-voice",
        "2026-07-26T21-14-03.jsonl",
    )


def test_appends_accumulate_within_a_session(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    session = _writer(tmp_path).open(SOURCE)

    session.write(USER_ID, USER, "first")
    session.write(USER_ID, USER, "second")

    lines = session.path.read_text().strip().splitlines()

    assert [json.loads(line)["text"] for line in lines] == ["first", "second"]


def test_channels_and_guilds_are_kept_apart(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    here = writer.open(SOURCE)
    next_door = writer.open(OTHER_CHANNEL)
    elsewhere = writer.open(OTHER_GUILD)

    here.write(USER_ID, USER, "general")
    next_door.write(USER_ID, USER, "side room")

    assert here.path != next_door.path
    assert next_door.path != elsewhere.path

    assert here.path.parent.parent == next_door.path.parent.parent
    assert here.path.parent.parent != elsewhere.path.parent.parent

    assert json.loads(here.path.read_text().strip())["text"] == "general"
    assert json.loads(next_door.path.read_text().strip())["text"] == "side room"


# ── slugs ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("example.net", "example-net"),
        ("general-voice", "general-voice"),
        ("Someone's Server", "someone-s-server"),
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

    hostile = Source(
        guild_id=13, guild_alias="../../etc", channel_id=14, channel="../../passwd"
    )
    session = _writer(tmp_path).open(hostile)
    session.write(USER_ID, USER, "nice try")

    assert tmp_path in session.path.parents
    assert session.path.relative_to(tmp_path).parts == (
        "etc",
        "passwd",
        "2026-07-26T10-00-00.jsonl",
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


def test_two_servers_sharing_an_alias_share_a_directory(tmp_path, frozen_clock):
    """
    Names are all that separate directories now, for servers and channels both.

    Nothing here can prevent a collision; the bot warns instead, at startup for
    aliases and on join for channels. These pin the consequence so the warnings
    cannot quietly stop mattering. Sessions keep the two conversations in
    separate files, but nothing in the tree says which file came from where.
    """
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    one = Source(guild_id=111, guild_alias="shared", channel_id=1, channel="general-voice")
    two = Source(guild_id=222, guild_alias="shared", channel_id=1, channel="general-voice")

    first, second = writer.open(one).path, writer.open(two).path

    assert first.parent == second.parent
    assert first != second


def test_two_channels_slugging_alike_share_a_directory(tmp_path, frozen_clock):
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    one = Source(guild_id=111, guild_alias="a", channel_id=1, channel="General")
    two = Source(guild_id=111, guild_alias="a", channel_id=2, channel="general")

    first, second = writer.open(one).path, writer.open(two).path

    assert first.parent == second.parent
    assert first != second


def test_concurrent_sessions_in_one_directory_do_not_share_a_file(tmp_path, frozen_clock):
    """Two sessions opening in the same second must not write into one transcript."""
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    first = writer.open(SOURCE)
    second = writer.open(SOURCE)

    first.write(USER_ID, USER, "mine")
    second.write(USER_ID, USER, "also mine")

    assert [utterance.text for utterance in first.close().read()] == ["mine"]
    assert [utterance.text for utterance in second.close().read()] == ["also mine"]


def test_a_repeated_session_is_still_pruned(tmp_path, frozen_clock):
    """The ordinal on the end of a name must not exempt it from retention."""
    zone = ZoneInfo(TIMEZONE)
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=zone))
    writer = _writer(tmp_path, retention_days=7)

    repeated = writer.open(SOURCE)
    writer.open(SOURCE)

    frozen_clock(datetime(2026, 8, 26, 10, 0, 0, tzinfo=zone))

    assert repeated.path in writer.prune()


def test_a_renamed_channel_starts_a_new_directory(tmp_path, frozen_clock):
    """Accepted: paths carry no channel ID, so a rename has nothing to follow."""
    frozen_clock(datetime(2026, 7, 26, 10, 0, 0, tzinfo=ZoneInfo(TIMEZONE)))
    writer = _writer(tmp_path)

    before = Source(guild_id=111, guild_alias="a", channel_id=1, channel="general")
    after = Source(guild_id=111, guild_alias="a", channel_id=1, channel="lounge")

    assert writer.open(before).path != writer.open(after).path
