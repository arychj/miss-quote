"""What sets a quote off, what comes back, how long the line stays spent, and who is paid for placing it."""

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from miss_quote.config import (
    BUNDLED_QUOTES,
    UNITY_VOLUME,
    ServerConfig,
    ToolSettings,
    quotes_cfg,
    scoreboard_cfg,
)
from miss_quote.ledger.credits import CreditLedger
from miss_quote.tools.base import ToolContext, Toolbox
from miss_quote.tools.quotes import (
    ANNOUNCEMENT_KEY,
    ANSWER_SECONDS_KEY,
    DEFAULT_ANNOUNCEMENT,
    DEFAULT_REMARKS,
    DEFAULT_SELF_ANSWER_ANNOUNCEMENT,
    DEFAULT_SELF_ANSWER_PENALTY,
    DEFAULT_TIE_ANNOUNCEMENT,
    FIRST_ROW,
    PENALIZE_SELF_ANSWERS_KEY,
    REMARKS_KEY,
    SELF_ANSWER_ANNOUNCEMENT_KEY,
    SELF_ANSWER_PENALTY_KEY,
    TIE_ANNOUNCEMENT_KEY,
    TIE_SECONDS_KEY,
    Quote,
    Quotes,
    RecentQuotes,
    Round,
    _denominated,
    _load,
)
from miss_quote.tools.runner import ToolRunner
from miss_quote.tools.scoreboard import Scoreboard
from miss_quote.transcript.writer import Source, Utterance

SERVER_ALIAS = "first-server"
SPEAKER = "Speaker One"
OTHER_SPEAKER = "Speaker Two"

SPEAKER_ID = 234567890123456789
OTHER_SPEAKER_ID = 345678901234567890
ROSTER = {SPEAKER_ID: SPEAKER, OTHER_SPEAKER_ID: OTHER_SPEAKER}

# Whoever sets a line off, kept apart from the two who answer it: they are
# barred from their own round, so a test about winning one should not have to
# think about who spoke first.
ASKER = "Speaker Three"
ASKER_ID = 456789012345678901

SOURCE = Source(
    guild_id=1, guild_alias=SERVER_ALIAS, channel_id=2, channel="general-voice"
)

MOVIE = "Firefly"
TRIGGER = "cool"
QUOTE = "Shiny."

OTHER_MOVIE = "The Princess Bride"
OTHER_TRIGGER = "impossible"
OTHER_QUOTE = "Inconceivable!"

# A trigger that is a phrase rather than a word, and one that contains a shorter
# trigger, which is the pair the ordering of the pattern turns on.
GENERAL_TRIGGER = "monday"
GENERAL_QUOTE = "Sounds like someone has a case of the Mondays."
SPECIFIC_TRIGGER = "case of the monday"
SPECIFIC_QUOTE = "No. No man."

# A line with a comma in it, which the file has to quote and the reader unquote.
COMMA_TRIGGER = "give up"
COMMA_QUOTE = "Never give up, never surrender!"

PERSONAL_TRIGGER = "question"
PERSONAL_QUOTE = "{user} question is dumb."

HEADER = "movie,trigger,quote"
ROWS = (
    f"{MOVIE},{TRIGGER},{QUOTE}",
    f"{OTHER_MOVIE},{OTHER_TRIGGER},{OTHER_QUOTE}",
)

# Taken from the config rather than rebuilt here, so a moved file is one edit.
BUNDLED = BUNDLED_QUOTES

# A window of its own, so a test about the backoff is not also a test of what the
# deployment set it to.
BACKOFF = 300.0
NO_BACKOFF = 0.0
SHORT_WINDOW = 30.0

# The round, on the same terms: fixed here rather than read from whatever the
# defaults happen to be.
ANSWER_WINDOW = 5.0
TIE_WINDOW = 1.0
NO_WINDOW = 0.0

# Naming the film, the way the game show does and the several ways a channel
# actually says it.
ANSWER = f"What is {MOVIE}"
CONTRACTED_ANSWER = f"What's {MOVIE}?"
WRONG_ANSWER = f"What is {OTHER_MOVIE}"

# A title with a leading article, which an answer may leave off, and one with an
# abbreviation a channel says as a word.
ARTICLE_MOVIE = "The Matrix"
VERSUS_MOVIE = "Tucker and Dale vs Evil"

# Which ending the tests see, settled by `settled` below so an announcement is a
# fixed string rather than one of several.
REMARK = DEFAULT_REMARKS[0]
ADDED_REMARK = "having watched it more recently than is respectable."

ONE_CREDIT = 1
TWO_CREDITS = 2
NOTHING = 0

LEDGER_NAME = "credits.json"

NOW = 1_000.0


class RecordingSpeaker:
    """A speaker that keeps what it was asked to say instead of playing it."""

    def __init__(self) -> None:
        self.played: list[tuple[Source, str]] = []
        self.scales: list[float] = []

    async def play(self, source, audio, scale: float = UNITY_VOLUME) -> None:
        spoken = "".join([chunk async for chunk in audio])
        self.played.append((source, spoken))
        self.scales.append(scale)


class FakeSpeech:
    """Stands in for the cache, handing back the text it was asked to render."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.warmed: list[str] = []
        self.held: set[str] = set()

    async def stream(self, text: str):
        self.asked.append(text)
        yield text

    async def warm(self, text: str) -> bool:
        self.warmed.append(text)

        if text in self.held:
            return False

        self.held.add(text)
        return True


class BlockingSpeaker(RecordingSpeaker):
    """A speaker that holds the channel open until it is let go of."""

    def __init__(self) -> None:
        super().__init__()
        self.playing = asyncio.Event()
        self.finish = asyncio.Event()

    async def play(self, source, audio, scale: float = UNITY_VOLUME) -> None:
        self.playing.set()
        await self.finish.wait()
        await super().play(source, audio, scale)


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


@pytest.fixture
def speech(monkeypatch) -> FakeSpeech:
    """Replace the process-wide cache so nothing reaches a synthesizer."""
    fake = FakeSpeech()
    monkeypatch.setattr("miss_quote.tools.quotes.shared_cache", lambda: fake)
    return fake


@pytest.fixture
def speaker() -> RecordingSpeaker:
    return RecordingSpeaker()


@pytest.fixture
def quotes_file(monkeypatch, tmp_path) -> Path:
    """A file of two quotes, in place of whatever the deployment ships."""
    return _written(monkeypatch, tmp_path, HEADER, *ROWS)


def _written(monkeypatch, directory: Path, *lines: str) -> Path:
    """A quotes file the tool will read, whatever it holds."""
    path = directory / "quotes.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _pointed_at(monkeypatch, path)

    return path


def _pointed_at(monkeypatch, path: Path) -> None:
    """
    Aim the tool at a file of this test's own.

    The environment is read at import, so the settings object is replaced rather
    than the variable behind it.
    """
    monkeypatch.setattr("miss_quote.tools.quotes.quotes_cfg", replace(quotes_cfg, file=path))


@pytest.fixture(autouse=True)
def settled(monkeypatch) -> None:
    """
    Pin which ending an announcement takes, so it is a string a test can name.

    Autouse because every test that hears an award wants it settled, and the one
    about the drawing does its own arranging.
    """
    monkeypatch.setattr(
        "miss_quote.tools.quotes._chosen", lambda remarks: remarks[0]
    )


def _drawn(monkeypatch, last: bool = False) -> None:
    """
    Settle which of several the tool draws, overriding the autouse `settled`.

    For the two things it leaves to chance — the ending an announcement takes,
    and the answer a repeated trigger gives — where a test is about the drawing
    rather than about what was drawn.
    """
    monkeypatch.setattr(
        "miss_quote.tools.quotes._chosen", lambda options: options[-1 if last else 0]
    )


@pytest.fixture
def board(monkeypatch, tmp_path) -> Scoreboard:
    """
    A real board on a ledger of this test's own.

    The tool asks for the shared ledger, and one reaching the real one would read
    whatever the machine running the tests happens to have at /credits.
    """
    ledger = CreditLedger(tmp_path / LEDGER_NAME)
    monkeypatch.setattr("miss_quote.tools.scoreboard.shared_ledger", lambda: ledger)

    return Scoreboard(ToolContext(server=SERVER_ALIAS, users=ROSTER))


def _tool(speaker, users=None, config=None, board=None) -> Quotes:
    return Quotes(
        ToolContext(
            server=SERVER_ALIAS,
            config=config or {},
            speaker=speaker,
            users=users or {},
            tools=Toolbox([board] if board is not None else []),
        )
    )


def _utterance(text: str, user: str = SPEAKER, user_id: int = SPEAKER_ID) -> Utterance:
    return Utterance(
        timestamp=datetime.now().astimezone(), user_id=user_id, user=user, text=text
    )


async def _hear(
    tool: Quotes, text: str, user: str = SPEAKER, user_id: int = SPEAKER_ID
) -> None:
    await tool.handle_utterance(_utterance(text, user, user_id), FakeSession(SOURCE))


def _announced(user: str = SPEAKER, tied: bool = False, remark: str = REMARK) -> str:
    """The award as the tool will say it, built from the wording it ships with."""
    template = DEFAULT_TIE_ANNOUNCEMENT if tied else DEFAULT_ANNOUNCEMENT

    return template.format(user=user, credits=_denominated(ONE_CREDIT), remark=remark)


async def _quoted(tool: Quotes, trigger: str = TRIGGER) -> None:
    """Set a line off from somebody who is then barred from naming it."""
    await _hear(tool, trigger, user=ASKER, user_id=ASKER_ID)


def _rebuked(user: str = SPEAKER, penalty: int = DEFAULT_SELF_ANSWER_PENALTY) -> str:
    """What somebody naming their own line is told."""
    return DEFAULT_SELF_ANSWER_ANNOUNCEMENT.format(
        user=user, credits=_denominated(penalty), remark=REMARK
    )


def _warmed_awards(
    *names: str, remarks: tuple = DEFAULT_REMARKS, policing: bool = True
) -> list[str]:
    """
    Every wording for each name, in the order the pre-warm renders them.

    Built from the templates rather than written out, so a reworded default is
    one edit and not a test that fails for saying the same thing differently.
    """
    return [
        wording
        for name in names
        for wording in (
            *(_announced(name, remark=remark) for remark in remarks),
            _announced(name, tied=True),
            *([_rebuked(name)] if policing else []),
        )
    ]


# ── the file ──────────────────────────────────────


def test_a_quote_is_loaded_for_every_row(quotes_file):
    assert _load(quotes_file) == {
        TRIGGER: (Quote(movie=MOVIE, trigger=TRIGGER, text=QUOTE),),
        OTHER_TRIGGER: (
            Quote(movie=OTHER_MOVIE, trigger=OTHER_TRIGGER, text=OTHER_QUOTE),
        ),
    }


def test_a_missing_file_will_not_start(monkeypatch, tmp_path, speech, speaker):
    _pointed_at(monkeypatch, tmp_path / "absent.csv")

    with pytest.raises(ValueError, match="Could not read"):
        _tool(speaker)


def test_a_file_with_no_quote_column_will_not_start(monkeypatch, tmp_path):
    path = _written(monkeypatch, tmp_path, "movie,trigger", f"{MOVIE},{TRIGGER}")

    with pytest.raises(ValueError, match="quote"):
        _load(path)


def test_a_file_with_only_a_header_will_not_start(monkeypatch, tmp_path):
    path = _written(monkeypatch, tmp_path, HEADER)

    with pytest.raises(ValueError, match="no usable quotes"):
        _load(path)


def test_a_row_missing_its_trigger_is_dropped(monkeypatch, tmp_path):
    """One typo in fifty lines should cost that line and no more."""
    path = _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},,{QUOTE}", *ROWS)

    assert set(_load(path)) == {TRIGGER, OTHER_TRIGGER}


def test_a_row_missing_its_quote_is_dropped(monkeypatch, tmp_path):
    path = _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{GENERAL_TRIGGER},", *ROWS)

    assert set(_load(path)) == {TRIGGER, OTHER_TRIGGER}


def test_a_quote_with_an_unfillable_placeholder_is_dropped(monkeypatch, tmp_path):
    """Checked at load rather than at the moment somebody says the trigger."""
    path = _written(
        monkeypatch, tmp_path, HEADER, f"{MOVIE},{GENERAL_TRIGGER},it is {{tally}}", *ROWS
    )

    assert set(_load(path)) == {TRIGGER, OTHER_TRIGGER}


def test_every_row_sharing_a_trigger_is_kept(monkeypatch, tmp_path):
    """A phrase worth answering two ways says so by being written down twice."""
    path = _written(monkeypatch, tmp_path, HEADER, *ROWS, f"{MOVIE},{TRIGGER},{OTHER_QUOTE}")

    assert [quote.text for quote in _load(path)[TRIGGER]] == [QUOTE, OTHER_QUOTE]


def test_rows_sharing_a_trigger_keep_the_order_of_the_file(monkeypatch, tmp_path):
    """So that a seeded draw picks the same answer twice running."""
    path = _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{OTHER_QUOTE}",
        f"{MOVIE},{TRIGGER},{QUOTE}",
    )

    assert [quote.text for quote in _load(path)[TRIGGER]] == [OTHER_QUOTE, QUOTE]


def test_rows_sharing_a_trigger_in_different_cases_are_one_trigger(monkeypatch, tmp_path):
    """The trigger is folded before it is keyed, so case is not what tells them apart."""
    path = _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER.upper()},{OTHER_QUOTE}",
    )

    loaded = _load(path)
    assert set(loaded) == {TRIGGER}
    assert len(loaded[TRIGGER]) == 2


def test_two_triggers_may_share_a_quote(monkeypatch, tmp_path):
    """Which is how the file says two phrases deserve the same answer."""
    path = _written(
        monkeypatch, tmp_path, HEADER, *ROWS, f"{MOVIE},{GENERAL_TRIGGER},{QUOTE}"
    )

    loaded = _load(path)
    assert loaded[TRIGGER][0].text == loaded[GENERAL_TRIGGER][0].text == QUOTE


def test_a_quote_may_hold_a_comma(monkeypatch, tmp_path):
    """It is a CSV of film dialogue; most of the punctuation is in the last column."""
    path = _written(monkeypatch, tmp_path, HEADER, f'{MOVIE},{COMMA_TRIGGER},"{COMMA_QUOTE}"')

    assert path.read_text(encoding="utf-8").count('"') == 2
    assert _load(path)[COMMA_TRIGGER][0].text == COMMA_QUOTE


def test_a_row_with_an_unquoted_comma_is_dropped(monkeypatch, tmp_path):
    """
    What survives it is the line cut at the comma, which is worse than silence.

    The only mistake in the file that loads cleanly: the reader files the rest of
    the sentence under an overflow nothing reads, so `Boy, that escalated
    quickly.` becomes `Boy` and nothing anywhere says so.
    """
    path = _written(
        monkeypatch, tmp_path, HEADER, f"{MOVIE},{COMMA_TRIGGER},{COMMA_QUOTE}", *ROWS
    )

    assert set(_load(path)) == {TRIGGER, OTHER_TRIGGER}


def test_the_dropped_row_says_which_line_and_why(monkeypatch, tmp_path, caplog):
    path = _written(
        monkeypatch, tmp_path, HEADER, f"{MOVIE},{COMMA_TRIGGER},{COMMA_QUOTE}", *ROWS
    )

    with caplog.at_level("WARNING"):
        _load(path)

    assert f"line {FIRST_ROW}" in caplog.text
    assert "comma" in caplog.text


def test_a_trigger_is_folded_for_matching(monkeypatch, tmp_path):
    path = _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{TRIGGER.upper()},{QUOTE}")

    assert set(_load(path)) == {TRIGGER}


def test_the_shipped_file_loads(speech, speaker):
    """The list the image carries, read by the same code that reads a mounted one."""
    assert len(_load(BUNDLED)) > 1


# ── detection ─────────────────────────────────────


async def test_a_trigger_is_answered_with_its_quote(quotes_file, speech, speaker):
    await _hear(_tool(speaker), f"that is pretty {TRIGGER}")

    assert speech.asked == [QUOTE]


async def test_an_utterance_with_no_trigger_says_nothing(quotes_file, speech, speaker):
    await _hear(_tool(speaker), "we should probably get started")

    assert speaker.played == []
    assert speech.asked == []


async def test_detection_ignores_case(quotes_file, speech, speaker):
    await _hear(_tool(speaker), TRIGGER.upper())

    assert speech.asked == [QUOTE]


async def test_punctuation_does_not_hide_a_trigger(quotes_file, speech, speaker):
    await _hear(_tool(speaker), f"well, {TRIGGER}!")

    assert speech.asked == [QUOTE]


async def test_a_trigger_inside_a_longer_word_is_not_a_trigger(quotes_file, speech, speaker):
    """"real" should not fire inside "really"; the triggers are ordinary English."""
    await _hear(_tool(speaker), f"{TRIGGER}ant water")

    assert speech.asked == []


async def test_a_trigger_of_several_words_is_heard(monkeypatch, tmp_path, speech, speaker):
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{SPECIFIC_TRIGGER},{SPECIFIC_QUOTE}")

    await _hear(_tool(speaker), f"I have a {SPECIFIC_TRIGGER} today")

    assert speech.asked == [SPECIFIC_QUOTE]


async def test_the_quote_is_played_back_where_it_was_set_off(quotes_file, speech, speaker):
    await _hear(_tool(speaker), TRIGGER)

    played_source, _ = speaker.played[0]
    assert played_source == SOURCE


async def test_a_quote_plays_at_full_volume(quotes_file, speech, speaker):
    """Nothing here backs off by loudness; a spent trigger simply says nothing."""
    await _hear(_tool(speaker), TRIGGER)

    assert speaker.scales == [UNITY_VOLUME]


# ── choosing between triggers ─────────────────────


async def test_one_utterance_earns_one_quote(quotes_file, speech, speaker):
    """Two lines over the top of each other is a denial of service on the channel."""
    await _hear(_tool(speaker), f"{TRIGGER} and also {OTHER_TRIGGER}")

    assert len(speaker.played) == 1


async def test_the_earliest_trigger_in_the_sentence_wins(quotes_file, speech, speaker):
    await _hear(_tool(speaker), f"{OTHER_TRIGGER}, but {TRIGGER}")

    assert speech.asked == [OTHER_QUOTE]


async def test_a_spent_trigger_does_not_swallow_a_live_one(quotes_file, speech, speaker):
    tool = _tool(speaker)
    await _hear(tool, TRIGGER)

    await _hear(tool, f"{TRIGGER} and also {OTHER_TRIGGER}")

    assert speech.asked == [QUOTE, OTHER_QUOTE]


async def test_the_more_specific_of_two_overlapping_triggers_wins(
    monkeypatch, tmp_path, speech, speaker
):
    """The longer trigger is in the file precisely because it deserves its own line."""
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{GENERAL_TRIGGER},{GENERAL_QUOTE}",
        f"{MOVIE},{SPECIFIC_TRIGGER},{SPECIFIC_QUOTE}",
    )

    await _hear(_tool(speaker), f"somebody has a {SPECIFIC_TRIGGER}")

    assert speech.asked == [SPECIFIC_QUOTE]


# ── a trigger with more than one answer ───────────


async def test_a_trigger_with_two_answers_gives_one_of_them(
    monkeypatch, tmp_path, speech, speaker
):
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER},{OTHER_QUOTE}",
    )

    await _hear(_tool(speaker), TRIGGER)

    assert speech.asked in ([QUOTE], [OTHER_QUOTE])


async def test_which_answer_a_trigger_gives_is_drawn_each_time(
    monkeypatch, tmp_path, speech, speaker
):
    """
    Drawn when the trigger fires rather than settled at load.

    A choice made once at startup would be the same one until the next restart,
    which is a file with two answers in it and a channel that only ever hears
    the one.
    """
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER},{OTHER_QUOTE}",
    )
    _drawn(monkeypatch, last=True)

    tool = _tool(speaker, config={})
    await _hear(tool, TRIGGER)

    assert speech.asked == [OTHER_QUOTE]


async def test_every_answer_a_trigger_can_give_is_warmed(
    monkeypatch, tmp_path, speech, speaker
):
    """Warming any less would leave the channel waiting on the coin toss."""
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER},{OTHER_QUOTE}",
    )

    await _tool(speaker).prewarm()

    assert speech.warmed == [QUOTE, OTHER_QUOTE]


async def test_a_trigger_with_two_answers_still_fires_once_per_window(
    monkeypatch, tmp_path, speech, speaker
):
    """The backoff is on the trigger, so several answers are spent together."""
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER},{OTHER_QUOTE}",
    )

    tool = _tool(speaker)
    await _hear(tool, TRIGGER)
    await _hear(tool, TRIGGER)

    assert len(speech.asked) == 1


# ── the speaker's name ────────────────────────────


async def test_a_quote_can_name_whoever_set_it_off(monkeypatch, tmp_path, speech, speaker):
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{PERSONAL_TRIGGER},{PERSONAL_QUOTE}")

    await _hear(_tool(speaker), f"I have a {PERSONAL_TRIGGER}")

    assert speech.asked == [PERSONAL_QUOTE.format(user=SPEAKER)]


async def test_the_name_comes_from_the_utterance(monkeypatch, tmp_path, speech, speaker):
    """Which is the roster name where a server configured one."""
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{PERSONAL_TRIGGER},{PERSONAL_QUOTE}")

    await _hear(_tool(speaker), PERSONAL_TRIGGER, user="Someone Else")

    assert speech.asked == [PERSONAL_QUOTE.format(user="Someone Else")]


# ── the backoff ───────────────────────────────────


async def test_a_trigger_said_twice_is_answered_once(quotes_file, speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, TRIGGER)
    await _hear(tool, f"yes, {TRIGGER}")

    assert speech.asked == [QUOTE]


async def test_a_spent_trigger_does_not_silence_another(quotes_file, speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, TRIGGER)
    await _hear(tool, OTHER_TRIGGER)

    assert speech.asked == [QUOTE, OTHER_QUOTE]


async def test_the_backoff_is_the_trigger_rather_than_the_speaker(
    quotes_file, speech, speaker
):
    """What wears out is the line, not the person who set it off."""
    tool = _tool(speaker)

    await _hear(tool, TRIGGER, user=SPEAKER)
    await _hear(tool, TRIGGER, user=OTHER_SPEAKER)

    assert speech.asked == [QUOTE]


async def test_two_servers_cool_down_separately(quotes_file, speech, speaker):
    """Two channels arriving at the same line have each made the joke once."""
    here = _tool(speaker)
    elsewhere = Quotes(ToolContext(server="second-server", speaker=speaker))

    await _hear(here, TRIGGER)
    await _hear(elsewhere, TRIGGER)

    assert speech.asked == [QUOTE, QUOTE]


def test_a_trigger_stops_being_spent_once_the_window_has_passed():
    recent = RecentQuotes(BACKOFF)
    now = 1_000.0

    recent.record(TRIGGER, now=now)

    assert recent.ready(TRIGGER, now=now + BACKOFF + 1)


def test_a_trigger_inside_the_window_is_still_spent():
    recent = RecentQuotes(BACKOFF)
    now = 1_000.0

    recent.record(TRIGGER, now=now)

    assert not recent.ready(TRIGGER, now=now + BACKOFF - 1)


def test_a_trigger_nobody_has_said_is_ready():
    assert RecentQuotes(BACKOFF).ready(TRIGGER)


def test_a_window_of_nothing_answers_every_time():
    """Which is what a deployment that wants the line every time asks for."""
    recent = RecentQuotes(NO_BACKOFF)
    now = 1_000.0

    recent.record(TRIGGER, now=now)

    assert recent.ready(TRIGGER, now=now)


def test_a_trigger_that_has_aged_out_is_forgotten_entirely():
    """The map is per process and nothing sweeps it; reading is what prunes."""
    recent = RecentQuotes(BACKOFF)
    now = 1_000.0
    recent.record(TRIGGER, now=now)

    recent.ready(TRIGGER, now=now + BACKOFF + 1)

    assert TRIGGER not in recent._fired


def test_the_window_comes_from_the_deployment(monkeypatch):
    """Nothing carries a five-minute default of its own past the settings."""
    monkeypatch.setattr(
        "miss_quote.tools.quotes.quotes_cfg", replace(quotes_cfg, backoff_seconds=SHORT_WINDOW)
    )

    assert RecentQuotes().window == SHORT_WINDOW


# ── the pre-warm ──────────────────────────────────


async def test_every_quote_is_warmed(quotes_file, speech, speaker):
    await _tool(speaker).prewarm()

    assert speech.warmed == [QUOTE, OTHER_QUOTE]


async def test_a_quote_naming_nobody_is_warmed_once_however_many_speakers(
    quotes_file, speech, speaker
):
    await _tool(speaker, users=ROSTER).prewarm()

    assert speech.warmed == [QUOTE, OTHER_QUOTE, *_warmed_awards(SPEAKER, OTHER_SPEAKER)]


async def test_a_quote_naming_the_speaker_is_warmed_per_name(
    monkeypatch, tmp_path, speech, speaker
):
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{PERSONAL_TRIGGER},{PERSONAL_QUOTE}")

    await _tool(speaker, users=ROSTER).prewarm()

    assert speech.warmed == [
        PERSONAL_QUOTE.format(user=SPEAKER),
        PERSONAL_QUOTE.format(user=OTHER_SPEAKER),
        *_warmed_awards(SPEAKER, OTHER_SPEAKER),
    ]


async def test_both_wordings_of_the_award_are_warmed_per_name(
    quotes_file, speech, speaker
):
    """A tie is announced as one, and nobody should wait for the synthesizer for it."""
    await _tool(speaker, users=ROSTER).prewarm()

    assert _announced(SPEAKER, tied=True) in speech.warmed


async def test_no_award_is_warmed_where_nothing_is_being_asked(
    quotes_file, speech, speaker
):
    tool = _tool(speaker, users=ROSTER, config={ANSWER_SECONDS_KEY: NO_WINDOW})

    await tool.prewarm()

    assert speech.warmed == [QUOTE, OTHER_QUOTE]


async def test_a_quote_naming_the_speaker_warms_nothing_without_a_roster(
    monkeypatch, tmp_path, speech, speaker
):
    """Their Discord name is not knowable from here, and not a closed set."""
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{PERSONAL_TRIGGER},{PERSONAL_QUOTE}")

    await _tool(speaker).prewarm()

    assert speech.warmed == []


async def test_a_warmed_quote_is_exactly_what_gets_said(quotes_file, speech, speaker):
    """A phrase differing by a space is one that gets synthesized twice."""
    tool = _tool(speaker, users=ROSTER)
    await tool.prewarm()

    await _hear(tool, TRIGGER)

    assert speech.asked[0] in speech.warmed


async def test_warming_plays_nothing(quotes_file, speech, speaker):
    """It is preparation; nobody has said anything yet."""
    await _tool(speaker, users=ROSTER).prewarm()

    assert speaker.played == []
    assert speech.asked == []


async def test_the_runner_warms_a_configured_server(quotes_file, speech, speaker):
    """The seam the rest of these skip past: no `config` block at all is enough."""
    servers = {
        SOURCE.guild_id: ServerConfig(
            alias=SERVER_ALIAS,
            users=ROSTER,
            tools={Quotes.name: ToolSettings(enabled=True, config={})},
        )
    }
    runner = ToolRunner(servers, {Quotes.name: Quotes}, speaker)

    await runner.prewarm()

    assert runner.problems == []
    assert speech.warmed == [QUOTE, OTHER_QUOTE, *_warmed_awards(SPEAKER, OTHER_SPEAKER)]


# ── the round ─────────────────────────────────────


def _round(movie: str = MOVIE, window: float = ANSWER_WINDOW, tie: float = TIE_WINDOW):
    """A round on a fixed clock, so a window is tested rather than waited out."""
    return Round(movie, window, tie, opened=NOW)


def test_naming_the_film_inside_the_window_earns():
    assert _round().answered_by(_utterance(ANSWER), now=NOW + 1)


def test_naming_the_film_after_the_window_earns_nothing():
    assert not _round().answered_by(_utterance(ANSWER), now=NOW + ANSWER_WINDOW + 1)


def test_naming_the_wrong_film_earns_nothing():
    assert not _round().answered_by(_utterance(WRONG_ANSWER), now=NOW + 1)


def test_saying_the_film_without_asking_earns_nothing():
    """It is a question or it is somebody talking about a film."""
    assert not _round().answered_by(_utterance(MOVIE), now=NOW + 1)


def test_a_second_answer_inside_the_tie_window_earns_too():
    """Which of two people the transcriber returned first is not a fact about them."""
    opened = _round()
    opened.answered_by(_utterance(ANSWER, SPEAKER, SPEAKER_ID), now=NOW + 1)

    assert opened.answered_by(
        _utterance(ANSWER, OTHER_SPEAKER, OTHER_SPEAKER_ID), now=NOW + 1 + TIE_WINDOW
    )


def test_a_second_answer_after_the_tie_window_has_been_beaten_to_it():
    opened = _round()
    opened.answered_by(_utterance(ANSWER, SPEAKER, SPEAKER_ID), now=NOW + 1)

    assert not opened.answered_by(
        _utterance(ANSWER, OTHER_SPEAKER, OTHER_SPEAKER_ID),
        now=NOW + 1 + TIE_WINDOW + 0.1,
    )


def test_the_tie_window_runs_from_the_first_answer_rather_than_the_question():
    """Nobody is punished for the round having been asked a moment earlier."""
    opened = _round()
    opened.answered_by(_utterance(ANSWER, SPEAKER, SPEAKER_ID), now=NOW + ANSWER_WINDOW)

    assert opened.answered_by(
        _utterance(ANSWER, OTHER_SPEAKER, OTHER_SPEAKER_ID), now=NOW + ANSWER_WINDOW
    )


def test_no_tie_window_pays_only_whoever_was_first():
    opened = _round(tie=NO_WINDOW)
    opened.answered_by(_utterance(ANSWER, SPEAKER, SPEAKER_ID), now=NOW + 1)

    assert not opened.answered_by(
        _utterance(ANSWER, OTHER_SPEAKER, OTHER_SPEAKER_ID), now=NOW + 1.1
    )


def test_nobody_earns_twice_from_one_round():
    opened = _round()
    opened.answered_by(_utterance(ANSWER), now=NOW + 1)

    assert not opened.answered_by(_utterance(ANSWER), now=NOW + 1.5)


def test_a_round_is_spent_once_its_window_has_passed():
    assert _round().expired(now=NOW + ANSWER_WINDOW + 1)


def test_a_round_inside_its_window_is_still_open():
    assert not _round().expired(now=NOW + ANSWER_WINDOW - 1)


# ── what counts as naming the film ────────────────


@pytest.mark.parametrize(
    "answer",
    [
        ANSWER,
        ANSWER.lower(),
        CONTRACTED_ANSWER,
        f"oh, {ANSWER}!",
        f"{ANSWER}, obviously",
    ],
)
def test_the_film_may_be_named_however_it_is_said(answer):
    assert _round().answered_by(_utterance(answer), now=NOW + 1)


def test_a_leading_article_is_optional():
    """The file writes the title as the poster does; a channel says either."""
    assert _round(movie=ARTICLE_MOVIE).answered_by(
        _utterance("what is matrix"), now=NOW + 1
    )


def test_a_title_with_a_leading_article_answers_to_it_as_well():
    assert _round(movie=ARTICLE_MOVIE).answered_by(
        _utterance(f"what is {ARTICLE_MOVIE}"), now=NOW + 1
    )


def test_an_abbreviation_in_a_title_may_be_said_as_a_word():
    assert _round(movie=VERSUS_MOVIE).answered_by(
        _utterance("what is tucker and dale versus evil"), now=NOW + 1
    )


def test_a_title_said_as_a_word_answers_to_the_abbreviation():
    assert _round(movie="Tucker and Dale versus Evil").answered_by(
        _utterance("what is tucker and dale vs. evil"), now=NOW + 1
    )


def test_an_apostrophe_the_transcript_dropped_still_names_the_film():
    assert _round(movie="Hitchhiker's Guide to the Galaxy").answered_by(
        _utterance("what is hitchhikers guide to the galaxy"), now=NOW + 1
    )


def test_a_longer_word_is_not_the_film():
    assert not _round().answered_by(_utterance(f"what is {MOVIE}ing"), now=NOW + 1)


# ── being paid for it ─────────────────────────────


async def test_naming_the_film_earns_a_credit(quotes_file, speech, speaker, board):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == ONE_CREDIT


async def test_the_question_is_only_asked_once_the_line_has_been_said(
    quotes_file, speech, speaker, board
):
    """Nobody can name a film the channel has not been quoted at yet."""
    await _hear(_tool(speaker, board=board), ANSWER)

    assert board.balance(SPEAKER_ID) == NOTHING


async def test_naming_the_wrong_film_earns_nothing(quotes_file, speech, speaker, board):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, WRONG_ANSWER)

    assert board.balance(SPEAKER_ID) == NOTHING


async def test_two_people_naming_it_at_once_are_both_paid(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER, user=SPEAKER, user_id=SPEAKER_ID)
    await _hear(tool, ANSWER, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert board.balance(SPEAKER_ID) == board.balance(OTHER_SPEAKER_ID) == ONE_CREDIT


async def test_saying_it_twice_is_paid_once(quotes_file, speech, speaker, board):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)
    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == ONE_CREDIT


async def test_a_credit_is_earned_per_round(quotes_file, speech, speaker, board):
    tool = _tool(speaker, board=board)

    await _quoted(tool)
    await _hear(tool, ANSWER)
    await _quoted(tool, OTHER_TRIGGER)
    await _hear(tool, f"what is {OTHER_MOVIE}")

    assert board.balance(SPEAKER_ID) == TWO_CREDITS


async def test_two_rounds_may_be_open_at_once(quotes_file, speech, speaker, board):
    """An answer names its own film, so neither question is made ambiguous."""
    tool = _tool(speaker, board=board)
    await _quoted(tool)
    await _quoted(tool, OTHER_TRIGGER)

    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == ONE_CREDIT


async def test_naming_the_film_is_announced(quotes_file, speech, speaker, board):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert speech.asked == [QUOTE, _announced(SPEAKER)]


async def test_the_announcement_names_whoever_got_it(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert speech.asked[-1] == _announced(OTHER_SPEAKER)


async def test_an_award_has_no_chime_in_front_of_it(
    quotes_file, speech, speaker, board
):
    """A flourish is for an interruption; this one answers a question already asked."""
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    _, spoken = speaker.played[-1]
    assert spoken == _announced(SPEAKER)


async def test_the_award_is_announced_where_it_was_earned(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    played_source, _ = speaker.played[-1]
    assert played_source == SOURCE


async def test_a_tied_answer_is_told_it_also_won(quotes_file, speech, speaker, board):
    """The whole sentence again reads as though the bot had lost track."""
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER, user=SPEAKER, user_id=SPEAKER_ID)
    await _hear(tool, ANSWER, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert speech.asked[-1] == _announced(OTHER_SPEAKER, tied=True)


async def _mid_announcement(board) -> tuple[Quotes, BlockingSpeaker, asyncio.Task]:
    """A tool with an award playing and the channel held open."""
    speaker = BlockingSpeaker()
    tool = _tool(speaker, board=board)

    quoting = asyncio.create_task(_quoted(tool))
    await speaker.playing.wait()
    speaker.finish.set()
    await quoting

    speaker.playing.clear()
    speaker.finish.clear()
    playing = asyncio.create_task(_hear(tool, ANSWER))
    await speaker.playing.wait()

    return tool, speaker, playing


async def test_a_tied_award_is_announced_while_the_first_is_still_playing(
    quotes_file, speech, board
):
    """Both are said. Paying somebody silently reads as the round having missed them."""
    tool, speaker, playing = await _mid_announcement(board)

    tying = asyncio.create_task(
        _hear(tool, ANSWER, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)
    )
    speaker.finish.set()
    await asyncio.gather(playing, tying)

    # Unordered: what keeps two announcements in the order they were earned is
    # the real speaker's per-server lock, which this stand-in does not hold.
    assert sorted(speech.asked) == sorted(
        [QUOTE, _announced(SPEAKER), _announced(OTHER_SPEAKER, tied=True)]
    )


async def test_a_rebuke_is_announced_while_something_is_still_playing(
    quotes_file, speech, board
):
    """A rebuke passed over is a fine nobody was told about."""
    tool, speaker, playing = await _mid_announcement(board)

    rebuking = asyncio.create_task(_hear(tool, ANSWER, user=ASKER, user_id=ASKER_ID))
    speaker.finish.set()
    await asyncio.gather(playing, rebuking)

    assert _rebuked(ASKER) in speech.asked


async def test_a_tied_award_is_still_paid(quotes_file, speech, board):
    tool, speaker, playing = await _mid_announcement(board)

    tying = asyncio.create_task(
        _hear(tool, ANSWER, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)
    )
    speaker.finish.set()
    await asyncio.gather(playing, tying)

    assert board.balance(OTHER_SPEAKER_ID) == ONE_CREDIT


async def test_the_channel_is_free_again_once_an_award_has_been_announced(
    quotes_file, speech, board
):
    tool, speaker, playing = await _mid_announcement(board)
    speaker.finish.set()
    await playing

    await _quoted(tool, OTHER_TRIGGER)

    assert speech.asked[-1] == OTHER_QUOTE


async def test_a_server_may_write_its_own_announcement(
    quotes_file, speech, speaker, board
):
    wording = "{user} wins {credits}."
    tool = _tool(speaker, config={ANNOUNCEMENT_KEY: wording}, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert speech.asked[-1] == wording.format(
        user=SPEAKER, credits=_denominated(ONE_CREDIT)
    )


def test_an_announcement_with_an_unfillable_placeholder_will_not_start(
    quotes_file, speech, speaker
):
    """Discovered at startup rather than at the moment there is a credit to explain."""
    with pytest.raises(ValueError, match=ANNOUNCEMENT_KEY):
        _tool(speaker, config={ANNOUNCEMENT_KEY: "{user} wins {tally}."})


def test_a_tie_announcement_with_an_unfillable_placeholder_will_not_start(
    quotes_file, speech, speaker
):
    with pytest.raises(ValueError, match=TIE_ANNOUNCEMENT_KEY):
        _tool(speaker, config={TIE_ANNOUNCEMENT_KEY: "{user} also wins {tally}."})


async def test_the_ending_is_drawn_from_the_list(
    monkeypatch, quotes_file, speech, speaker, board
):
    """One fixed sentence is a joke told once and then endured."""
    chosen = DEFAULT_REMARKS[-1]
    monkeypatch.setattr(
        "miss_quote.tools.quotes._chosen", lambda remarks: remarks[-1]
    )
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert speech.asked[-1] == _announced(SPEAKER, remark=chosen)


def test_a_server_may_add_an_ending_of_its_own(quotes_file, speech, speaker):
    tool = _tool(speaker, config={REMARKS_KEY: [ADDED_REMARK]})

    assert tool._remarks == (*DEFAULT_REMARKS, ADDED_REMARK)


def test_an_added_ending_does_not_replace_the_shipped_ones(
    quotes_file, speech, speaker
):
    """Saying one extra thing should not cost writing out all of them."""
    tool = _tool(speaker, config={REMARKS_KEY: [ADDED_REMARK]})

    assert set(DEFAULT_REMARKS) <= set(tool._remarks)


def test_a_lone_ending_may_be_written_unquoted(quotes_file, speech, speaker):
    """A bare string where a list was expected is one line, not a mistake."""
    tool = _tool(speaker, config={REMARKS_KEY: ADDED_REMARK})

    assert tool._remarks == (*DEFAULT_REMARKS, ADDED_REMARK)


def test_an_ending_that_is_not_a_list_will_not_start(quotes_file, speech, speaker):
    tool_config = {REMARKS_KEY: {"not": "a list"}}

    with pytest.raises(ValueError, match=REMARKS_KEY):
        _tool(speaker, config=tool_config)


async def test_every_ending_is_warmed(quotes_file, speech, speaker):
    """Which one comes up is decided when somebody wins, not at startup."""
    tool = _tool(speaker, users=ROSTER, config={REMARKS_KEY: [ADDED_REMARK]})

    await tool.prewarm()

    assert speech.warmed == [
        QUOTE,
        OTHER_QUOTE,
        *_warmed_awards(
            SPEAKER, OTHER_SPEAKER, remarks=(*DEFAULT_REMARKS, ADDED_REMARK)
        ),
    ]


async def test_a_tie_wording_with_no_ending_is_warmed_once(
    quotes_file, speech, speaker
):
    """A template carrying no remark is one phrase however many are written."""
    await _tool(speaker, users=ROSTER).prewarm()

    assert speech.warmed.count(_announced(SPEAKER, tied=True)) == 1


async def test_the_currency_is_what_the_deployment_calls_it(
    monkeypatch, quotes_file, speech, speaker, board
):
    monkeypatch.setattr(
        "miss_quote.tools.quotes.scoreboard_cfg",
        replace(scoreboard_cfg, currency="doubloon"),
    )
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert "1 doubloon" in speech.asked[-1]


async def test_an_answer_does_not_set_off_another_quote(
    monkeypatch, tmp_path, speech, speaker, board
):
    """Otherwise the tool would be driving the loop rather than following it."""
    _written(
        monkeypatch,
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{OTHER_MOVIE},{MOVIE.lower()},{OTHER_QUOTE}",
    )
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert speech.asked == [QUOTE, _announced(SPEAKER)]


async def test_a_row_that_names_no_film_asks_nothing(
    monkeypatch, tmp_path, speech, speaker, board
):
    _written(monkeypatch, tmp_path, HEADER, f",{TRIGGER},{QUOTE}")
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, "what is it")

    assert board.balance(SPEAKER_ID) == NOTHING


async def test_a_server_with_no_board_pays_nothing_and_carries_on(
    quotes_file, speech, speaker
):
    """Saying the line is this tool's job; keeping score is somebody else's."""
    tool = _tool(speaker)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert speech.asked == [QUOTE, _announced(SPEAKER)]


# ── naming your own line ──────────────────────────


async def _self_answered(tool: Quotes) -> None:
    """Somebody sets a line off and then names it themselves."""
    await _quoted(tool)
    await _hear(tool, ANSWER, user=ASKER, user_id=ASKER_ID)


async def test_naming_your_own_line_earns_nothing(quotes_file, speech, speaker, board):
    """The trigger and the title are both in front of them; they recalled neither."""
    await _self_answered(_tool(speaker, board=board))

    assert board.balance(ASKER_ID) == -DEFAULT_SELF_ANSWER_PENALTY


async def test_naming_your_own_line_is_called_out(quotes_file, speech, speaker, board):
    """A rule nobody is told about is one everybody keeps testing."""
    await _self_answered(_tool(speaker, board=board))

    assert speech.asked[-1] == _rebuked(ASKER)


async def test_the_rebuke_says_what_it_cost(quotes_file, speech, speaker, board):
    await _self_answered(_tool(speaker, board=board))

    assert _denominated(DEFAULT_SELF_ANSWER_PENALTY) in speech.asked[-1]


async def test_the_penalty_is_what_the_server_set(quotes_file, speech, speaker, board):
    penalty = 3
    tool = _tool(speaker, config={SELF_ANSWER_PENALTY_KEY: penalty}, board=board)

    await _self_answered(tool)

    assert board.balance(ASKER_ID) == -penalty


async def test_a_penalty_is_taken_once_however_many_attempts(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, board=board)
    await _self_answered(tool)

    await _hear(tool, ANSWER, user=ASKER, user_id=ASKER_ID)

    assert board.balance(ASKER_ID) == -DEFAULT_SELF_ANSWER_PENALTY


async def test_naming_your_own_line_does_not_close_the_round(
    quotes_file, speech, speaker, board
):
    """An attempt should not win anything, nor spoil it for the channel."""
    tool = _tool(speaker, board=board)
    await _self_answered(tool)

    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == ONE_CREDIT


async def test_naming_your_own_line_does_not_start_the_tie_window(
    quotes_file, speech, speaker, board
):
    """Whoever names it after them is the first answer, not a tie."""
    tool = _tool(speaker, board=board)
    await _self_answered(tool)

    await _hear(tool, ANSWER)

    assert speech.asked[-1] == _announced(SPEAKER)


async def test_somebody_else_naming_it_is_not_penalized(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == ONE_CREDIT


async def test_the_bar_is_per_round(quotes_file, speech, speaker, board):
    """Setting one line off does not disqualify you from naming the next."""
    tool = _tool(speaker, board=board)
    await _quoted(tool)
    await _hear(tool, OTHER_TRIGGER, user=SPEAKER, user_id=SPEAKER_ID)

    await _hear(tool, ANSWER, user=ASKER, user_id=ASKER_ID)

    assert board.balance(ASKER_ID) == -DEFAULT_SELF_ANSWER_PENALTY


async def test_a_server_may_let_people_name_their_own(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, config={PENALIZE_SELF_ANSWERS_KEY: False}, board=board)

    await _self_answered(tool)

    assert board.balance(ASKER_ID) == ONE_CREDIT


async def test_a_server_that_allows_it_says_the_ordinary_thing(
    quotes_file, speech, speaker, board
):
    tool = _tool(speaker, config={PENALIZE_SELF_ANSWERS_KEY: False}, board=board)

    await _self_answered(tool)

    assert speech.asked[-1] == _announced(ASKER)


async def test_a_server_that_allows_it_warms_no_rebuke(quotes_file, speech, speaker):
    """Rendering it would be paying a synthesizer for a phrase nothing can reach."""
    tool = _tool(
        speaker, users=ROSTER, config={PENALIZE_SELF_ANSWERS_KEY: False}
    )

    await tool.prewarm()

    assert speech.warmed == [
        QUOTE,
        OTHER_QUOTE,
        *_warmed_awards(SPEAKER, OTHER_SPEAKER, policing=False),
    ]


async def test_the_rebuke_is_warmed_per_name(quotes_file, speech, speaker):
    await _tool(speaker, users=ROSTER).prewarm()

    assert _rebuked(SPEAKER) in speech.warmed


def test_a_server_may_write_its_own_rebuke(quotes_file, speech, speaker):
    wording = "No. {user} loses {credits}."
    tool = _tool(speaker, config={SELF_ANSWER_ANNOUNCEMENT_KEY: wording})

    assert tool._announcements[SELF_ANSWER_ANNOUNCEMENT_KEY] == wording


def test_a_rebuke_with_an_unfillable_placeholder_will_not_start(
    quotes_file, speech, speaker
):
    with pytest.raises(ValueError, match=SELF_ANSWER_ANNOUNCEMENT_KEY):
        _tool(speaker, config={SELF_ANSWER_ANNOUNCEMENT_KEY: "No. {tally}."})


def test_a_penalty_that_is_not_a_number_will_not_start(quotes_file, speech, speaker):
    with pytest.raises(ValueError, match=SELF_ANSWER_PENALTY_KEY):
        _tool(speaker, config={SELF_ANSWER_PENALTY_KEY: "five"})


def test_a_negative_penalty_is_floored_at_nothing(quotes_file, speech, speaker):
    """A penalty below zero is a reward, and there is a flag for wanting that."""
    tool = _tool(speaker, config={SELF_ANSWER_PENALTY_KEY: -5})

    assert tool._penalty == NOTHING


# ── the windows, as a server sets them ────────────


async def test_no_answer_window_asks_nothing(quotes_file, speech, speaker, board):
    """Which is what a deployment that wants the lines and not the game asks for."""
    tool = _tool(speaker, config={ANSWER_SECONDS_KEY: NO_WINDOW}, board=board)
    await _quoted(tool)

    await _hear(tool, ANSWER)

    assert board.balance(SPEAKER_ID) == NOTHING


def test_the_windows_come_from_the_server(quotes_file, speech, speaker):
    tool = _tool(
        speaker,
        config={ANSWER_SECONDS_KEY: SHORT_WINDOW, TIE_SECONDS_KEY: TIE_WINDOW},
    )

    assert (tool._window, tool._tie) == (SHORT_WINDOW, TIE_WINDOW)


def test_a_server_that_sets_neither_window_gets_the_defaults(
    quotes_file, speech, speaker
):
    tool = _tool(speaker)

    assert (tool._window, tool._tie) == (ANSWER_WINDOW, TIE_WINDOW)


def test_a_window_that_is_not_a_number_will_not_start(quotes_file, speech, speaker):
    """A server that wrote a window down meant something by it."""
    with pytest.raises(ValueError, match=ANSWER_SECONDS_KEY):
        _tool(speaker, config={ANSWER_SECONDS_KEY: "five"})
