"""What the Verbal Morality Bot hears, and what it says about it."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import tools.verbal_morality as verbal_morality
from config import tts_cfg
from tools.verbal_morality import DEFAULT_ANNOUNCEMENT, VerbalMorality, _lead
from transcript.writer import Source, Utterance

SERVER_ALIAS = "first-server"
SPEAKER = "Speaker One"

CHIME_NAME = "chime.wav"
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
    """A speaker that keeps what it was asked to say instead of playing it."""

    def __init__(self) -> None:
        self.played: list[tuple[Source, str]] = []

    async def play(self, source, audio) -> None:
        spoken = "".join([chunk async for chunk in audio])
        self.played.append((source, spoken))


class FakeSpeech:
    """
    Stands in for the cache, handing back the text it was asked to render.

    Clips are strings here too, so what a speaker collects is one readable
    string rather than a mixture nothing can join.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.asked: list[str] = []
        self.clips_asked: list[str] = []
        self.pulled: list[str] = []

        # Set by a test that cares how a phrase is paced; a phrase arrives whole
        # otherwise, which is what a cache hit looks like.
        self.chunks: tuple[str, ...] | None = None

    async def stream(self, text: str):
        self.asked.append(text)

        for chunk in (text,) if self.chunks is None else self.chunks:
            self.pulled.append(chunk)
            yield chunk

    def clip_path(self, name: str) -> Path:
        return self.directory / name

    async def clip(self, name: str) -> str:
        self.clips_asked.append(name)
        path = self.clip_path(name)

        return path.read_text(encoding="utf-8") if path.is_file() else ""


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


@pytest.fixture
def speech(monkeypatch, tmp_path):
    """Replace the process-wide cache so nothing reaches a synthesizer."""
    fake = FakeSpeech(tmp_path)
    monkeypatch.setattr("tools.verbal_morality.shared_cache", lambda: fake)
    return fake


@pytest.fixture
def chime(speech) -> str:
    """A clip sitting in the cache directory, as an operator would leave one."""
    (speech.directory / CHIME_NAME).write_text(CHIME_AUDIO, encoding="utf-8")
    return CHIME_NAME


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


# ── the chime ─────────────────────────────────────


async def test_no_chime_is_played_when_none_is_configured(speech, speaker):
    await _hear(_tool(speaker), FORBIDDEN)

    _, spoken = speaker.played[0]
    assert speech.clips_asked == []
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
    tool = _tool(speaker, {"words": WORDS, "chime": "not-there.wav"})

    await _hear(tool, FORBIDDEN)

    _, spoken = speaker.played[0]
    assert spoken == speech.asked[0]


async def test_an_empty_chime_is_the_same_as_none(speech, speaker):
    tool = _tool(speaker, {"words": WORDS, "chime": "  "})

    await _hear(tool, FORBIDDEN)

    assert speech.clips_asked == []


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

    leading = await _first(tool._announce(DEFAULT_ANNOUNCEMENT))

    assert leading == CHIME_AUDIO
    assert speech.pulled == list(CHUNKS)


async def test_no_head_start_plays_on_the_first_chunk(speech, speaker, chime, monkeypatch):
    """A synthesizer that streams as it renders needs nothing held back."""
    monkeypatch.setattr(
        verbal_morality, "tts_cfg", replace(tts_cfg, lead_ms=NO_HEAD_START)
    )
    speech.chunks = CHUNKS
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    leading = await _first(tool._announce(DEFAULT_ANNOUNCEMENT))

    assert leading == CHIME_AUDIO
    assert speech.pulled == []


async def test_the_head_start_does_not_reorder_the_announcement(speech, speaker, chime):
    speech.chunks = CHUNKS
    tool = _tool(speaker, {"words": WORDS, "chime": chime})

    await _hear(tool, FORBIDDEN)

    _, spoken = speaker.played[0]
    assert spoken == CHIME_AUDIO + "".join(CHUNKS)


async def test_a_head_start_stops_once_it_has_enough(speech):
    speech.chunks = CHUNKS
    words = speech.stream(DEFAULT_ANNOUNCEMENT)

    held = await _lead(words, len(CHUNKS[0]) + 1)

    assert held == [CHUNKS[0], CHUNKS[1]]
    assert [chunk async for chunk in words] == [CHUNKS[2]]


async def test_a_phrase_shorter_than_the_head_start_is_not_waited_on(speech):
    """The stream ends; there is no more coming however much was asked for."""
    speech.chunks = CHUNKS
    words = speech.stream(DEFAULT_ANNOUNCEMENT)

    held = await _lead(words, len("".join(CHUNKS)) + 1)

    assert held == list(CHUNKS)
