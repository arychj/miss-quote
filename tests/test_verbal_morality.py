"""What the Verbal Morality Bot hears, and what it says about it."""

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import miss_quote.tools.tts as tts_tool
import miss_quote.tools.verbal_morality as verbal_morality
from miss_quote.audio.chimes import CHIME_SUFFIX
from miss_quote.config import (
    SILENT_VOLUME,
    UNITY_VOLUME,
    ServerConfig,
    ToolSettings,
    morality_cfg,
    scoreboard_cfg,
    tts_cfg,
)
from miss_quote.ledger.credits import CreditLedger
from miss_quote.tools.base import ToolContext, Toolbox
from miss_quote.tools.runner import ToolRunner
from miss_quote.tools.scoreboard import Scoreboard
from miss_quote.tools.tts import Tts
from miss_quote.tools.verbal_morality import (
    DEFAULT_ANNOUNCEMENT,
    DEFAULT_REPEAT_ANNOUNCEMENT,
    REPEATED_FINE,
    RecentViolations,
    VerbalMorality,
)
from miss_quote.transcript.writer import Source, Utterance

SERVER_ALIAS = "first-server"
OTHER_SERVER_ALIAS = "second-server"
SPEAKER = "Speaker One"
OTHER_SPEAKER = "Speaker Two"

SPEAKER_ID = 234567890123456789
OTHER_SPEAKER_ID = 345678901234567890
ROSTER = {SPEAKER_ID: SPEAKER, OTHER_SPEAKER_ID: OTHER_SPEAKER}

# Somebody the server never wrote down, known by whatever Discord reports.
STRANGER = "Someone Else"
STRANGER_ID = 456789012345678901

# Enough violations to reach the floor whatever the step is.
MANY_VIOLATIONS = 100
QUIETEST = 0.25

# The backoff as the deployment has it, which is what a tool built here uses.
BACKOFF_STEP = morality_cfg.backoff_step
BACKOFF_WINDOW = morality_cfg.backoff_seconds
REPEAT_WINDOW = morality_cfg.repeat_seconds

# Announcements the pre-warm renders per speaker: one violation, two, and three,
# each in both the first-fine and the repeat wording.
WARMED_PER_SPEAKER = 6

# The name as a config file writes it, and the file it resolves to.
CHIME_NAME = "chime"
CHIME_FILE = f"{CHIME_NAME}{CHIME_SUFFIX}"
CHIME_AUDIO = "♪"

# A phrase in pieces, for tests about what is waited for before playback starts.
CHUNKS = ("one", "two", "three")
NO_HEAD_START = 0

SOURCE = Source(
    guild_id=1, guild_alias=SERVER_ALIAS, channel_id=2, channel="general-voice"
)

FORBIDDEN = "fiddlesticks"
ALSO_FORBIDDEN = "poppycock"
WORDS = [FORBIDDEN, ALSO_FORBIDDEN]

# A stem whose endings all attach without the spelling changing, so a test about
# what the tool hears is not also a test of `utils.stems`.
STEM = ALSO_FORBIDDEN
ENDINGS = ("s", "ed", "ing", "er", "ers")


class RecordingSpeaker:
    """
    A speaker that keeps what it was asked to say instead of playing it.

    It takes a clip either way, because the real one does and the choice is the
    point: a phrase with nothing in front of it and nothing to be done to it
    arrives as something that can be had already encoded, and anything else
    arrives as a stream. `encoded` records which it was, so a test can say that
    the free path is still the free path.
    """

    def __init__(self) -> None:
        self.played: list[tuple[Source, str]] = []
        self.scales: list[float] = []
        self.encoded: list[bool] = []

    async def play(self, source, audio, scale: float = UNITY_VOLUME) -> None:
        packets = hasattr(audio, "packets") and scale == UNITY_VOLUME

        if hasattr(audio, "packets"):
            audio = audio.packets() if packets else audio.pcm()

        spoken = "".join([chunk async for chunk in audio])
        self.played.append((source, spoken))
        self.scales.append(scale)
        self.encoded.append(packets)


class BlockingSpeaker(RecordingSpeaker):
    """
    A speaker that holds the channel until it is let go.

    The real one returns when a clip has finished playing, which is what makes a
    second fine arriving mid-announcement possible at all; a speaker that returns
    immediately can never be caught busy.
    """

    def __init__(self) -> None:
        super().__init__()
        self.playing = asyncio.Event()
        self.finish = asyncio.Event()

    async def play(self, source, audio, scale: float = UNITY_VOLUME) -> None:
        self.playing.set()
        await self.finish.wait()
        await super().play(source, audio, scale)


class FakePhrase:
    """One phrase from `FakeSpeech`, in whichever form is asked for."""

    def __init__(self, speech: "FakeSpeech", text: str) -> None:
        self._speech = speech
        self._text = text

    def pcm(self):
        return self._speech._pcm(self._text)

    def packets(self):
        return self._speech._pcm(self._text)


class FakeSpeech:
    """
    Stands in for the cache, handing back the text it was asked to render.

    Clips are strings here too, so what a speaker collects is one readable
    string rather than a mixture nothing can join.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.pulled: list[str] = []
        self.warmed: list[str] = []
        self.held: set[str] = set()

        # Set by a test that cares how a phrase is paced; a phrase arrives whole
        # otherwise, which is what a cache hit looks like.
        self.chunks: tuple[str, ...] | None = None

    def stream(self, text: str, *, keep: bool = True) -> "FakePhrase":
        """
        A phrase the speaker can take either way, as the real cache hands back.

        This tool only ever asks for samples — it chains a chime in front of the
        words and plays the result quieter — but the shape has to match, or a
        test would pass against an object the tool cannot use.
        """
        self.asked.append(text)

        return FakePhrase(self, text)

    async def _pcm(self, text: str):
        for chunk in (text,) if self.chunks is None else self.chunks:
            self.pulled.append(chunk)
            yield chunk

    async def warm(self, text: str) -> bool:
        """Render a phrase unless it is already held, as the real cache does."""
        self.warmed.append(text)

        if text in self.held:
            return False

        self.held.add(text)
        return True


class FakeChimes:
    """
    Stands in for the chime library, which is a directory and nothing else.

    Clips are strings here too, so what a speaker collects is one readable
    string rather than a mixture nothing can join. The suffix is added the way
    the real one adds it, so a name written here is a name a config file could
    hold.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.asked: list[str] = []

    def path(self, name: str) -> Path:
        return self.directory / f"{name}{CHIME_SUFFIX}"

    async def clip(self, name: str) -> str:
        self.asked.append(name)
        path = self.path(name)

        return path.read_text(encoding="utf-8") if path.is_file() else ""


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


@pytest.fixture(autouse=True)
def speech(monkeypatch):
    """
    Replace the process-wide cache so nothing reaches a synthesizer.

    Autouse because the speaking tool builds one whether or not the test it is
    standing beside cares what gets rendered.
    """
    fake = FakeSpeech()
    monkeypatch.setattr(tts_tool, "shared_cache", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def chimes(monkeypatch, tmp_path):
    """
    Replace the process-wide chime library with an empty directory.

    Autouse because a tool with no chime configured still builds one, and the
    real library would resolve names against whatever the deployment mounted.
    """
    fake = FakeChimes(tmp_path)
    monkeypatch.setattr(tts_tool, "shared_chimes", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def credits(monkeypatch, tmp_path) -> CreditLedger:
    """
    A ledger of its own per test, in place of the process-wide one.

    Autouse because every board built here enrolls its roster on construction,
    and one reaching the real ledger would read whatever the deployment has on
    disk — and, worse, count into it.
    """
    ledger = CreditLedger(tmp_path / "credits.json")
    monkeypatch.setattr("miss_quote.tools.scoreboard.shared_ledger", lambda: ledger)

    return ledger


@pytest.fixture
def chime(chimes) -> str:
    """A clip sitting in the chime directory, as an operator would leave one."""
    (chimes.directory / CHIME_FILE).write_text(CHIME_AUDIO, encoding="utf-8")
    return CHIME_NAME


@pytest.fixture
def speaker() -> RecordingSpeaker:
    return RecordingSpeaker()


def _tool(
    speaker,
    config=None,
    users=None,
    server: str = SERVER_ALIAS,
    counted: bool = True,
    spoken: bool = True,
) -> VerbalMorality:
    """
    The tool, with its server's board and its server's voice beside it in one box.

    Which is what the runner builds: the fine reaches the tally and the channel
    because all three tools are that server's, not because any of them knows
    about the others' settings. `counted=False` is the server that enabled no
    board, and `spoken=False` the one that enabled nothing to say it with.
    """
    box = Toolbox()
    context = ToolContext(
        server=server,
        # `is None` rather than a falsy check: an empty config is a case under test.
        config={"words": WORDS} if config is None else config,
        speaker=speaker,
        users=users or {},
        tools=box,
    )

    if counted:
        box.add(Scoreboard(replace(context, config={}, tools=box.view(Scoreboard))))

    if spoken:
        box.add(Tts(replace(context, config={}, tools=box.view(Tts))))

    tool = VerbalMorality(replace(context, tools=box.view(VerbalMorality)))
    box.add(tool)

    return tool


def _speaking(tool: VerbalMorality) -> Tts:
    """The speaking tool beside one under test, for a test that drives it directly."""
    return tool.tools.find(Tts)


async def _render(tool: VerbalMorality) -> None:
    """
    Warm the tool up and let the renderer get to the end of the queue.

    Two steps because they are two tools: warming lines phrases up and returns,
    and rendering them is a service the runner starts separately.
    """
    await tool.prewarm()

    speaking = _speaking(tool)
    running = asyncio.create_task(speaking.run())

    try:
        await speaking.drained()
    finally:
        running.cancel()


def _utterance(
    text: str, user: str = SPEAKER, user_id: int = SPEAKER_ID
) -> Utterance:
    return Utterance(
        timestamp=datetime.now().astimezone(), user_id=user_id, user=user, text=text
    )


async def _hear(
    tool: VerbalMorality,
    text: str,
    user: str = SPEAKER,
    user_id: int = SPEAKER_ID,
) -> None:
    await tool.handle_utterance(_utterance(text, user, user_id), FakeSession(SOURCE))


# ── construction ──────────────────────────────────


def test_a_tool_with_no_words_will_not_start(speech, speaker):
    """Enabled and listening for nothing is a mistake the runner should report."""
    with pytest.raises(ValueError, match="words"):
        _tool(speaker, {})


def test_a_tool_with_an_empty_word_list_will_not_start(speech, speaker):
    with pytest.raises(ValueError, match="words"):
        _tool(speaker, {"words": ["", "  "]})


def test_a_single_word_need_not_be_a_list(speech, speaker):
    """YAML reads a lone value as a string, which is a reasonable thing to write."""
    tool = _tool(speaker, {"words": FORBIDDEN})

    assert tool._forbidden.search(f"oh {FORBIDDEN}")


def test_an_announcement_with_an_unfillable_placeholder_will_not_start(speech, speaker):
    with pytest.raises(ValueError, match="placeholder"):
        _tool(speaker, {"words": WORDS, "announcement": "{user} owes {tally}"})


def test_the_announcement_is_optional(speech, speaker):
    assert _tool(speaker)._announcement == DEFAULT_ANNOUNCEMENT


# ── detection ─────────────────────────────────────


async def test_a_forbidden_word_is_announced(speech, speaker):
    await _hear(_tool(speaker), f"oh {FORBIDDEN} that hurt")

    assert len(speaker.played) == 1


async def test_a_clean_utterance_says_nothing(speech, speaker):
    await _hear(_tool(speaker), "that should work")

    assert speaker.played == []
    assert speech.asked == []


async def test_detection_ignores_case(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN.upper())

    assert len(speaker.played) == 1


async def test_any_of_the_configured_words_counts(speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, f"absolute {ALSO_FORBIDDEN}")

    assert len(speaker.played) == 1


async def test_a_word_inside_another_word_is_not_a_violation(speech, speaker):
    """The Scunthorpe problem: a substring match fines the innocent."""
    tool = _tool(speaker, {"words": ["cuss"]})

    await _hear(tool, "we discussed it at length")

    assert speaker.played == []


async def test_punctuation_does_not_hide_a_violation(speech, speaker):
    await _hear(_tool(speaker), f"well, {FORBIDDEN}!")

    assert len(speaker.played) == 1


async def test_several_violations_in_one_utterance_earn_one_announcement(speech, speaker):
    """Stacking announcements would deny the channel to everyone in it."""
    await _hear(_tool(speaker), f"{FORBIDDEN} and {ALSO_FORBIDDEN} and {FORBIDDEN}")

    assert len(speaker.played) == 1


# ── stems ─────────────────────────────────────────


async def test_a_configured_word_is_a_stem(speech, speaker):
    """A server objects to a word in every tense it has, not just the infinitive."""
    tool = _tool(speaker, {"words": [STEM]})

    await _hear(tool, f"he {STEM}ed it up")

    assert len(speaker.played) == 1


async def test_every_common_ending_is_heard(speech, speaker):
    tool = _tool(speaker, {"words": [STEM]})

    for ending in ENDINGS:
        await _hear(tool, f"absolute {STEM}{ending}")

    assert len(speaker.played) == len(ENDINGS)


async def test_a_grown_form_costs_one_credit_like_any_other(speech, speaker):
    await _hear(_tool(speaker), f"{FORBIDDEN}ing {FORBIDDEN}er")

    assert "fined 2 credits for" in speech.asked[0]


async def test_a_stem_inside_a_longer_innocent_word_is_still_not_a_violation(
    speech, speaker
):
    tool = _tool(speaker, {"words": ["cuss"]})

    await _hear(tool, "we discussed the discussion at length")

    assert speaker.played == []


# ── the fine ──────────────────────────────────────


async def test_one_word_costs_one_credit(speech, speaker):
    await _hear(_tool(speaker), f"oh {FORBIDDEN}")

    assert "fined 1 credit for" in speech.asked[0]


async def test_each_further_word_costs_another_credit(speech, speaker):
    await _hear(_tool(speaker), f"{FORBIDDEN} and {ALSO_FORBIDDEN} and {FORBIDDEN}")

    assert "fined 3 credits for" in speech.asked[0]


async def test_the_same_word_twice_costs_twice(speech, speaker):
    """Each utterance of a forbidden word is its own violation."""
    await _hear(_tool(speaker), f"{FORBIDDEN}, {FORBIDDEN}")

    assert "fined 2 credits for" in speech.asked[0]


async def test_the_credits_are_available_to_a_custom_announcement(speech, speaker):
    tool = _tool(speaker, {"words": WORDS, "announcement": "{user} owes {credits}"})

    await _hear(tool, f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert speech.asked == [f"{SPEAKER} owes 2 credits"]


# ── the currency ──────────────────────────────────


def _denominated(monkeypatch, currency: str) -> None:
    """What a fine is counted in belongs to the board that keeps the count."""
    monkeypatch.setattr(
        verbal_morality, "scoreboard_cfg", replace(scoreboard_cfg, currency=currency)
    )


async def test_the_currency_is_what_the_deployment_calls_it(
    speech, speaker, monkeypatch
):
    _denominated(monkeypatch, "buck")

    await _hear(_tool(speaker), FORBIDDEN)

    assert "fined 1 buck for" in speech.asked[0]


async def test_a_renamed_currency_is_pluralized(speech, speaker, monkeypatch):
    _denominated(monkeypatch, "buck")

    await _hear(_tool(speaker), f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert "fined 2 bucks for" in speech.asked[0]


async def test_a_currency_is_pluralized_by_the_spelling(speech, speaker, monkeypatch):
    """The same rule the word list is grown by, so nobody is fined "2 pennys"."""
    _denominated(monkeypatch, "penny")

    await _hear(_tool(speaker), f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert "fined 2 pennies for" in speech.asked[0]


async def test_a_sibilant_currency_takes_an_es(speech, speaker, monkeypatch):
    _denominated(monkeypatch, "crash")

    await _hear(_tool(speaker), f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert "fined 2 crashes for" in speech.asked[0]


# ── the announcement ──────────────────────────────


async def test_the_speaker_is_named_in_the_fine(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    assert speech.asked == [
        f"{SPEAKER}, you are fined 1 credit for a violation of "
        "the verbal morality statute."
    ]


async def test_the_name_comes_from_the_utterance(speech, speaker):
    """Which is the roster name where a server configured one."""
    await _hear(_tool(speaker), FORBIDDEN, user="Someone Else")

    assert speech.asked[0].startswith("Someone Else,")


async def test_one_violation_is_announced_in_the_singular(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    assert "for a violation of" in speech.asked[0]


async def test_several_violations_are_announced_in_the_plural(speech, speaker):
    await _hear(_tool(speaker), f"{FORBIDDEN} and {ALSO_FORBIDDEN}")

    assert "for multiple violations of" in speech.asked[0]


async def test_the_plural_does_not_repeat_the_count(speech, speaker):
    """The number is already in the fine; twice makes it sound like an invoice."""
    await _hear(_tool(speaker), f"{FORBIDDEN} {FORBIDDEN} {FORBIDDEN}")

    assert speech.asked[0].count("3") == 1


async def test_the_violations_are_available_to_a_custom_announcement(speech, speaker):
    tool = _tool(
        speaker, {"words": WORDS, "announcement": "{user} is guilty of {violations}"}
    )

    await _hear(tool, f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert speech.asked == [f"{SPEAKER} is guilty of multiple violations"]


async def test_the_announcement_can_be_overridden(speech, speaker):
    tool = _tool(speaker, {"words": WORDS, "announcement": "language, {user}"})

    await _hear(tool, FORBIDDEN)

    assert speech.asked == [f"language, {SPEAKER}"]


async def test_the_fine_is_played_back_where_it_was_earned(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    played_source, _ = speaker.played[0]
    assert played_source == SOURCE


async def test_two_speakers_get_their_own_announcements(speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN, user="First")
    await _hear(tool, FORBIDDEN, user="Second")

    assert [text.split(",")[0] for text in speech.asked] == ["First", "Second"]


# ── the tally ─────────────────────────────────────


async def test_a_fine_comes_off_the_tally(speech, speaker, credits):
    """A fine is a debit; the number beside a name is what swearing has cost."""
    await _hear(_tool(speaker), FORBIDDEN)

    assert credits.total(SERVER_ALIAS, SPEAKER_ID) == -1


async def test_a_fine_is_still_announced_where_nothing_is_counting(speech, speaker):
    """Announcing the fine is this tool's job; keeping score is somebody else's."""
    await _hear(_tool(speaker, counted=False), FORBIDDEN)

    assert speech.asked == [_wording(SPEAKER, "1 credit", "a violation")]


async def test_a_server_with_no_scoreboard_is_told_at_startup(speech, speaker, caplog):
    """Rather than left to be discovered by wondering why the topic is empty."""
    with caplog.at_level("WARNING"):
        await _tool(speaker, counted=False).prewarm()

    assert any("No scoreboard is enabled" in record.message for record in caplog.records)


async def test_a_server_with_a_scoreboard_is_not_told_anything(speech, speaker, caplog):
    with caplog.at_level("WARNING"):
        await _tool(speaker).prewarm()

    assert caplog.records == []


async def test_the_tally_accumulates_across_utterances(speech, speaker, credits):
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, f"{FORBIDDEN} and {ALSO_FORBIDDEN}")

    assert credits.total(SERVER_ALIAS, SPEAKER_ID) == -3


async def test_a_clean_utterance_costs_nothing(speech, speaker, credits):
    await _hear(_tool(speaker), "that should work")

    assert credits.total(SERVER_ALIAS, SPEAKER_ID) == 0


async def test_the_roster_starts_on_the_board_at_nothing_spent(speech, speaker, credits):
    """So the topic says who is being watched before anybody has sworn."""
    _tool(speaker, users=ROSTER)

    assert credits.topic(SERVER_ALIAS) == f"{SPEAKER}: 0 {OTHER_SPEAKER}: 0"


async def test_the_tally_reads_as_the_channel_topic(speech, speaker, credits):
    tool = _tool(speaker, users=ROSTER)

    await _hear(tool, f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert credits.topic(SERVER_ALIAS) == f"{SPEAKER}: -2 {OTHER_SPEAKER}: 0"


async def test_somebody_off_the_roster_is_not_on_the_board(speech, speaker, credits):
    """A Discord nickname its owner can set to anything does not go in a topic."""
    tool = _tool(speaker, users=ROSTER)

    await _hear(tool, FORBIDDEN, user=STRANGER, user_id=STRANGER_ID)

    assert STRANGER not in credits.topic(SERVER_ALIAS)


async def test_somebody_off_the_roster_is_still_fined(speech, speaker, credits):
    """Ineligible for the board is not the same as unwatched."""
    tool = _tool(speaker, users=ROSTER)

    await _hear(tool, FORBIDDEN, user=STRANGER, user_id=STRANGER_ID)

    assert credits.total(SERVER_ALIAS, STRANGER_ID) == -1


async def test_somebody_off_the_roster_is_still_announced(speech, speaker):
    tool = _tool(speaker, users=ROSTER)

    await _hear(tool, FORBIDDEN, user=STRANGER, user_id=STRANGER_ID)

    assert speech.asked[0].startswith(STRANGER)


async def test_two_servers_keep_their_own_tallies(speech, speaker, credits):
    """A server's words are its own, and so is what they cost."""
    here = _tool(speaker, users=ROSTER)
    elsewhere = _tool(speaker, users=ROSTER, server=OTHER_SERVER_ALIAS)

    await _hear(here, FORBIDDEN)
    await _hear(elsewhere, f"{FORBIDDEN} {ALSO_FORBIDDEN}")

    assert credits.total(SERVER_ALIAS, SPEAKER_ID) == -1
    assert credits.total(OTHER_SERVER_ALIAS, SPEAKER_ID) == -2


# ── the backoff ───────────────────────────────────


async def test_the_first_fine_in_a_while_is_announced_at_full_volume(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    assert speaker.scales == [UNITY_VOLUME]


async def test_each_violation_takes_the_next_announcement_down(speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)

    assert speaker.scales == pytest.approx(
        [
            UNITY_VOLUME,
            UNITY_VOLUME - BACKOFF_STEP,
            UNITY_VOLUME - BACKOFF_STEP * 2,
        ]
    )


async def test_every_word_in_an_utterance_counts_toward_the_backoff(speech, speaker):
    """Four in a sentence is four steps down, however few announcements it took."""
    tool = _tool(speaker)

    await _hear(tool, f"{FORBIDDEN} {FORBIDDEN} {ALSO_FORBIDDEN}")
    await _hear(tool, FORBIDDEN)

    assert speaker.scales[1] == pytest.approx(UNITY_VOLUME - BACKOFF_STEP * 3)


async def test_one_speaker_backing_off_does_not_quieten_another(speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert speaker.scales[-1] == UNITY_VOLUME


def test_the_backoff_stops_at_the_floor():
    """However much somebody swears, the announcement does not invert itself."""
    recent = RecentViolations(floor=QUIETEST)

    recent.record(SPEAKER_ID, MANY_VIOLATIONS)

    assert recent.scale(SPEAKER_ID) == QUIETEST


def test_a_floor_of_silence_silences_a_repeat_offender():
    """Which is what a server that wants the joke to stop entirely asks for."""
    recent = RecentViolations(floor=SILENT_VOLUME)

    recent.record(SPEAKER_ID, MANY_VIOLATIONS)

    assert recent.scale(SPEAKER_ID) == SILENT_VOLUME


def test_a_violation_stops_counting_once_the_window_has_passed():
    recent = RecentViolations()
    now = 1_000.0

    recent.record(SPEAKER_ID, 1, now=now)

    assert recent.scale(SPEAKER_ID, now=now + BACKOFF_WINDOW + 1) == UNITY_VOLUME


def test_a_violation_inside_the_window_still_counts():
    recent = RecentViolations()
    now = 1_000.0

    recent.record(SPEAKER_ID, 1, now=now)

    assert recent.count(SPEAKER_ID, now=now + BACKOFF_WINDOW - 1) == 1


def test_a_speaker_who_has_aged_out_is_forgotten_entirely():
    """The map is per process and nothing sweeps it; reading is what prunes."""
    recent = RecentViolations()
    now = 1_000.0
    recent.record(SPEAKER_ID, 1, now=now)

    recent.count(SPEAKER_ID, now=now + BACKOFF_WINDOW + 1)

    assert SPEAKER_ID not in recent._seen


def test_the_backoff_reads_its_step_and_window_from_the_deployment():
    """Both are environment settings; nothing in the tool carries a number."""
    recent = RecentViolations()

    assert recent._step == morality_cfg.backoff_step
    assert recent._window == morality_cfg.backoff_seconds


# ── the repeat wording ────────────────────────────


async def test_a_first_fine_is_announced_in_full(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    assert "you are fined" in speech.asked[0]


async def test_a_second_fine_in_quick_succession_says_also(speech, speaker):
    """Reading the whole sentence again sounds like a bot that lost track."""
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)

    assert "you are also fined" in speech.asked[1]


async def test_the_repeat_wording_is_per_speaker(speech, speaker):
    tool = _tool(speaker)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert "you are also fined" not in speech.asked[1]


async def test_the_repeat_announcement_can_be_overridden(speech, speaker):
    tool = _tool(
        speaker,
        {"words": WORDS, "repeat_announcement": "{user}, again? {credits}"},
    )

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)

    assert speech.asked[1] == f"{SPEAKER}, again? 1 credit"


async def test_a_repeat_announcement_with_an_unfillable_placeholder_will_not_start(
    speech, speaker
):
    with pytest.raises(ValueError, match="repeat_announcement"):
        _tool(speaker, {"words": WORDS, "repeat_announcement": "{user} owes {tally}"})


async def test_the_repeat_announcement_is_optional(speech, speaker):
    assert _tool(speaker)._repeat_announcement == DEFAULT_REPEAT_ANNOUNCEMENT


def test_a_speaker_who_has_just_been_fined_is_repeating():
    recent = RecentViolations()
    now = 1_000.0

    recent.record(SPEAKER_ID, 1, now=now)

    assert recent.repeating(SPEAKER_ID, REPEAT_WINDOW, now=now + REPEAT_WINDOW - 1)


def test_a_speaker_fined_a_while_ago_is_not_repeating():
    """Past the window it is a fresh offence, and gets the whole sentence again."""
    recent = RecentViolations()
    now = 1_000.0

    recent.record(SPEAKER_ID, 1, now=now)

    assert not recent.repeating(SPEAKER_ID, REPEAT_WINDOW, now=now + REPEAT_WINDOW + 1)


def test_a_speaker_who_has_never_been_fined_is_not_repeating():
    assert not RecentViolations().repeating(SPEAKER_ID, REPEAT_WINDOW)


def test_a_repeat_window_of_zero_turns_the_second_wording_off():
    recent = RecentViolations()
    now = 1_000.0
    recent.record(SPEAKER_ID, 1, now=now)

    assert not recent.repeating(SPEAKER_ID, 0.0, now=now)


# ── the busy channel ──────────────────────────────


async def _mid_announcement(speech) -> tuple[VerbalMorality, BlockingSpeaker, asyncio.Task]:
    """A tool with an announcement playing and the channel held open."""
    speaker = BlockingSpeaker()
    tool = _tool(speaker)
    playing = asyncio.create_task(_hear(tool, FORBIDDEN))
    await speaker.playing.wait()

    return tool, speaker, playing


async def test_a_violation_during_an_announcement_is_not_announced(speech):
    """Queueing them would read the channel fines for things it has moved on from."""
    tool, speaker, playing = await _mid_announcement(speech)

    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    speaker.finish.set()
    await playing

    assert len(speaker.played) == 1


async def test_a_violation_during_an_announcement_is_not_even_rendered(speech):
    """Nothing is going to play it; paying a synthesizer for it would be waste."""
    tool, speaker, playing = await _mid_announcement(speech)

    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    speaker.finish.set()
    await playing

    assert len(speech.asked) == 1


async def test_a_violation_during_an_announcement_is_still_fined(speech, credits):
    """What somebody owes is not a function of whether they were told about it."""
    tool, speaker, playing = await _mid_announcement(speech)

    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    speaker.finish.set()
    await playing

    assert credits.total(SERVER_ALIAS, OTHER_SPEAKER_ID) == -1


async def test_a_violation_during_an_announcement_still_counts_toward_the_backoff(
    speech,
):
    tool, speaker, playing = await _mid_announcement(speech)
    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)
    speaker.finish.set()
    await playing

    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert speaker.scales[-1] == pytest.approx(UNITY_VOLUME - BACKOFF_STEP)


async def test_the_channel_is_free_again_once_an_announcement_ends(speech):
    tool, speaker, playing = await _mid_announcement(speech)
    speaker.finish.set()
    await playing

    await _hear(tool, FORBIDDEN, user=OTHER_SPEAKER, user_id=OTHER_SPEAKER_ID)

    assert len(speaker.played) == 2


async def test_a_speaker_that_raises_does_not_wedge_the_channel(speech, speaker):
    """The flag has to come back down however the play went."""
    tool = _tool(speaker)

    async def refuse(source, audio, scale=UNITY_VOLUME) -> None:
        raise RuntimeError("the channel is on fire")

    speaker.play = refuse
    with pytest.raises(RuntimeError):
        await _hear(tool, FORBIDDEN)

    assert not tool._announcing


# ── the chime ─────────────────────────────────────


async def test_no_chime_is_played_when_none_is_configured(speech, chimes, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    _, spoken = speaker.played[0]
    assert chimes.asked == []
    assert spoken.startswith(SPEAKER)


async def test_a_configured_chime_leads_the_announcement(speech, speaker, chime):
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    await _hear(tool, FORBIDDEN)

    _, spoken = speaker.played[0]
    assert spoken == CHIME_AUDIO + speech.asked[0]


async def test_the_chime_and_the_words_are_one_clip(speech, speaker, chime):
    """Two calls to the speaker would put an audible gap between them."""
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    await _hear(tool, FORBIDDEN)

    assert len(speaker.played) == 1


async def test_a_missing_chime_still_announces_the_fine(speech, speaker):
    tool = _tool(speaker, {"words": WORDS, "chime": "not-there"})

    await _hear(tool, FORBIDDEN)

    _, spoken = speaker.played[0]
    assert spoken == speech.asked[0]


async def test_an_empty_chime_is_the_same_as_none(speech, chimes, speaker):
    tool = _tool(speaker, {"words": WORDS, "chime": "  "})

    await _hear(tool, FORBIDDEN)

    assert chimes.asked == []


# ── the head start ────────────────────────────────


async def _first(clip) -> str:
    """
    The first piece of a clip, and no more.

    Abandoned rather than drained, so what the stream has given up by then is
    what it gave up before playback started.
    """
    leading = await anext(clip)
    await clip.aclose()

    return leading


async def test_the_words_are_waited_for_before_the_chime(speech, speaker, chime):
    """A chime that starts ahead of the speech leaves a gap in the middle."""
    speech.chunks = CHUNKS
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    leading = await _first(_speaking(tool)._announce(DEFAULT_ANNOUNCEMENT, chime))

    assert leading == CHIME_AUDIO
    assert speech.pulled == list(CHUNKS)


async def test_no_head_start_plays_on_the_first_chunk(speech, speaker, chime, monkeypatch):
    """A synthesizer that streams as it renders needs nothing held back."""
    monkeypatch.setattr(tts_tool, "tts_cfg", replace(tts_cfg, lead_ms=NO_HEAD_START))
    speech.chunks = CHUNKS
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    leading = await _first(_speaking(tool)._announce(DEFAULT_ANNOUNCEMENT, chime))

    assert leading == CHIME_AUDIO
    assert speech.pulled == []


async def test_the_head_start_does_not_reorder_the_announcement(speech, speaker, chime):
    speech.chunks = CHUNKS
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    await _hear(tool, FORBIDDEN)

    _, spoken = speaker.played[0]
    assert spoken == CHIME_AUDIO + "".join(CHUNKS)


# ── the pre-warm ──────────────────────────────────


def _wording(
    user: str, credits: str, violations: str, repeat: bool = False
) -> str:
    template = DEFAULT_REPEAT_ANNOUNCEMENT if repeat else DEFAULT_ANNOUNCEMENT

    return template.format(user=user, credits=credits, violations=violations)


async def test_every_name_on_the_roster_is_warmed(speech, speaker):
    tool = _tool(speaker, users=ROSTER)

    await _render(tool)

    assert len(speech.warmed) == len(ROSTER) * WARMED_PER_SPEAKER
    assert {wording.split(",")[0] for wording in speech.warmed} == {
        SPEAKER,
        OTHER_SPEAKER,
    }


async def test_a_speaker_is_warmed_for_one_two_and_three_violations(speech, speaker):
    """What a sentence usually holds; past that they can wait for the synthesizer."""
    tool = _tool(speaker, users={SPEAKER_ID: SPEAKER})

    await _render(tool)

    assert speech.warmed == [
        _wording(SPEAKER, "1 credit", "a violation"),
        _wording(SPEAKER, "1 credit", "a violation", REPEATED_FINE),
        _wording(SPEAKER, "2 credits", "multiple violations"),
        _wording(SPEAKER, "2 credits", "multiple violations", REPEATED_FINE),
        _wording(SPEAKER, "3 credits", "multiple violations"),
        _wording(SPEAKER, "3 credits", "multiple violations", REPEATED_FINE),
    ]


async def test_the_repeat_wording_is_warmed_too(speech, speaker):
    """A speaker who swears twice in five seconds should not wait for the second."""
    tool = _tool(speaker, users={SPEAKER_ID: SPEAKER})
    await _render(tool)

    await _hear(tool, FORBIDDEN)
    await _hear(tool, FORBIDDEN)

    assert speech.asked[1] in speech.warmed


async def test_a_warmed_announcement_is_exactly_what_gets_said(speech, speaker):
    """A phrase differing by a space is one that gets synthesized twice."""
    tool = _tool(speaker, users={SPEAKER_ID: SPEAKER})
    await _render(tool)

    await _hear(tool, f"{FORBIDDEN} and {ALSO_FORBIDDEN}")

    assert speech.asked[0] in speech.warmed


async def test_a_custom_announcement_is_what_is_warmed(speech, speaker):
    tool = _tool(
        speaker,
        {
            "words": WORDS,
            "announcement": "language, {user}",
            "repeat_announcement": "language again, {user}",
        },
        users={SPEAKER_ID: SPEAKER},
    )

    await _render(tool)

    # An announcement that names no count is the same sentence however many
    # violations earned it, and the same sentence is rendered once.
    assert speech.warmed == [f"language, {SPEAKER}", f"language again, {SPEAKER}"]


async def test_a_speaker_who_is_not_on_the_roster_is_not_warmed(speech, speaker):
    """Their Discord name is not knowable from here, and not a closed set."""
    tool = _tool(speaker, users={SPEAKER_ID: SPEAKER})
    await _render(tool)

    await _hear(tool, FORBIDDEN, user="Someone Else")

    assert speech.asked[0] not in speech.warmed


async def test_an_empty_roster_warms_nothing(speech, speaker):
    await _render(_tool(speaker))

    assert speech.warmed == []


async def test_one_name_under_two_ids_is_warmed_once(speech, speaker):
    """Two IDs and one name is one phrase, however it got written down."""
    tool = _tool(speaker, users={SPEAKER_ID: SPEAKER, OTHER_SPEAKER_ID: SPEAKER})

    await _render(tool)

    assert len(speech.warmed) == WARMED_PER_SPEAKER


async def test_warming_plays_nothing(speech, speaker):
    """It is preparation, not an announcement; nobody has earned one yet."""
    await _render(_tool(speaker, users=ROSTER))

    assert speaker.played == []
    assert speech.asked == []


async def test_the_runner_warms_a_configured_server(speech, speaker):
    """
    The seam the rest of these skip past.

    A tool is handed the roster by the runner, from the server's own config
    rather than the tool's, and warmed once the bot is up.
    """
    servers = {
        SOURCE.guild_id: ServerConfig(
            alias=SERVER_ALIAS,
            users=ROSTER,
            tools={
                VerbalMorality.name: ToolSettings(enabled=True, config={"words": WORDS}),
                Tts.name: ToolSettings(enabled=True, config={}),
            },
        )
    }
    registry = {VerbalMorality.name: VerbalMorality, Tts.name: Tts}
    runner = ToolRunner(servers, registry, speaker)

    await runner.prewarm()
    running = runner.start()
    try:
        await _drained(runner)
    finally:
        for task in running:
            task.cancel()

    assert runner.problems == []
    assert len(speech.warmed) == len(ROSTER) * WARMED_PER_SPEAKER


async def _drained(runner: ToolRunner) -> None:
    """Wait out every renderer the runner started."""
    for tool in runner._serving:
        if isinstance(tool, Tts):
            await tool.drained()


async def test_the_runner_wires_a_fine_to_the_board(speech, speaker, credits):
    """
    The other seam: two tools on one server, and a fine that reaches the tally.

    Built the way a deployment builds them — from the config file, in the order
    it happens to list them — rather than handed to each other by a test.
    """
    servers = {
        SOURCE.guild_id: ServerConfig(
            alias=SERVER_ALIAS,
            users=ROSTER,
            tools={
                VerbalMorality.name: ToolSettings(enabled=True, config={"words": WORDS}),
                Scoreboard.name: ToolSettings(enabled=True, config={}),
                Tts.name: ToolSettings(enabled=True, config={}),
            },
        )
    }
    registry = {
        VerbalMorality.name: VerbalMorality,
        Scoreboard.name: Scoreboard,
        Tts.name: Tts,
    }
    runner = ToolRunner(servers, registry, speaker)

    await runner.dispatch_utterance(FakeSession(SOURCE), _utterance(FORBIDDEN))

    assert runner.problems == []
    assert credits.total(SERVER_ALIAS, SPEAKER_ID) == -1
