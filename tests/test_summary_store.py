import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from miss_quote.config import transcript_cfg
from miss_quote.summary.store import SummaryStore
from miss_quote.summary.when import LATEST, When
from miss_quote.transcript.writer import Source, Transcript

KEEP_FOREVER = -1
KEEP_A_WEEK = 7

# Wide enough that a break is still one evening, narrow enough that two
# evenings on one day stay two. The tool's default.
GAP = timedelta(minutes=10)

EXACT_DAY = 0
NEAREST_DAYS = 3

ZONE = ZoneInfo(transcript_cfg.timezone)

OPENED = datetime(2026, 7, 26, 20, 14, 3, tzinfo=timezone.utc)
CLOSED = datetime(2026, 7, 26, 22, 31, 55, tzinfo=timezone.utc)

SOURCE = Source(
    guild_id=987654321,
    guild_alias="first-server",
    channel_id=456123,
    channel="General Voice",
)
OTHER_CHANNEL = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=999888, channel="side-room"
)

SUMMARY = "They argued about the rules for an hour and nobody won."


def _transcript(root: Path, name: str, source: Source = SOURCE) -> Transcript:
    path = root / source.relative_directory / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    return Transcript(path=path, source=source, opened=OPENED, closed=CLOSED, utterances=12)


def _store(tmp_path: Path, retention_days: int = KEEP_FOREVER) -> SummaryStore:
    return SummaryStore(
        directory=tmp_path / "summaries",
        retention_days=retention_days,
        transcripts=tmp_path / "transcripts",
    )


# ── the chaining helpers ──────────────────────


def _at(*parts: int) -> datetime:
    """A moment in the timezone the transcripts are named in."""
    return datetime(*parts, tzinfo=ZONE)


def _stem(opened: datetime) -> str:
    return opened.strftime(transcript_cfg.filename_timestamp_format)


def _session(
    tmp_path: Path,
    opened: datetime,
    *,
    spoken: datetime | None = None,
    summary: str | None = SUMMARY,
    transcript: bool = True,
    source: Source = SOURCE,
) -> str:
    """
    One filed session, as the two trees would have it.

    `spoken` is when the last thing in it was said, which is the only record of
    when a session ended. Left out, the transcript is there and empty — which is
    what a session nobody talked in looks like.
    """
    stem = _stem(opened)

    if transcript:
        path = tmp_path / "transcripts" / source.relative_directory / f"{stem}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        if spoken is None:
            path.touch()
        else:
            line = {
                "ts": spoken.isoformat(),
                "user_id": 1234,
                "user": "someone",
                "text": "and then what happened",
            }
            path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    if summary is not None:
        written = tmp_path / "summaries" / source.relative_directory / f"{stem}.txt"
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(summary, encoding="utf-8")

    return stem


# ── the shape of the tree ─────────────────────


def test_the_summary_mirrors_the_transcripts_path(tmp_path):
    """Same guild and channel directories, same stem, different root and suffix."""
    store = _store(tmp_path)
    transcript = _transcript(tmp_path / "transcripts", "2026-07-26T20-14-03")

    path = store.path_for(transcript)

    assert path.parent == tmp_path / "summaries" / "first-server" / "general-voice"
    assert path.name == "2026-07-26T20-14-03.txt"


def test_a_session_that_took_an_ordinal_keeps_it(tmp_path):
    """Two sessions that could not share a transcript name cannot share a summary."""
    store = _store(tmp_path)
    transcript = _transcript(tmp_path / "transcripts", "2026-07-26T20-14-03-2")

    assert store.path_for(transcript).name == "2026-07-26T20-14-03-2.txt"


def test_writing_leaves_the_summary_and_nothing_else(tmp_path):
    store = _store(tmp_path)
    transcript = _transcript(tmp_path / "transcripts", "2026-07-26T20-14-03")

    path = store.write(transcript, SUMMARY)

    assert path is not None
    assert path.read_text(encoding="utf-8") == SUMMARY
    assert list(path.parent.glob("*.partial")) == []


def test_an_unwritable_directory_costs_the_summary_and_not_the_process(tmp_path, monkeypatch):
    store = _store(tmp_path)
    transcript = _transcript(tmp_path / "transcripts", "2026-07-26T20-14-03")

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)

    assert store.write(transcript, SUMMARY) is None


# ── the most recent evening ───────────────────


def test_latest_is_the_newest_session_in_that_channel(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 26, 20, 14, 3), summary="the older one")
    newest = _session(tmp_path, _at(2026, 7, 27, 9, 31, 55), summary="the newer one")

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert "the newer one" in found.read()
    assert "the older one" not in found.read()
    assert found.name == newest


def test_latest_reads_the_filename_rather_than_the_mtime(tmp_path):
    """
    The name is when the session was; the mtime is when the file happened to be
    written, which differs the moment anything is regenerated or restored.
    """
    store = _store(tmp_path)

    older = _session(tmp_path, _at(2026, 7, 26, 20, 14, 3), summary="the older one")
    _session(tmp_path, _at(2026, 7, 27, 9, 31, 55), summary="the newer one")

    written = tmp_path / "summaries" / SOURCE.relative_directory
    touched = (written / "2026-07-27T09-31-55.txt").stat().st_mtime + 60
    os.utime(written / f"{older}.txt", (touched, touched))

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert "the newer one" in found.read()


def test_channels_do_not_see_each_others_summaries(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 26, 20, 14, 3), summary="in general")

    assert store.latest(OTHER_CHANNEL, GAP) is None


def test_a_channel_with_no_summaries_has_no_latest(tmp_path):
    assert _store(tmp_path).latest(SOURCE, GAP) is None


def test_sessions_with_no_summaries_anywhere_are_nothing_to_tell(tmp_path):
    """A transcript nobody wrote about is not an evening anybody can be told."""
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 26, 20, 14, 3), summary=None)

    assert store.latest(SOURCE, GAP) is None


# ── one evening, several sessions ─────────────


def test_a_short_break_is_one_evening(tmp_path):
    """Nine minutes between the talking stopping and starting again."""
    store = _store(tmp_path)

    first = _session(
        tmp_path,
        _at(2026, 7, 26, 20, 0, 0),
        spoken=_at(2026, 7, 26, 21, 0, 0),
        summary="the first half",
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 21, 9, 0),
        spoken=_at(2026, 7, 26, 22, 0, 0),
        summary="the second half",
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert found.parts == 2
    assert found.name == first
    assert "the first half" in found.read()
    assert "the second half" in found.read()


def test_a_long_break_is_two_evenings(tmp_path):
    """Eleven minutes, and the earlier half is a different conversation."""
    store = _store(tmp_path)

    _session(
        tmp_path,
        _at(2026, 7, 26, 20, 0, 0),
        spoken=_at(2026, 7, 26, 21, 0, 0),
        summary="the afternoon",
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 21, 11, 0),
        spoken=_at(2026, 7, 26, 22, 0, 0),
        summary="the evening",
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert found.parts == 1
    assert "the afternoon" not in found.read()


def test_the_gap_is_measured_from_when_the_talking_stopped(tmp_path):
    """
    Close to open, not open to open.

    The filename is only when a session started. Four hours of conversation
    followed five minutes later by more of it is one evening, and anything
    reading the two names alone sees four hours between them and says otherwise.
    """
    store = _store(tmp_path)

    first = _session(
        tmp_path,
        _at(2026, 7, 26, 18, 0, 0),
        spoken=_at(2026, 7, 26, 22, 0, 0),
        summary="the long first half",
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 22, 5, 0),
        spoken=_at(2026, 7, 26, 23, 0, 0),
        summary="the rest of it",
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert found.parts == 2
    assert found.name == first


def test_a_session_too_short_to_summarize_bridges_the_two_around_it(tmp_path):
    """
    A session under the summarizing threshold has no summary and is still what
    holds an evening together. Looking only at summaries breaks the chain at
    exactly the point something has to hold it.
    """
    store = _store(tmp_path)

    first = _session(
        tmp_path,
        _at(2026, 7, 26, 20, 0, 0),
        spoken=_at(2026, 7, 26, 21, 0, 0),
        summary="before the reconnect",
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 21, 5, 0),
        spoken=_at(2026, 7, 26, 21, 6, 0),
        summary=None,
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 21, 8, 0),
        spoken=_at(2026, 7, 26, 22, 0, 0),
        summary="after the reconnect",
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert len(found.sessions) == 3
    assert found.parts == 2
    assert found.name == first
    assert "before the reconnect" in found.read()
    assert "after the reconnect" in found.read()


def test_a_short_session_at_the_end_of_a_day_does_not_hide_it(tmp_path):
    """
    The last session on a day is the first anchor tried and need not be one
    anybody wrote about. An hour after the evening ended, somebody rejoining
    for two minutes must not answer for the whole of it.
    """
    store = _store(tmp_path)

    evening = _session(
        tmp_path,
        _at(2026, 7, 29, 20, 27, 26),
        spoken=_at(2026, 7, 29, 20, 47, 45),
        summary="they argued about the rules",
    )
    _session(
        tmp_path,
        _at(2026, 7, 29, 21, 47, 44),
        spoken=_at(2026, 7, 29, 21, 48, 30),
        summary=None,
    )

    found = store.find(SOURCE, When(target=date(2026, 7, 29), tolerance_days=EXACT_DAY), GAP)

    assert found is not None
    assert found.name == evening
    assert "they argued about the rules" in found.read()


def test_asking_in_the_middle_of_a_session_finds_the_one_before_it(tmp_path):
    """
    A session still in progress is the newest thing in the channel and has no
    summary, since that is written when the transcript seals. "Last time" asked
    from inside one means the conversation before it.
    """
    store = _store(tmp_path)

    previous = _session(
        tmp_path,
        _at(2026, 7, 29, 20, 27, 26),
        spoken=_at(2026, 7, 29, 22, 40, 0),
        summary="the evening before",
    )
    _session(
        tmp_path,
        _at(2026, 8, 1, 9, 39, 38),
        spoken=_at(2026, 8, 1, 9, 39, 53),
        summary=None,
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert found.name == previous
    assert "the evening before" in found.read()


def test_a_transcript_that_is_gone_stops_the_chain(tmp_path):
    """
    An unknown ending reads as no length at all, so the chain stops rather than
    being stitched on a guess. Summaries outliving transcripts is a thing a
    deployment is told it may want.
    """
    store = _store(tmp_path)

    _session(
        tmp_path,
        _at(2026, 7, 26, 20, 0, 0),
        summary="the pruned half",
        transcript=False,
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 22, 5, 0),
        spoken=_at(2026, 7, 26, 23, 0, 0),
        summary="the half still on disk",
    )

    found = store.latest(SOURCE, GAP)

    assert found is not None
    assert found.parts == 1
    assert "the pruned half" not in found.read()


def test_the_parts_are_read_oldest_first(tmp_path):
    store = _store(tmp_path)

    _session(
        tmp_path,
        _at(2026, 7, 26, 20, 0, 0),
        spoken=_at(2026, 7, 26, 21, 0, 0),
        summary="first",
    )
    _session(
        tmp_path,
        _at(2026, 7, 26, 21, 5, 0),
        spoken=_at(2026, 7, 26, 22, 0, 0),
        summary="second",
    )

    read = store.latest(SOURCE, GAP).read()

    assert read.index("first") < read.index("second")


def test_the_evening_is_read_with_the_day_it_was(tmp_path):
    """So a retelling can place the story rather than inferring a date."""
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 26, 20, 0, 0), summary=SUMMARY)

    assert "Sunday, 26 July 2026" in store.latest(SOURCE, GAP).read()


# ── an evening somebody named ─────────────────


def test_a_named_day_is_the_evening_that_started_on_it(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 12, 20, 0, 0), summary="the twelfth")
    _session(tmp_path, _at(2026, 7, 19, 20, 0, 0), summary="the nineteenth")

    found = store.find(SOURCE, When(target=_at(2026, 7, 12).date(), tolerance_days=EXACT_DAY), GAP)

    assert found is not None
    assert "the twelfth" in found.read()


def test_a_named_day_with_two_evenings_takes_the_later(tmp_path):
    """One date, two conversations, and "what happened on the twelfth" is the second."""
    store = _store(tmp_path)

    _session(
        tmp_path,
        _at(2026, 7, 12, 13, 0, 0),
        spoken=_at(2026, 7, 12, 14, 0, 0),
        summary="the afternoon one",
    )
    _session(
        tmp_path,
        _at(2026, 7, 12, 20, 0, 0),
        spoken=_at(2026, 7, 12, 22, 0, 0),
        summary="the evening one",
    )

    found = store.find(SOURCE, When(target=_at(2026, 7, 12).date(), tolerance_days=EXACT_DAY), GAP)

    assert found is not None
    assert found.parts == 1
    assert "the evening one" in found.read()


def test_an_evening_that_ran_past_midnight_keeps_the_rest_of_itself(tmp_path):
    """Asked for by the day it started, and it does not end there."""
    store = _store(tmp_path)

    _session(
        tmp_path,
        _at(2026, 7, 12, 23, 30, 0),
        spoken=_at(2026, 7, 12, 23, 55, 0),
        summary="before midnight",
    )
    _session(
        tmp_path,
        _at(2026, 7, 13, 0, 2, 0),
        spoken=_at(2026, 7, 13, 1, 0, 0),
        summary="after midnight",
    )

    found = store.find(SOURCE, When(target=_at(2026, 7, 12).date(), tolerance_days=EXACT_DAY), GAP)

    assert found is not None
    assert found.parts == 2
    assert "after midnight" in found.read()


def test_a_day_with_nothing_on_it_is_nothing(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 12, 20, 0, 0), summary="the twelfth")

    assert (
        store.find(
            SOURCE, When(target=_at(2026, 7, 15).date(), tolerance_days=EXACT_DAY), GAP
        )
        is None
    )


def test_counting_back_weeks_lands_on_the_nearest_evening(tmp_path):
    """
    A channel that meets on a night of the week does not meet on a date. Two
    weeks ago is the session two weeks ago, whichever day it moved to.
    """
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 10, 20, 0, 0), summary="three weeks back")
    _session(tmp_path, _at(2026, 7, 18, 20, 0, 0), summary="two weeks back")
    _session(tmp_path, _at(2026, 7, 24, 20, 0, 0), summary="last week")

    found = store.find(
        SOURCE, When(target=_at(2026, 7, 17).date(), tolerance_days=NEAREST_DAYS), GAP
    )

    assert found is not None
    assert "two weeks back" in found.read()


def test_a_tie_in_distance_goes_to_the_later_evening(tmp_path):
    """Somebody counting back is counting to a session they were more likely at."""
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 16, 20, 0, 0), summary="the day before")
    _session(tmp_path, _at(2026, 7, 18, 20, 0, 0), summary="the day after")

    found = store.find(
        SOURCE, When(target=_at(2026, 7, 17).date(), tolerance_days=NEAREST_DAYS), GAP
    )

    assert found is not None
    assert "the day after" in found.read()


def test_nothing_inside_the_window_is_nothing(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 1, 20, 0, 0), summary="a month before")

    assert (
        store.find(
            SOURCE, When(target=_at(2026, 7, 17).date(), tolerance_days=NEAREST_DAYS), GAP
        )
        is None
    )


def test_latest_is_find_with_no_date(tmp_path):
    store = _store(tmp_path)

    _session(tmp_path, _at(2026, 7, 26, 20, 0, 0), summary="the only one")

    assert store.find(SOURCE, LATEST, GAP).read() == store.latest(SOURCE, GAP).read()


# ── retention ─────────────────────────────────


def test_retention_drops_what_is_older_than_the_window(tmp_path):
    store = _store(tmp_path, retention_days=KEEP_A_WEEK)
    root = tmp_path / "transcripts"

    today = datetime.now().date()
    fresh = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    stale = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    store.write(_transcript(root, f"{fresh}T20-14-03"), "recent")
    store.write(_transcript(root, f"{stale}T20-14-03"), "ancient")

    removed = store.prune()

    assert [path.name for path in removed] == [f"{stale}T20-14-03.txt"]
    assert "recent" in store.latest(SOURCE, GAP).read()


def test_retention_off_keeps_everything(tmp_path):
    store = _store(tmp_path, retention_days=KEEP_FOREVER)
    root = tmp_path / "transcripts"

    stale = (datetime.now().date() - timedelta(days=3650)).strftime("%Y-%m-%d")
    store.write(_transcript(root, f"{stale}T20-14-03"), "ancient")

    assert store.prune() == []
    assert store.latest(SOURCE, GAP) is not None
