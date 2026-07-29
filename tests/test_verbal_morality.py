"""What the Verbal Morality Bot hears, and what it says about it."""

from datetime import datetime

import pytest

from tools.verbal_morality import DEFAULT_ANNOUNCEMENT, VerbalMorality
from transcript.writer import Source, Utterance

SERVER_ALIAS = "first-server"
SPEAKER = "Speaker One"

SOURCE = Source(
    guild_id=1, guild_alias=SERVER_ALIAS, channel_id=2, channel="general-voice"
)

FORBIDDEN = "fiddlesticks"
ALSO_FORBIDDEN = "poppycock"
WORDS = [FORBIDDEN, ALSO_FORBIDDEN]


class RecordingSpeaker:
    """A speaker that keeps what it was asked to say instead of playing it."""

    def __init__(self) -> None:
        self.played: list[tuple[Source, str]] = []

    async def play(self, source, audio) -> None:
        spoken = "".join([chunk async for chunk in audio])
        self.played.append((source, spoken))


class FakeSpeech:
    """Stands in for the cache, handing back the text it was asked to render."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def stream(self, text: str):
        self.asked.append(text)
        yield text


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


@pytest.fixture
def speech(monkeypatch):
    """Replace the process-wide cache so nothing reaches a synthesizer."""
    fake = FakeSpeech()
    monkeypatch.setattr("tools.verbal_morality.shared_cache", lambda: fake)
    return fake


@pytest.fixture
def speaker() -> RecordingSpeaker:
    return RecordingSpeaker()


def _tool(speaker, config=None) -> VerbalMorality:
    # `is None` rather than a falsy check: an empty config is a case under test.
    return VerbalMorality(
        server=SERVER_ALIAS,
        config={"words": WORDS} if config is None else config,
        speaker=speaker,
    )


def _utterance(text: str, user: str = SPEAKER) -> Utterance:
    return Utterance(
        timestamp=datetime.now().astimezone(), user_id=1, user=user, text=text
    )


async def _hear(tool: VerbalMorality, text: str, user: str = SPEAKER) -> None:
    await tool.handle_utterance(_utterance(text, user), FakeSession(SOURCE))


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
        _tool(speaker, {"words": WORDS, "announcement": "{user} owes {credits}"})


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


# ── the announcement ──────────────────────────────


async def test_the_speaker_is_named_in_the_fine(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    assert speech.asked == [
        f"{SPEAKER}, you are fined one credit for a violation of "
        "the verbal morality statute."
    ]


async def test_the_name_comes_from_the_utterance(speech, speaker):
    """Which is the roster name where a server configured one."""
    await _hear(_tool(speaker), FORBIDDEN, user="Someone Else")

    assert speech.asked[0].startswith("Someone Else,")


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
