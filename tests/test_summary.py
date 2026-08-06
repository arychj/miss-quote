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
from miss_quote.tools.summary import (
    DEFAULT_ADDRESS_WINDOW_SECONDS,
    ELLIPSIS,
    MINIMUM_TRANSCRIPT_REFRESH_SECONDS,
    TRANSCRIPT_FENCE,
    TRANSCRIPT_LINE_LIMIT,
    Summary,
)
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
ASKER_ID = 1

# Somebody else in the room, for the half of a question that is not theirs.
OTHER_ASKER = "Someone Else"
OTHER_ASKER_ID = 2
ENOUGH_UTTERANCES = 12

PATIENCE_SECONDS = 2.0

# How long a question that named no evening waits for one, in here. Short, so
# the tests that never send a clause spend no real time timing out — and real,
# rather than zero, so they still go through the waiting. A test that does send
# one asks for a long window instead: the wait ends when the clause lands, so
# the number only ever costs anything when nothing arrives.
CLAUSE_WINDOW = 0.05
PATIENT_CLAUSE_WINDOW = 5.0

# Turns of the loop a test gives an ask to reach the point of waiting. Generous,
# because what it is waiting on is a lookup and a task start rather than a clock.
PARKING_ATTEMPTS = 100

# What the model is asked for over one sealed session and one retelling of it.
SUMMARIZED_AND_RETOLD = 2

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


class FakeTicker:
    """
    Somewhere to keep one message, remembering every version of it.

    A list rather than the last one, because what the feed is judged on is how
    often it wrote as much as what it wrote: a tool that rewrote an unchanged
    block every couple of seconds would look identical from the final state.
    """

    def __init__(self, refusing: bool = False) -> None:
        self.shown: list[tuple[str, str]] = []
        self._refusing = refusing

    async def show(self, server: str, channel: str, text: str) -> bool:
        self.shown.append((channel, text))

        return not self._refusing


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
    ticker: FakeTicker | None = None,
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
        ticker=ticker or FakeTicker(),
    )

    talking = (speech or FakeTts)(context)
    toolbox.add(talking)

    return Summary(context), talking


def _config(**channel) -> dict:
    """A tool config watching one channel, on the given terms."""
    return {
        "monitored_channels": {
            WATCHED: {"channel": POSTING_CHANNEL, "preamble": PREAMBLE, "empty": EMPTY,
             "closing": CLOSING, "clause_window_seconds": CLAUSE_WINDOW}
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


def _said(text: str, user: str = ASKER, user_id: int = ASKER_ID) -> Utterance:
    return Utterance(timestamp=OPENED, user_id=user_id, user=user, text=text)


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
        # The name on its own gives the transcriber nothing either side to weigh
        # it against, so it reaches for the nearest real word.
        "Mrs. Quote, what happened last session",
        "misquotes what happened last session",
        "Misquoted. What happened last session?",
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


# ── a question in two utterances ──────────────


def _aged(tool: Summary, seconds: float, user_id: int = ASKER_ID) -> None:
    """
    Push a held name back in time, so a window can be walked past without waiting.

    The stored moment is monotonic, so this moves it rather than the clock: a
    test that slept out the real window would be a test of `asyncio.sleep`.
    """
    held = (WATCHED_KEY, user_id)
    tool._addressed[held] = tool._addressed[held] - seconds


async def test_the_name_and_the_question_in_two_utterances_still_ask(summaries, model):
    """What an ASR returns is utterances, and it splits wherever somebody paused."""
    tool, speech = _tool()

    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(
        _said("What happened on the twenty ninth?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [MISSING]


async def test_a_bare_stem_after_the_name_asks_for_the_last_one(summaries, model):
    tool, speech = _tool()

    await tool.handle_utterance(_said("Miss Quote?"), Session(WATCHED_SOURCE))
    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


async def test_the_two_halves_have_to_come_from_one_speaker(summaries, model):
    """Somebody else's question is a different question, and was not addressed."""
    tool, speech = _tool()

    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(
        _said("What happened?", OTHER_ASKER, OTHER_ASKER_ID), Session(WATCHED_SOURCE)
    )

    assert speech.played == []


async def test_something_in_between_does_not_break_the_pair(summaries, model):
    """A held name is spent by a question rather than by the next thing said."""
    tool, speech = _tool()

    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(_said("Uh."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


async def test_the_name_is_forgotten_once_the_window_has_passed(summaries, model):
    tool, speech = _tool()
    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    _aged(tool, DEFAULT_ADDRESS_WINDOW_SECONDS + 1)

    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == []


async def test_the_name_is_still_held_inside_the_window(summaries, model):
    tool, speech = _tool()
    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    _aged(tool, DEFAULT_ADDRESS_WINDOW_SECONDS - 1)

    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


async def test_a_held_name_is_spent_by_the_question_it_asks(summaries, model):
    """Otherwise one "Miss Quote" makes every "what happened" after it a question."""
    tool, speech = _tool()
    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


async def test_a_continuation_that_is_not_a_question_asks_nothing(summaries, model):
    """The clause after the stem still has to be one `summary.when` can read."""
    tool, speech = _tool()

    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(
        _said("What happened to my beer?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == []


async def test_a_channel_can_set_how_long_the_name_is_held(summaries, model):
    tool, speech = _tool(address_window_seconds=DEFAULT_ADDRESS_WINDOW_SECONDS * 2)
    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    _aged(tool, DEFAULT_ADDRESS_WINDOW_SECONDS + 1)

    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == [EMPTY]


async def test_a_window_of_zero_wants_the_whole_question_in_one_breath(
    summaries, model
):
    tool, speech = _tool(address_window_seconds=0)

    await tool.handle_utterance(_said("Miss Quote."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(_said("What happened?"), Session(WATCHED_SOURCE))

    assert speech.played == []


async def test_the_whole_question_in_one_breath_never_holds_a_name(summaries, model):
    """The ordinary ask does not go through the memory at all."""
    tool, speech = _tool()

    await tool.handle_utterance(
        _said("Miss Quote, what happened?"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [EMPTY]
    assert tool._addressed == {}


def test_a_window_that_is_not_a_number_will_not_start(summaries):
    with pytest.raises(ValueError, match="address_window_seconds"):
        _tool(address_window_seconds="fifteen")


# ── a clause in its own breath ────────────────


async def _asking(tool: Summary, said: str) -> asyncio.Task:
    """
    Put a question to the tool and hand back the ask still in flight.

    A question that named no evening parks until a clause arrives or the window
    runs out, so a test that wants to send the clause has to let go of the first
    one first.
    """
    asking = asyncio.create_task(
        tool.handle_utterance(_said(said), Session(WATCHED_SOURCE))
    )
    await _parked(tool)

    return asking


async def _parked(tool: Summary) -> None:
    """Wait until the ask is actually waiting, rather than about to."""
    for _ in range(PARKING_ATTEMPTS):
        if tool._awaiting:
            return
        await asyncio.sleep(0)

    raise AssertionError("the question never waited for a clause")


async def test_a_clause_in_a_second_breath_names_the_evening(summaries, model):
    """
    The failure this exists for: "Miss Quote, what happened" is a whole question
    on its own, so answering it as it lands retells the wrong night.
    """
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    asking = await _asking(tool, "Miss Quote, what happened")

    await tool.handle_utterance(
        _said("On the twenty ninth?"), Session(WATCHED_SOURCE)
    )
    await asking

    # The evening on disk is the 26th, so the 29th has no notes — which is the
    # answer that proves the date was heard at all.
    assert speech.played == [PREAMBLE, MISSING]


async def test_the_clause_is_waited_for_beside_the_preamble(summaries, model):
    """Not in front of it: the wait is free precisely because it is covered."""
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    asking = await _asking(tool, "Miss Quote, what happened")

    assert speech.played == [PREAMBLE]
    await tool.handle_utterance(_said("Last session."), Session(WATCHED_SOURCE))
    await asking


async def test_a_clause_naming_the_same_evening_changes_nothing(summaries, model):
    """"Last session" is what was already assumed, not a second thing to look up."""
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    asking = await _asking(tool, "Miss Quote, what happened")

    await tool.handle_utterance(_said("Last session."), Session(WATCHED_SOURCE))
    await asking

    # Once to summarize the session and once to retell it. A third would be the
    # completion having been thrown away and started again for the same evening.
    assert speech.played == [PREAMBLE, RETELLING, CLOSING]
    assert len(model.asked) == SUMMARIZED_AND_RETOLD


async def test_nothing_more_said_answers_the_evening_it_assumed(summaries, model):
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_a_question_that_named_its_evening_never_waits(summaries, model):
    """Anything spelled out is finished, and is answered as fast as it always was."""
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]
    assert tool._awaiting == {}


async def test_only_the_asker_can_name_the_evening(summaries, model):
    """Somebody else saying "last week" is talking to the room."""
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    asking = await _asking(tool, "Miss Quote, what happened")

    await tool.handle_utterance(
        _said("On the twenty ninth?", OTHER_ASKER, OTHER_ASKER_ID),
        Session(WATCHED_SOURCE),
    )
    await asking

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_something_that_is_not_a_clause_does_not_redirect_it(summaries, model):
    tool, speech = _tool()
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    asking = await _asking(tool, "Miss Quote, what happened")

    await tool.handle_utterance(_said("So anyway."), Session(WATCHED_SOURCE))
    await asking

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]


async def test_a_clause_is_not_dropped_by_the_retelling_gate(summaries, model):
    """
    The ask it belongs to is holding that lock while it waits, so a clause
    checked against the gate first would be dropped by the question waiting on it.
    """
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))
    asking = await _asking(tool, "Miss Quote, what happened")

    assert tool._telling.locked()
    await tool.handle_utterance(
        _said("On the twenty ninth?"), Session(WATCHED_SOURCE)
    )
    await asking

    assert speech.played == [PREAMBLE, MISSING]


async def test_a_window_of_zero_answers_the_moment_it_is_asked(summaries, model):
    tool, speech = _tool(clause_window_seconds=0)
    model.answers = [SUMMARY, RETELLING]
    await tool.handle_finished(_transcript(summaries, WATCHED_SOURCE))

    await tool.handle_utterance(
        _said("Miss Quote, what happened"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [PREAMBLE, RETELLING, CLOSING]
    assert tool._awaiting == {}


async def test_an_evening_with_nothing_in_it_never_waits(summaries, model):
    """No clause can change a room that has never been written about."""
    tool, speech = _tool(clause_window_seconds=PATIENT_CLAUSE_WINDOW)

    await tool.handle_utterance(
        _said("Miss Quote, what happened"), Session(WATCHED_SOURCE)
    )

    assert speech.played == [EMPTY]


async def test_a_clause_window_that_is_not_a_number_will_not_start(summaries):
    with pytest.raises(ValueError, match="clause_window_seconds"):
        _tool(clause_window_seconds="a moment")


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


# ── showing it as it is said ──────────────────


async def _refreshed(tool: Summary) -> None:
    """
    Write the watched room's feed once, if it has anything new to write.

    What the service loop does between two sleeps, called directly: a test about
    what the message says should not also be a test of `asyncio.sleep`.
    """
    await tool._refresh(tool._monitored[WATCHED_KEY])


def _watching(**channel) -> dict:
    """A watched channel that shows its transcript as it is said."""
    return _config(post_transcripts=True, **channel)


def _block(ticker: FakeTicker) -> str:
    """What the message says after the most recent write."""
    return ticker.shown[-1][1]


def _lines(shown: str) -> list[str]:
    """The lines of a shown block, with the fence taken off either end."""
    return [line for line in shown.strip().splitlines() if line != TRANSCRIPT_FENCE]


async def test_a_room_shows_nothing_unless_it_asks(summaries):
    """Off by default: the same words in a text channel are a different artifact."""
    ticker = FakeTicker()
    tool, _ = _tool(ticker=ticker)

    await tool.handle_utterance(_said("Anything at all."), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    assert ticker.shown == []


async def test_a_watched_room_shows_what_was_said(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)

    await tool.handle_utterance(_said("We open the door."), Session(WATCHED_SOURCE))
    await tool.handle_utterance(
        _said("There is nothing behind it.", OTHER_ASKER, OTHER_ASKER_ID),
        Session(WATCHED_SOURCE),
    )
    await _refreshed(tool)

    channel, shown = ticker.shown[0]

    assert channel == POSTING_CHANNEL
    assert _lines(shown) == [
        f"{ASKER}: We open the door.",
        f"{OTHER_ASKER}: There is nothing behind it.",
    ]


async def test_a_room_that_has_said_nothing_more_is_not_written_again(summaries):
    """On change, not on a tick — the whole reason this fits inside the limit."""
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)
    await tool.handle_utterance(_said("Once."), Session(WATCHED_SOURCE))

    await _refreshed(tool)
    await _refreshed(tool)

    assert len(ticker.shown) == 1


async def test_something_new_is_written(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)
    await tool.handle_utterance(_said("First."), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    await tool.handle_utterance(_said("Second."), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    assert len(ticker.shown) == 2
    assert _lines(_block(ticker)) == [f"{ASKER}: First.", f"{ASKER}: Second."]


async def test_only_the_last_lines_are_shown(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(transcript_lines=2), ticker=ticker)

    for said in ("One.", "Two.", "Three."):
        await tool.handle_utterance(_said(said), Session(WATCHED_SOURCE))

    await _refreshed(tool)

    assert _lines(_block(ticker)) == [f"{ASKER}: Two.", f"{ASKER}: Three."]


async def test_a_question_to_the_bot_is_still_a_line(summaries, model):
    """The room is watching what was said, not what the tool decided it meant."""
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)

    await tool.handle_utterance(
        _said("Miss Quote, what happened last session?"), Session(WATCHED_SOURCE)
    )
    await _refreshed(tool)

    assert "what happened last session" in _block(ticker)


async def test_an_unwatched_room_is_not_shown(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)

    await tool.handle_utterance(_said("Elsewhere."), Session(UNWATCHED_SOURCE))
    await _refreshed(tool)

    assert ticker.shown == []


async def test_one_long_line_cannot_clear_the_rest_off(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)

    await tool.handle_utterance(_said("word " * 200), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    said = _lines(_block(ticker))[0]

    assert said.endswith(ELLIPSIS)
    assert len(said) <= TRANSCRIPT_LINE_LIMIT + len(f"{ASKER}: ")


async def test_a_backtick_cannot_break_the_fence(summaries):
    """A fence is what stops a transcript from formatting the channel."""
    ticker = FakeTicker()
    tool, _ = _tool(config=_watching(), ticker=ticker)

    await tool.handle_utterance(
        _said("```js\nnot really code"), Session(WATCHED_SOURCE)
    )
    await _refreshed(tool)

    assert _lines(_block(ticker)) == [f"{ASKER}: js not really code"]


async def test_a_refused_write_is_tried_again(summaries):
    """Nothing is recorded as shown that was not shown."""
    ticker = FakeTicker(refusing=True)
    tool, _ = _tool(config=_watching(), ticker=ticker)
    await tool.handle_utterance(_said("Once."), Session(WATCHED_SOURCE))

    await _refreshed(tool)
    await _refreshed(tool)

    assert len(ticker.shown) == 2


async def test_a_room_with_nowhere_to_post_shows_nothing(summaries):
    """The feed goes where the summary goes; a room that named nowhere has neither."""
    ticker = FakeTicker()
    tool, _ = _tool(
        config={
            "monitored_channels": {
                WATCHED: {"preamble": PREAMBLE, "post_transcripts": True}
            }
        },
        ticker=ticker,
    )

    await tool.handle_utterance(_said("Anything."), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    assert ticker.shown == []


async def test_an_interval_of_nothing_is_off(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(
        config=_watching(transcript_refresh_seconds=0), ticker=ticker
    )

    await tool.handle_utterance(_said("Anything."), Session(WATCHED_SOURCE))
    await _refreshed(tool)

    assert ticker.shown == []


def test_an_interval_faster_than_discord_is_held_at_the_floor(summaries):
    """A twentieth of a second is not a faster feed, it is one running behind."""
    tool, _ = _tool(config=_watching(transcript_refresh_seconds=0.05))

    assert (
        tool._monitored[WATCHED_KEY].transcript_refresh_seconds
        == MINIMUM_TRANSCRIPT_REFRESH_SECONDS
    )


def test_an_interval_that_is_not_a_number_stops_the_tool_from_starting(summaries):
    with pytest.raises(ValueError, match="transcript_refresh_seconds"):
        _tool(config=_watching(transcript_refresh_seconds="often"))


async def test_a_server_showing_nothing_has_nothing_to_run(summaries):
    """Which the runner treats as a service deciding it has nothing to do."""
    tool, _ = _tool()

    await asyncio.wait_for(tool.run(), timeout=PATIENCE_SECONDS)


async def test_the_service_writes_what_the_room_says(summaries):
    ticker = FakeTicker()
    tool, _ = _tool(
        config=_watching(transcript_refresh_seconds=MINIMUM_TRANSCRIPT_REFRESH_SECONDS),
        ticker=ticker,
    )
    running = asyncio.create_task(tool.run())

    try:
        await tool.handle_utterance(_said("Live."), Session(WATCHED_SOURCE))

        for _ in range(PARKING_ATTEMPTS):
            if ticker.shown:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("the feed never wrote anything")
    finally:
        running.cancel()

    assert _lines(_block(ticker)) == [f"{ASKER}: Live."]
