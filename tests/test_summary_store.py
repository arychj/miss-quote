import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miss_quote.summary.store import SummaryStore
from miss_quote.transcript.writer import Source, Transcript

KEEP_FOREVER = -1
KEEP_A_WEEK = 7

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
    return SummaryStore(directory=tmp_path / "summaries", retention_days=retention_days)


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


def test_latest_is_the_newest_session_in_that_channel(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / "transcripts"

    store.write(_transcript(root, "2026-07-26T20-14-03"), "the older one")
    store.write(_transcript(root, "2026-07-27T09-31-55"), "the newer one")

    stored = store.latest(SOURCE)

    assert stored is not None
    assert stored.text == "the newer one"
    assert stored.session == "2026-07-27T09-31-55"


def test_latest_reads_the_filename_rather_than_the_mtime(tmp_path):
    """
    The name is when the session was; the mtime is when the file happened to be
    written, which differs the moment anything is regenerated or restored.
    """
    store = _store(tmp_path)
    root = tmp_path / "transcripts"

    store.write(_transcript(root, "2026-07-26T20-14-03"), "the older one")
    newer = store.write(_transcript(root, "2026-07-27T09-31-55"), "the newer one")

    older = store.path_for(_transcript(root, "2026-07-26T20-14-03"))
    touched = newer.stat().st_mtime + 60
    os.utime(older, (touched, touched))

    stored = store.latest(SOURCE)

    assert stored is not None
    assert stored.text == "the newer one"


def test_channels_do_not_see_each_others_summaries(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / "transcripts"

    store.write(_transcript(root, "2026-07-26T20-14-03"), "in general")

    assert store.latest(OTHER_CHANNEL) is None


def test_a_channel_with_no_summaries_has_no_latest(tmp_path):
    assert _store(tmp_path).latest(SOURCE) is None


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
    assert store.latest(SOURCE).text == "recent"


def test_retention_off_keeps_everything(tmp_path):
    store = _store(tmp_path, retention_days=KEEP_FOREVER)
    root = tmp_path / "transcripts"

    stale = (datetime.now().date() - timedelta(days=3650)).strftime("%Y-%m-%d")
    store.write(_transcript(root, f"{stale}T20-14-03"), "ancient")

    assert store.prune() == []
    assert store.latest(SOURCE) is not None


def test_an_unwritable_directory_costs_the_summary_and_not_the_process(tmp_path, monkeypatch):
    store = _store(tmp_path)
    transcript = _transcript(tmp_path / "transcripts", "2026-07-26T20-14-03")

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)

    assert store.write(transcript, SUMMARY) is None
