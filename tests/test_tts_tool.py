"""The tool that speaks: what it hands the player, and what it renders in advance."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import miss_quote.tools.tts as tts_tool
from miss_quote.audio.chimes import CHIME_SUFFIX
from miss_quote.config import UNITY_VOLUME, tts_cfg
from miss_quote.tools.base import ToolContext, Toolbox
from miss_quote.tools.tts import Tts
from miss_quote.transcript.writer import Source

SERVER_ALIAS = "first-server"

SOURCE = Source(
    guild_id=1, guild_alias=SERVER_ALIAS, channel_id=2, channel="general-voice"
)

WORDS = "you are fined one credit"
OTHER_WORDS = "you are also fined one credit"

CHIME_NAME = "chime"
CHIME_FILE = f"{CHIME_NAME}{CHIME_SUFFIX}"
CHIME_AUDIO = "♪"

HOLD_NAME = "hold"
HOLD_FILE = f"{HOLD_NAME}{CHIME_SUFFIX}"
HOLD_AUDIO = "♫"

# One frame of the loop and the way out of it. The envelope over real samples is
# `test_hold`'s; what these are for is reading the order back off a string.
HOLD_FRAME = "~"
HOLD_FADE = "…"

# What the model came back with, and what it said instead of coming back.
LATE_WORDS = "it was a quiet night"
BROKEN = "the model is on fire"

# A phrase in pieces, for the tests about what is held back before playback.
CHUNKS = ("one", "two", "three")
NO_HEAD_START = 0

QUIETER = 0.5

# A phrase the fake synthesizer refuses, so a failed render is a case rather
# than an accident.
UNSAYABLE = "☠"


class RecordingSpeaker:
    """
    A speaker that keeps what it was asked to say instead of playing it.

    It takes a clip either way, because the real one does and which one it got
    is the thing under test: a clip that can be had already encoded is one
    Discord is sent untouched.
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


class FakePhrase:
    """One phrase from `FakeSpeech`, in whichever form is asked for."""

    def __init__(self, speech: "FakeSpeech", text: str) -> None:
        self._speech = speech
        self._text = text

    def pcm(self):
        return self._speech._chunks(self._text)

    def packets(self):
        return self._speech._chunks(self._text)


class FakeSpeech:
    """
    Stands in for the cache, handing back the text it was asked to render.

    Clips are strings here, so what a speaker collects is one readable string
    rather than a mixture nothing can join.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.pulled: list[str] = []
        self.warmed: list[str] = []
        self.held: set[str] = set()

        # Set by a test that cares how a phrase is paced; a phrase arrives whole
        # otherwise, which is what a cache hit looks like.
        self.chunks: tuple[str, ...] | None = None

    def stream(self, text: str, *, keep: bool = True) -> FakePhrase:
        self.asked.append(text)

        return FakePhrase(self, text)

    async def _chunks(self, text: str):
        for chunk in (text,) if self.chunks is None else self.chunks:
            self.pulled.append(chunk)
            yield chunk

    async def warm(self, text: str) -> bool:
        """Render a phrase unless it is already held, as the real cache does."""
        if text == UNSAYABLE:
            raise RuntimeError("the synthesizer is on fire")

        self.warmed.append(text)

        if text in self.held:
            return False

        self.held.add(text)
        return True


class FakeChimes:
    """Stands in for the chime library, which is a directory and nothing else."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.asked: list[str] = []

    def path(self, name: str) -> Path:
        return self.directory / f"{name}{CHIME_SUFFIX}"

    async def clip(self, name: str) -> str:
        self.asked.append(name)
        path = self.path(name)

        return path.read_text(encoding="utf-8") if path.is_file() else ""


class FakeHoldMusic:
    """
    Stands in for the envelope, so a wait reads back as one frame per stretch.

    The real one is paced against a wall clock and would make every test that
    covers a wait take as long as the wait. What is under test here is the order
    the pieces arrive in and what the music is played at, both of which survive
    a loop of exactly one frame.
    """

    def __init__(self, clip, volume=None, **_) -> None:
        self.clip = clip
        self.volume = volume

    @property
    def playable(self) -> bool:
        return bool(self.clip)

    async def until(self, finished):
        while self.playable and not finished.done():
            yield HOLD_FRAME
            await asyncio.wait([finished])

    async def fading_out(self):
        if self.playable:
            yield HOLD_FADE


@pytest.fixture(autouse=True)
def music(monkeypatch) -> list[FakeHoldMusic]:
    """Every performance the tool set going, so a test can read what it asked for."""
    built: list[FakeHoldMusic] = []

    def build(clip, volume=None, **rest):
        performance = FakeHoldMusic(clip, volume, **rest)
        built.append(performance)
        return performance

    monkeypatch.setattr(tts_tool, "HoldMusic", build)

    return built


@pytest.fixture
def hold(chimes) -> str:
    """A hold clip sitting in the chime directory, beside the flourishes."""
    (chimes.directory / HOLD_FILE).write_text(HOLD_AUDIO, encoding="utf-8")
    return HOLD_NAME


@pytest.fixture(autouse=True)
def speech(monkeypatch) -> FakeSpeech:
    """Replace the process-wide cache so nothing reaches a synthesizer."""
    fake = FakeSpeech()
    monkeypatch.setattr(tts_tool, "shared_cache", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def chimes(monkeypatch, tmp_path) -> FakeChimes:
    """Replace the process-wide chime library with a directory of this test's own."""
    fake = FakeChimes(tmp_path)
    monkeypatch.setattr(tts_tool, "shared_chimes", lambda: fake)
    return fake


@pytest.fixture
def speaker() -> RecordingSpeaker:
    return RecordingSpeaker()


@pytest.fixture
def chime(chimes) -> str:
    """A clip sitting in the chime directory, as an operator would leave one."""
    (chimes.directory / CHIME_FILE).write_text(CHIME_AUDIO, encoding="utf-8")
    return CHIME_NAME


def _tool(speaker=None) -> Tts:
    return Tts(
        ToolContext(
            server=SERVER_ALIAS,
            speaker=RecordingSpeaker() if speaker is None else speaker,
            tools=Toolbox(),
        )
    )


async def _rendered(tool: Tts) -> None:
    """Run the renderer until it has got to the end of what is queued."""
    running = asyncio.create_task(tool.run())

    try:
        await tool.drained()
    finally:
        running.cancel()


# ── playing ───────────────────────────────────────


async def test_a_phrase_is_played_where_it_was_asked_for(speaker):
    await _tool(speaker).play(SOURCE, WORDS)

    played_source, spoken = speaker.played[0]
    assert played_source is SOURCE
    assert spoken == WORDS


async def test_a_phrase_on_its_own_is_sent_as_it_was_stored(speaker):
    """
    The free path, and the reason the cache stores what Discord takes.

    Nothing in front of it and nothing to be done to it means no decode, no
    encode, and no resample between the file and the wire.
    """
    await _tool(speaker).play(SOURCE, WORDS)

    assert speaker.encoded == [True]


async def test_a_quieter_phrase_is_handed_over_as_samples(speaker):
    """A gain is a multiplication, and there is nothing to multiply in a packet."""
    await _tool(speaker).play(SOURCE, WORDS, scale=QUIETER)

    assert speaker.encoded == [False]


async def test_the_scale_reaches_the_speaker(speaker):
    """How loud a deployment is stays the speaker's; this only says how much less."""
    await _tool(speaker).play(SOURCE, WORDS, scale=QUIETER)

    assert speaker.scales == [QUIETER]


async def test_a_phrase_is_played_at_full_volume_by_default(speaker):
    await _tool(speaker).play(SOURCE, WORDS)

    assert speaker.scales == [UNITY_VOLUME]


# ── the chime ─────────────────────────────────────


async def test_a_chime_leads_the_words(speaker, chime):
    await _tool(speaker).play(SOURCE, WORDS, chime=chime)

    _, spoken = speaker.played[0]
    assert spoken == CHIME_AUDIO + WORDS


async def test_a_chime_and_the_words_are_one_clip(speaker, chime):
    """Two calls to the speaker would put an audible gap between them."""
    await _tool(speaker).play(SOURCE, WORDS, chime=chime)

    assert len(speaker.played) == 1


async def test_a_chime_forces_the_sample_path(speaker, chime):
    """There is nothing to join an encoded packet onto."""
    await _tool(speaker).play(SOURCE, WORDS, chime=chime)

    assert speaker.encoded == [False]


async def test_no_chime_is_fetched_when_none_is_named(speaker, chimes):
    await _tool(speaker).play(SOURCE, WORDS)

    assert chimes.asked == []


async def test_a_missing_chime_costs_the_chime_and_not_the_words(speaker):
    """It is the opening flourish; whatever it introduces is the part that matters."""
    await _tool(speaker).play(SOURCE, WORDS, chime="never-mounted")

    _, spoken = speaker.played[0]
    assert spoken == WORDS


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


async def test_the_words_are_waited_for_before_the_chime(speech, chime):
    """A chime that starts ahead of the speech leaves a gap in the middle."""
    speech.chunks = CHUNKS

    leading = await _first(_tool()._announce(WORDS, chime))

    assert leading == CHIME_AUDIO
    assert speech.pulled == list(CHUNKS)


async def test_no_head_start_plays_on_the_first_chunk(speech, chime, monkeypatch):
    """A synthesizer that streams as it renders needs nothing held back."""
    monkeypatch.setattr(tts_tool, "tts_cfg", replace(tts_cfg, lead_ms=NO_HEAD_START))
    speech.chunks = CHUNKS

    leading = await _first(_tool()._announce(WORDS, chime))

    assert leading == CHIME_AUDIO
    assert speech.pulled == []


async def test_the_head_start_does_not_reorder_the_clip(speech, speaker, chime):
    speech.chunks = CHUNKS

    await _tool(speaker).play(SOURCE, WORDS, chime=chime)

    _, spoken = speaker.played[0]
    assert spoken == CHIME_AUDIO + "".join(CHUNKS)


async def test_a_head_start_stops_once_it_has_enough(speech):
    speech.chunks = CHUNKS
    words = speech.stream(WORDS).pcm()

    held = await tts_tool._lead(words, len(CHUNKS[0]) + 1)

    assert held == [CHUNKS[0], CHUNKS[1]]
    assert [chunk async for chunk in words] == [CHUNKS[2]]


async def test_a_phrase_shorter_than_the_head_start_is_not_waited_on(speech):
    """The stream ends; there is no more coming however much was asked for."""
    speech.chunks = CHUNKS
    words = speech.stream(WORDS).pcm()

    held = await tts_tool._lead(words, len("".join(CHUNKS)) + 1)

    assert held == list(CHUNKS)


# ── holding ───────────────────────────────────────


def _pending() -> asyncio.Future:
    """A sentence somebody is still working out."""
    return asyncio.get_running_loop().create_future()


async def test_the_music_covers_the_wait_and_the_words_follow_it(speaker, hold):
    words = _pending()
    playing = asyncio.create_task(
        _tool(speaker).play_held(SOURCE, words, hold=hold, keep=False)
    )

    await asyncio.sleep(0)
    words.set_result(LATE_WORDS)
    await playing

    _, spoken = speaker.played[0]
    assert spoken == HOLD_FRAME + HOLD_FRAME + HOLD_FADE + LATE_WORDS


async def test_the_music_covers_the_synthesizer_as_well_as_the_model(
    speech, speaker, hold
):
    """
    The second wait, and the one nothing else would have covered: a sentence
    that arrives instantly still has to be rendered before there is anything to
    play, and the music is what is over that too.
    """
    speech.chunks = CHUNKS
    words = _pending()
    words.set_result(LATE_WORDS)

    await _tool(speaker).play_held(SOURCE, words, hold=hold, keep=False)

    _, spoken = speaker.played[0]
    assert spoken == HOLD_FRAME + HOLD_FADE + "".join(CHUNKS)


async def test_the_music_is_played_at_its_own_loudness(speaker, hold, music):
    """
    The channel's loudness is the sentence's, and the music is a fraction of it.
    Two settings in two places, because only one of them reaches the speaker.
    """
    words = _pending()
    words.set_result(LATE_WORDS)

    await _tool(speaker).play_held(SOURCE, words, hold=hold, hold_volume=QUIETER)

    assert music[0].volume == QUIETER
    assert speaker.scales == [UNITY_VOLUME]


async def test_nothing_to_hold_with_is_the_wait_there_always_was(speaker, chimes):
    """
    Unset is the default, and the default has to cost nothing. A sentence with
    no music under it is handed over exactly as `play` would hand it over,
    encoded path and all.
    """
    words = _pending()
    words.set_result(LATE_WORDS)

    await _tool(speaker).play_held(SOURCE, words)

    assert speaker.played == [(SOURCE, LATE_WORDS)]
    assert speaker.encoded == [True]
    assert chimes.asked == []


async def test_a_missing_hold_clip_costs_the_music_and_not_the_answer(speaker):
    words = _pending()
    words.set_result(LATE_WORDS)

    await _tool(speaker).play_held(SOURCE, words, hold="never-mounted")

    assert speaker.played == [(SOURCE, LATE_WORDS)]


async def test_a_silent_wait_is_waited_out_before_the_player_is_armed(speaker):
    """
    The player is armed the moment the speaker is handed a clip, and gives up on
    one that has produced nothing for `stall_seconds`. With music there is
    always something to feed it; without, the wait has to happen first.
    """
    words = _pending()
    playing = asyncio.create_task(_tool(speaker).play_held(SOURCE, words))

    await asyncio.sleep(0)
    assert speaker.played == []

    words.set_result(LATE_WORDS)
    await playing

    assert speaker.played == [(SOURCE, LATE_WORDS)]


async def test_a_sentence_that_never_arrives_ends_the_music_rather_than_cutting_it():
    """What went wrong is the caller's to report; the channel is owed an ending."""
    tool = _tool()
    words = _pending()
    played: list[str] = []

    async def collect() -> None:
        async for frame in tool._holding(FakeHoldMusic(HOLD_AUDIO), words, keep=False):
            played.append(frame)

    collecting = asyncio.create_task(collect())

    await asyncio.sleep(0)
    words.set_exception(RuntimeError(BROKEN))

    with pytest.raises(RuntimeError, match=BROKEN):
        await collecting

    assert played == [HOLD_FRAME, HOLD_FADE]


# ── rendering in advance ──────────────────────────


async def test_a_queued_phrase_is_rendered(speech):
    tool = _tool()
    tool.enqueue([WORDS])

    await _rendered(tool)

    assert speech.warmed == [WORDS]


async def test_everything_queued_is_rendered(speech):
    tool = _tool()
    tool.enqueue([WORDS, OTHER_WORDS])

    await _rendered(tool)

    assert speech.warmed == [WORDS, OTHER_WORDS]


async def test_the_same_phrase_twice_is_queued_once(speech):
    """Two servers with a name in common ask for the same sentence."""
    tool = _tool()

    assert tool.enqueue([WORDS, WORDS]) == 1

    await _rendered(tool)

    assert speech.warmed == [WORDS]


async def test_queueing_says_how_much_of_it_was_new():
    tool = _tool()
    tool.enqueue([WORDS])

    assert tool.enqueue([WORDS, OTHER_WORDS]) == 1


async def test_queueing_renders_nothing_on_its_own(speech):
    """It is a list, not a synthesis; what happens to it is the renderer's."""
    _tool().enqueue([WORDS])

    assert speech.warmed == []


async def test_queueing_plays_nothing(speaker):
    tool = _tool(speaker)
    tool.enqueue([WORDS])

    await _rendered(tool)

    assert speaker.played == []


async def test_a_phrase_that_will_not_render_does_not_end_the_run(speech, caplog):
    """Nothing is waiting on any of it; the next phrase should still get its turn."""
    tool = _tool()
    tool.enqueue([UNSAYABLE, WORDS])

    await _rendered(tool)

    assert speech.warmed == [WORDS]
    assert "Could not render" in caplog.text


async def test_a_phrase_queued_after_the_renderer_started_is_still_rendered(speech):
    """The list is not complete when the run begins; a tool may think of one later."""
    tool = _tool()
    running = asyncio.create_task(tool.run())

    try:
        tool.enqueue([WORDS])
        await tool.drained()
        tool.enqueue([OTHER_WORDS])
        await tool.drained()
    finally:
        running.cancel()

    assert speech.warmed == [WORDS, OTHER_WORDS]


# ── locating a chime ──────────────────────────────


def test_a_chime_that_is_there_is_kept(chime):
    assert _tool().locate(chime) == CHIME_NAME


def test_a_chime_that_is_missing_is_reported_and_kept(caplog):
    """The file may yet arrive in a directory that is usually a mounted volume."""
    assert _tool().locate("never-mounted") == "never-mounted"
    assert "No chime" in caplog.text


def test_no_chime_configured_locates_nothing():
    assert _tool().locate(None) is None


def test_a_blank_chime_locates_nothing():
    """A setting left empty is one nobody filled in, not a clip called nothing."""
    assert _tool().locate("   ") is None
