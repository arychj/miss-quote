"""What sets a quote off, what comes back, and how long the line stays spent."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from config import UNITY_VOLUME, ServerConfig, ToolSettings, quotes_cfg
from tools.quotes import Quote, Quotes, RecentQuotes, _load
from tools.runner import ToolRunner
from transcript.writer import Source, Utterance

SERVER_ALIAS = "first-server"
SPEAKER = "Speaker One"
OTHER_SPEAKER = "Speaker Two"

SPEAKER_ID = 234567890123456789
OTHER_SPEAKER_ID = 345678901234567890
ROSTER = {SPEAKER_ID: SPEAKER, OTHER_SPEAKER_ID: OTHER_SPEAKER}

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

BUNDLED = Path(__file__).resolve().parent.parent / "resources" / "quotes.csv"

# A window of its own, so a test about the backoff is not also a test of what the
# deployment set it to.
BACKOFF = 300.0
NO_BACKOFF = 0.0
SHORT_WINDOW = 30.0


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


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


@pytest.fixture
def speech(monkeypatch) -> FakeSpeech:
    """Replace the process-wide cache so nothing reaches a synthesizer."""
    fake = FakeSpeech()
    monkeypatch.setattr("tools.quotes.shared_cache", lambda: fake)
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
    monkeypatch.setattr("tools.quotes.quotes_cfg", replace(quotes_cfg, file=path))


def _tool(speaker, users=None) -> Quotes:
    return Quotes(server=SERVER_ALIAS, config={}, speaker=speaker, users=users)


def _utterance(text: str, user: str = SPEAKER, user_id: int = SPEAKER_ID) -> Utterance:
    return Utterance(
        timestamp=datetime.now().astimezone(), user_id=user_id, user=user, text=text
    )


async def _hear(tool: Quotes, text: str, user: str = SPEAKER) -> None:
    await tool.handle_utterance(_utterance(text, user), FakeSession(SOURCE))


# ── the file ──────────────────────────────────────


def test_a_quote_is_loaded_for_every_row(quotes_file):
    assert _load(quotes_file) == {
        TRIGGER: Quote(movie=MOVIE, trigger=TRIGGER, text=QUOTE),
        OTHER_TRIGGER: Quote(movie=OTHER_MOVIE, trigger=OTHER_TRIGGER, text=OTHER_QUOTE),
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


def test_the_first_of_two_rows_sharing_a_trigger_wins(monkeypatch, tmp_path):
    path = _written(monkeypatch, tmp_path, HEADER, *ROWS, f"{MOVIE},{TRIGGER},something else")

    assert _load(path)[TRIGGER].text == QUOTE


def test_two_triggers_may_share_a_quote(monkeypatch, tmp_path):
    """Which is how the file says two phrases deserve the same answer."""
    path = _written(
        monkeypatch, tmp_path, HEADER, *ROWS, f"{MOVIE},{GENERAL_TRIGGER},{QUOTE}"
    )

    loaded = _load(path)
    assert loaded[TRIGGER].text == loaded[GENERAL_TRIGGER].text == QUOTE


def test_a_quote_may_hold_a_comma(monkeypatch, tmp_path):
    """It is a CSV of film dialogue; most of the punctuation is in the last column."""
    path = _written(monkeypatch, tmp_path, HEADER, f'{MOVIE},{COMMA_TRIGGER},"{COMMA_QUOTE}"')

    assert path.read_text(encoding="utf-8").count('"') == 2
    assert _load(path)[COMMA_TRIGGER].text == COMMA_QUOTE


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
    elsewhere = Quotes(server="second-server", config={}, speaker=speaker, users=None)

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
        "tools.quotes.quotes_cfg", replace(quotes_cfg, backoff_seconds=SHORT_WINDOW)
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

    assert speech.warmed == [QUOTE, OTHER_QUOTE]


async def test_a_quote_naming_the_speaker_is_warmed_per_name(
    monkeypatch, tmp_path, speech, speaker
):
    _written(monkeypatch, tmp_path, HEADER, f"{MOVIE},{PERSONAL_TRIGGER},{PERSONAL_QUOTE}")

    await _tool(speaker, users=ROSTER).prewarm()

    assert speech.warmed == [
        PERSONAL_QUOTE.format(user=SPEAKER),
        PERSONAL_QUOTE.format(user=OTHER_SPEAKER),
    ]


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
    assert speech.warmed == [QUOTE, OTHER_QUOTE]
