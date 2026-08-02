"""The tool that writes down what happened and reads it back when asked."""

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import miss_quote.llm.client as llm_module
import miss_quote.summary.store as store_module
import miss_quote.tools.summary as summary_module
from miss_quote.audio.hold import DEFAULT_HOLD_VOLUME
from miss_quote.config import SummaryConfig, TranscriptConfig, transcript_cfg
from miss_quote.llm.client import CompletionError
from miss_quote.tools.base import Tool, ToolContext, Toolbox
from miss_quote.tools.summary import Summary
from miss_quote.tools.tts import Tts
from miss_quote.transcript.writer import Source, Transcript, Utterance

SERVER = "first-server"

WATCHED = "General Voice"
WATCHED_KEY = "general-voice"
UNWATCHED = "side-room"

POSTING_CHANNEL = "session-summaries"

OPENED = datetime(2026, 7, 26, 20, 14, 3, tzinfo=timezone.utc)
CLOSED = datetime(2026, 7, 26, 22, 31, 55, tzinfo=timezone.utc)

SUMMARY = "They argued about the rules for an hour and nobody won."
RETELLING = "So there they were, arguing about the rules, and nobody won."

PREAMBLE = "Sure! Let me go look at my notes."
EMPTY = "I don't have any notes from this channel yet."
MISSING = "I don't have any notes from then."
CLOSING = "I wonder what'll happen tonight?"

# What a channel names to have something played under the wait, and how loud it
# asked for it. The clip itself is the chime library's business.
HOLD_MUSIC = "on-hold"
QUIETER = 0.4

ASKER = "Erik"
ENOUGH_UTTERANCES = 12

PATIENCE_SECONDS = 2.0

# The day every question in here is asked on. Late enough in a long month that
# an ordinal has somewhere to land in it, and a Friday, so counting back weeks
# lands on a weekday a channel plausibly meets on.
TODAY = date(2026, 7, 31)
ZONE = ZoneInfo(transcript_cfg.timezone)

WATCHED_SOURCE = Source(
    guild_id=1, guild_alias=SERVER, channel_id=10, channel=WATCHED
)
UNWATCHED_SOURCE = Source(
    guild_id=1, guild_alias=SERVER, channel_id=20, channel=UNWATCHED
)


class FakeTts(Tts):
    """
    The speaking tool, without the cache, the chimes or the voice connection.

    A real subclass because a tool finds its neighbours by class, and it skips
    `Tts.__init__` because everything that would do is talk to the filesystem.
    """

    def __init__(self, context: ToolContext) -> None:
        Tool.__init__(self, context)
        self.played: list[str] = []
        self.warmed: list[str] = []
        self.located: list[str | None] = []
        self.holds: list[tuple[str | None, float]] = []
        self.kept: dict[str, bool] = {}

    async def play(self, source, text, *, scale=1.0, chime=None, keep=True) -> None:
        self.played.append(text)
        self.kept[text] = keep

    async def play_held(
        self, source, words, *, hold=None, hold_volume=1.0, scale=1.0, keep=True
    ) -> None:
        self.holds.append((hold, hold_volume))
        await self.play(source, await words, scale=scale, keep=keep)

    def locate(self, chime) -> str | None:
        self.located.append(chime)
        return chime

    def enqueue(self, phrases) -> int:
        self.warmed.extend(phrases)
        return len(self.warmed)


class BlockingTts(FakeTts):
    """
    A speaking tool whose preamble will not finish until the model has started.

    This is the whole point of the recall, expressed as a deadlock: if the
    completion is started after the preamble rather than alongside it, the
    preamble waits for something that is waiting for it, and the test times out
    instead of quietly passing on a bot that sounds broken.
    """

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)
        self.thinking = asyncio.Event()

    async def play(self, source, text, *, scale=1.0, chime=None, keep=True) -> None:
        if text == PREAMBLE:
            await asyncio.wait_for(self.thinking.wait(), timeout=PATIENCE_SECONDS)

        self.played.append(text)
        self.kept[text] = keep


class FakeAnnouncer:
    """Somewhere to post, remembering what it was given."""

    def __init__(self, channels: tuple[str, ...] = (POSTING_CHANNEL,)) -> None:
        self.posts: list[tuple[str, str]] = []
        self._channels = channels

    def resolve(self, server: str, channel: str):
        return channel if channel in self._channels else None

    async def post(self, server: str, channel: str, text: str) -> bool:
        self.posts.append((channel, text))
        return True


@dataclass
class Session:
    """What the tool reads off a live session, which is where it came from."""

    source: Source


@pytest.fixture(autouse=True)
def summaries(tmp_path, monkeypatch):
    """
    Both trees under the test's own, and a fixed day to ask questions on.

    The store reads transcripts as well as summaries now — an evening filed in
    pieces is put back together from when each piece stopped being talked in,
    and that is only in the JSONL. A date somebody names is resolved against
    today, so today is pinned rather than left to the calendar the suite happens
    to run on.
    """
    monkeypatch.setattr(
        store_module, "summary_cfg", SummaryConfig(directory=tmp_path / "summaries")
    )
    monkeypatch.setattr(
        store_module,
        "transcript_cfg",
        TranscriptConfig(directory=tmp_path / "transcripts"),
    )
    monkeypatch.setattr(summary_module, "_today", lambda: TODAY)

    return tmp_path


@pytest.fixture
def model(monkeypatch):
    """A model that answers instantly, remembering what it was asked."""

    class Model:
        def __init__(self) -> None:
            self.asked: list[tuple[str, str]] = []
            self.answers = [SUMMARY]
            self.failure: Exception | None = None

        async def complete(self, instruction: str, text: str) -> str:
            self.asked.append((instruction, text))
            if self.failure is not None:
                raise self.failure

            return self.answers[min(len(self.asked), len(self.answers)) - 1]

    served = Model()
    monkeypatch.setattr(llm_module, "complete", served.complete)

    return served


def _tool(
    config: dict | None = None,
    announcer: FakeAnnouncer | None = None,
    speech=None,
    **channel,
) -> tuple[Summary, FakeTts]:
    """
    One server's summary tool, with a speaking tool beside it in the box.

    Keyword arguments are the watched channel's own settings, so a test that
    turns one thing up says only that thing.
    """
    toolbox = Toolbox()
    context = ToolContext(
        server=SERVER,
        config=config if config is not None else _config(**channel),
        tools=toolbox.view(Summary),
        announcer=announcer or FakeAnnouncer(),
    )

    talking = (speech or FakeTts)(context)
    toolbox.add(talking)

    return Summary(context), talking


def _config(**channel) -> dict:
    """A tool config watching one channel, on the given terms."""
    return {
        "monitored_channels": {
            WATCHED: {"channel": POSTING_CHANNEL, "preamble": PREAMBLE, "empty": EMPTY,
             "closing": CLOSING}
            | channel
        }
    }


def _silent_ending() -> dict:
    """A watched channel that never mentions `closing`, which is most of them."""
    return {
        "monitored_channels": {
            WATCHED: {"channel": POSTING_CHANNEL, "preamble": PREAMBLE, "empty": EMPTY}
        }
    }


def _transcript(root: Path, source: Source, lines: int = ENOUGH_UTTERANCES) -> Transcript:
    """A sealed session with something in it."""
    path = root / "transcripts" / source.relative_directory / "2026-07-26T20-14-03.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "ts": OPENED.isoformat(),
                    "user_id": 1,
                    "user": ASKER,
                    "text": f"line {number}",
                }
            )
            + "\n"
            for number in range(lines)
        ),
        encoding="utf-8",
    )

    return Transcript(
        path=path, source=source, opened=OPENED, closed=CLOSED, utterances=lines
    )


def _said(text: str) -> Utterance:
    return Utterance(timestamp=OPENED, user_id=1, user=ASKER, text=text)


def _filed(
    root: Path,
    opened: datetime,
    *,
    spoken: datetime,
    summary: str,
    source: Source = WATCHED_SOURCE,
) -> None:
    """
    One session already on disk, without going through the summarizing.

    Both halves, because an evening is put back together from when each of its
    pieces stopped being talked in and that only exists in the transcript.
    """
    stem = opened.strftime(transcript_cfg.filename_timestamp_format)
    line = {"ts": spoken.isoformat(), "user_id": 1, "user": ASKER, "text": "and so on"}

    transcript = root / "transcripts" / source.relative_directory / f"{stem}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")

    written = root / "summaries" / source.relative_directory / f"{stem}.txt"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(summary, encoding="utf-8")


def _evening(*parts: int) -> datetime:
    """A moment in the timezone the transcripts are named in."""
    return datetime(*parts, tzinfo=ZONE)


# ── the gate ──────────────────────────────────


async def test_a_channel_nobody_listed_is_not_summarized(summaries, model):
    tool, _ = _tool()

    await tool.handle_finished(_transcript(summaries, UNWATCHED_SOURCE))

    assert model.asked == []
    assert list((summaries / "summaries").rglob("*.txt")) == []


async def test_a_channel_nobody_listed_cannot_be_asked_either(summaries, model):
    tool, speech = _tool()

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(UNWATCHED_SOURCE)
    )

    assert speech.played == []
    assert model.asked == []


async def test_a_configured_name_matches_the_channel_through_slugify(summaries, model):
    """`General Voice` in the file is `general-voice` on disk and in Discord."""
    tool, _ = _tool()

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    stored = list((summaries / "summaries").rglob("*.txt"))

    assert [path.parent.name for path in stored] == [WATCHED_KEY]


# ── writing it down ───────────────────────────


async def test_a_finished_session_is_summarized_stored_and_posted(summaries, model):
    announcer = FakeAnnouncer()
    tool, _ = _tool(announcer=announcer)

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    stored = list((summaries / "summaries").rglob("*.txt"))
    assert [path.name for path in stored] == ["2026-07-26T20-14-03.txt"]
    assert stored[0].read_text(encoding="utf-8") == SUMMARY

    channel, posted = announcer.posts[0]
    assert channel == POSTING_CHANNEL
    assert SUMMARY in posted
    assert WATCHED in posted


async def test_the_model_is_given_a_speaker_and_text_script(summaries, model):
    tool, _ = _tool(minimum_utterances=1)

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE, lines=2))
    _, script = model.asked[0]

    assert f"{ASKER}: line 0 line 1" == script


async def test_a_session_too_short_to_be_one_is_left_alone(summaries, model):
    tool, _ = _tool(minimum_utterances=5)

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE, lines=4))

    assert model.asked == []
    assert list((summaries / "summaries").rglob("*.txt")) == []


async def test_a_model_failure_writes_nothing_and_posts_nothing(summaries, model):
    announcer = FakeAnnouncer()
    tool, _ = _tool(announcer=announcer)
    model.failure = CompletionError("the endpoint is down")

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    assert list((summaries / "summaries").rglob("*.txt")) == []
    assert announcer.posts == []


async def test_a_channel_with_nowhere_to_post_still_writes_the_summary(summaries, model):
    announcer = FakeAnnouncer()
    tool, _ = _tool(config={"monitored_channels": {WATCHED: {}}}, announcer=announcer)

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    assert list((summaries / "summaries").rglob("*.txt"))
    assert announcer.posts == []


# ── reading it back ───────────────────────────


async def test_the_model_is_asked_before_the_preamble_has_finished(
    summaries, model, monkeypatch
):
    """
    The whole reason the recall does not sound broken.

    `BlockingTts` will not let the preamble finish until the model has started,
    so a tool that waits for the announcement before asking anything deadlocks
    here rather than passing: the preamble is waiting for the completion and the
    completion is waiting for the preamble.
    """
    tool, speech = _tool(speech=BlockingTts)
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    async def thinking(instruction: str, text: str) -> str:
        speech.thinking.set()
        return RETELLING

    monkeypatch.setattr(llm_module, "complete", thinking)

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_the_retelling_is_the_most_recent_summary(summaries, model):
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]
    assert SUMMARY in model.asked[-1][1]


async def test_with_no_notes_it_says_so_and_asks_nothing(summaries, model):
    tool, speech = _tool()

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [EMPTY]
    assert model.asked == []


@pytest.mark.parametrize(
    "said",
    [
        "Miss Quote, what happened last session?",
        "misquote what happened last time",
        # What a transcriber actually returned the first time somebody asked:
        # the two words run together with both esses kept.
        "Missquote. What happened last session?",
        "mis quote, what happened last session",
        "Ms. Quote — recap the last session",
        "mizquote what happened last session",
        "hey miss quote, what did we do last session, out of interest",
    ],
)
async def test_the_spellings_an_asr_might_return_all_ask(summaries, model, said):
    tool, speech = _tool()
    model.answers = [RETELLING]

    await tool.handle_utterance(_said(said), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


@pytest.mark.parametrize(
    "said",
    [
        "what happened last session",
        "has anyone seen Miss Quote",
        "what happened last session, and where is miss quote",
        # A stem with something after it that is not a date. The stems are short
        # now that they no longer carry one, and this is what keeps them honest.
        "Miss Quote, what happened to my beer",
        "miss quote, recap the rules for me",
    ],
)
async def test_a_name_or_a_trigger_on_its_own_is_not_a_question(summaries, model, said):
    tool, speech = _tool()

    await tool.handle_utterance(_said(said), Session(WATCHED_SOURCE))

    assert speech.played == []


async def test_a_stem_on_its_own_asks_for_the_last_one(summaries, model):
    """"Miss Quote, what happened" is the same question with the clause left off."""
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


# ── one evening, several sessions ─────────────


async def test_an_evening_filed_in_halves_is_retold_as_one(summaries, model):
    """
    A room that empties and refills files the rest of the night separately, and
    it is one evening. The model is handed both halves and told nothing about
    there having been two.
    """
    tool, speech = _tool()
    model.answers = [RETELLING]

    _filed(
        summaries,
        _evening(2026, 7, 30, 19, 0, 0),
        spoken=_evening(2026, 7, 30, 21, 0, 0),
        summary="the first half",
    )
    _filed(
        summaries,
        _evening(2026, 7, 30, 21, 6, 0),
        spoken=_evening(2026, 7, 30, 23, 0, 0),
        summary="the second half",
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened last time?"), Session(WATCHED_SOURCE)
    )

    _, given = model.asked[-1]
    assert "the first half" in given
    assert "the second half" in given
    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_a_channel_can_set_how_long_a_break_is(summaries, model):
    """Six minutes is one evening at the default and two at one minute."""
    tool, speech = _tool(session_gap_minutes=1)
    model.answers = [RETELLING]

    _filed(
        summaries,
        _evening(2026, 7, 30, 19, 0, 0),
        spoken=_evening(2026, 7, 30, 21, 0, 0),
        summary="the first half",
    )
    _filed(
        summaries,
        _evening(2026, 7, 30, 21, 6, 0),
        spoken=_evening(2026, 7, 30, 23, 0, 0),
        summary="the second half",
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened last time?"), Session(WATCHED_SOURCE)
    )

    assert "the first half" not in model.asked[-1][1]


# ── an evening somebody named ─────────────────


async def test_a_named_day_is_retold(summaries, model):
    tool, speech = _tool()
    model.answers = [RETELLING]

    _filed(
        summaries,
        _evening(2026, 7, 12, 20, 0, 0),
        spoken=_evening(2026, 7, 12, 22, 0, 0),
        summary="the twelfth",
    )
    _filed(
        summaries,
        _evening(2026, 7, 26, 20, 0, 0),
        spoken=_evening(2026, 7, 26, 22, 0, 0),
        summary="the twenty sixth",
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened on the twelfth?"), Session(WATCHED_SOURCE)
    )

    assert "the twelfth" in model.asked[-1][1]
    assert "the twenty sixth" not in model.asked[-1][1]


async def test_counting_back_weeks_is_retold(summaries, model):
    tool, speech = _tool()
    model.answers = [RETELLING]

    _filed(
        summaries,
        _evening(2026, 7, 16, 20, 0, 0),
        spoken=_evening(2026, 7, 16, 22, 0, 0),
        summary="two weeks back",
    )
    _filed(
        summaries,
        _evening(2026, 7, 30, 20, 0, 0),
        spoken=_evening(2026, 7, 30, 22, 0, 0),
        summary="last night",
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened two weeks ago?"), Session(WATCHED_SOURCE)
    )

    assert "two weeks back" in model.asked[-1][1]


async def test_a_day_with_no_notes_says_so_rather_than_saying_there_are_none(
    summaries, model
):
    """
    Two different answers. One says the bot has never written anything down
    here, and the other says it was not listening that night; a channel told the
    first when the second is true goes looking for a fault that is not there.
    """
    tool, speech = _tool()

    _filed(
        summaries,
        _evening(2026, 7, 26, 20, 0, 0),
        spoken=_evening(2026, 7, 26, 22, 0, 0),
        summary=SUMMARY,
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened on the second?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [MISSING]
    assert model.asked == []


async def test_it_is_not_told_twice_inside_the_backoff(summaries, model):
    tool, speech = _tool(backoff_seconds=300)
    model.answers = [SUMMARY, RETELLING, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    asked = _said("Miss Quote, what happened last session?")
    await tool.handle_utterance(asked, Session(WATCHED_SOURCE))
    await tool.handle_utterance(asked, Session(WATCHED_SOURCE))

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_a_different_evening_inside_the_backoff_is_still_answered(summaries, model):
    """
    The window holds off one story, not one channel. Somebody asking about last
    Thursday is asking a second question, and it has a different answer.
    """
    tool, speech = _tool(backoff_seconds=300)
    model.answers = [RETELLING]

    _filed(
        summaries,
        _evening(2026, 7, 12, 20, 0, 0),
        spoken=_evening(2026, 7, 12, 22, 0, 0),
        summary="the twelfth",
    )
    _filed(
        summaries,
        _evening(2026, 7, 30, 20, 0, 0),
        spoken=_evening(2026, 7, 30, 22, 0, 0),
        summary="last night",
    )

    await tool.handle_utterance(
        _said("Miss Quote, what happened last time?"), Session(WATCHED_SOURCE)
    )
    await tool.handle_utterance(
        _said("Miss Quote, what happened on the twelfth?"), Session(WATCHED_SOURCE)
    )

    assert speech.played.count(RETELLING) == 2
    assert "the twelfth" in model.asked[-1][1]


async def test_a_second_ask_mid_retelling_is_dropped(summaries, model):
    """What is queued behind a minute of narration is a minute of the same."""
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    asked = _said("Miss Quote, what happened last session?")
    await asyncio.gather(
        tool.handle_utterance(asked, Session(WATCHED_SOURCE)),
        tool.handle_utterance(asked, Session(WATCHED_SOURCE)),
    )

    assert speech.played.count(RETELLING) == 1


async def test_a_model_failure_mid_recall_says_nothing_more(summaries, model):
    tool, speech = _tool()
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    model.failure = CompletionError("the endpoint is down")

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE]


# ── configuration ─────────────────────────────


async def test_two_channels_each_get_their_own_prompt(summaries, model):
    tool, _ = _tool(
        config={
            "prompts": {"terse": "Three sentences."},
            "monitored_channels": {
                WATCHED: {"prompt": "terse"},
                UNWATCHED: {"prompt": "minutes"},
            },
        }
    )

    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    await tool.handle_finished(_transcript(summaries, UNWATCHED_SOURCE))

    assert model.asked[0][0] == "Three sentences."
    assert "minutes" in model.asked[1][0].lower()


def test_a_prompt_nothing_answers_to_stops_the_tool_from_starting():
    with pytest.raises(ValueError, match="no prompt named"):
        _tool(config={"monitored_channels": {WATCHED: {"prompt": "nonexistent"}}})


def test_a_setting_written_where_nothing_reads_it_stops_the_tool(summaries):
    with pytest.raises(ValueError, match="nothing reads"):
        _tool(config={"monitored_channels": {WATCHED: {"prmopt": "recap"}}})


def test_a_tool_with_no_channels_still_builds(summaries):
    tool, _ = _tool(config={})

    assert tool._monitored == {}


async def test_a_tool_with_no_channels_says_so(summaries, caplog):
    tool, _ = _tool(config={})

    await tool.prewarm()

    assert "monitored_channels" in caplog.text


# ── the ending ────────────────────────────────


async def test_a_channel_that_asked_for_no_closing_ends_on_the_story(summaries, model):
    """
    The ordinary case. The retelling prompt ends the story itself, and a fixed
    sentence after one that has just said goodbye is one goodbye too many.
    """
    tool, speech = _tool(config=_silent_ending())
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING]


async def test_a_closing_nobody_asked_for_is_not_rendered_either(summaries, model):
    """An empty phrase is a synthesizer round trip for silence."""
    tool, speech = _tool(config=_silent_ending())

    await tool.prewarm()

    assert sorted(speech.warmed) == sorted([PREAMBLE, EMPTY, MISSING])


async def test_a_retelling_is_followed_by_a_fixed_closing(summaries, model):
    """
    For a server that would rather hear the same sentence every time than trust
    the prompt to end the story.
    """
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_the_closing_is_rendered_in_advance_with_the_rest(summaries, model):
    tool, speech = _tool()

    await tool.prewarm()

    assert sorted(speech.warmed) == sorted([PREAMBLE, EMPTY, MISSING, CLOSING])


async def test_nothing_to_tell_gets_no_closing(summaries, model):
    """There is no story to have finished, so saying so would be a non sequitur."""
    tool, speech = _tool()

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [EMPTY]


async def test_the_retelling_is_not_kept_but_the_fixed_lines_are(summaries, model):
    """
    The cache is for phrases that come round again. An account of one evening
    is a large file nothing will ever ask for twice.
    """
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.kept == {PREAMBLE: True, RETELLING: False, CLOSING: True}


# ── the music over the wait ───────────────────────


async def test_a_channel_holds_with_the_clip_it_named(summaries, model):
    tool, speech = _tool(hold_music=HOLD_MUSIC, hold_volume=QUIETER)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.holds == [(HOLD_MUSIC, QUIETER)]
    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_a_channel_that_named_nothing_waits_the_way_it_always_did(
    summaries, model
):
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.holds == [(None, DEFAULT_HOLD_VOLUME)]


async def test_a_hold_clip_is_looked_for_before_anybody_asks(summaries, model):
    """
    A name that is not in the directory should be a line on the way up rather
    than a discovery made the first time somebody asks a question.
    """
    tool, speech = _tool(hold_music=HOLD_MUSIC)

    await tool.prewarm()

    assert speech.located == [HOLD_MUSIC]


async def test_music_nobody_named_is_not_looked_for(summaries, model):
    tool, speech = _tool()

    await tool.prewarm()

    assert speech.located == [None]


@pytest.mark.parametrize(
    ("wanted", "played"),
    [(2.0, 1.0), (-1.0, 0.0), (0.4, 0.4)],
)
async def test_music_is_clamped_to_the_channels_own_loudness(
    summaries, model, wanted, played
):
    """
    Either side of the range means the same as its nearest end, so there is
    nothing to tell somebody that they will not hear for themselves.
    """
    tool, speech = _tool(hold_music=HOLD_MUSIC, hold_volume=wanted)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )

    assert speech.holds == [(HOLD_MUSIC, played)]


async def test_music_that_is_not_a_number_stops_the_tool_from_starting(summaries):
    with pytest.raises(ValueError, match="hold_volume"):
        _tool(hold_music=HOLD_MUSIC, hold_volume="loud")
