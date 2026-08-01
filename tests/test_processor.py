import asyncio
import json

import pytest

import miss_quote.stt.processor as processor_module
from miss_quote.config import vad_cfg
from miss_quote.stt.processor import STTProcessor
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.schedule import ALWAYS
from miss_quote.transcript.writer import Source, TranscriptSession, TranscriptWriter

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
SOURCE = Source(
    guild_id=987654321, guild_alias="first-server", channel_id=456, channel="general-voice"
)

ALICE = (101, "alice")
BOB = (202, "bob")


class ScriptedIterator:
    """
    Stands in for the Silero iterator so tests drive segmentation directly.

    Speech detection itself is covered in test_vad; what matters here is what
    the processor does with the edges.
    """

    def __init__(self) -> None:
        self.triggered = False
        self.resets = 0

    def __call__(self, frame, return_seconds: bool = False):
        return None

    def reset_states(self) -> None:
        self.resets += 1
        self.triggered = False


class ScriptedVAD:
    def __init__(self) -> None:
        self.iterators: list[ScriptedIterator] = []

    def create_iterator(self) -> ScriptedIterator:
        iterator = ScriptedIterator()
        self.iterators.append(iterator)
        return iterator

    @staticmethod
    def frame_to_array(frame_bytes: bytes):
        return frame_bytes


@pytest.fixture
def transcripts(monkeypatch):
    """Capture what would have gone to Wyoming, and what it returns."""
    calls: list[bytes] = []
    replies: dict[str, str] = {"text": "hello there"}

    async def fake_transcribe(pcm: bytes):
        calls.append(pcm)
        await asyncio.sleep(0)
        return replies["text"]

    monkeypatch.setattr(processor_module, "transcribe", fake_transcribe)
    return calls, replies


@pytest.fixture
async def build(monkeypatch, tmp_path, transcripts):
    monkeypatch.setattr(processor_module, "SileroVAD", ScriptedVAD)
    started: list[STTProcessor] = []

    def _build(tools: ToolRunner | None = None) -> tuple[STTProcessor, TranscriptSession]:
        writer = TranscriptWriter(
            directory=tmp_path,
            timezone=TIMEZONE,
            retention_days=KEEP_FOREVER,
            schedules=lambda guild_id, channel: ALWAYS,
        )
        session = writer.open(SOURCE)
        processor = STTProcessor(tools or ToolRunner({}, {}))
        processor.start(asyncio.get_running_loop())
        started.append(processor)
        return processor, session

    yield _build

    for processor in started:
        await processor.stop()


def _speak(
    processor: STTProcessor,
    session: TranscriptSession,
    speaker: tuple[int, str],
    frames: int,
) -> None:
    user_id, name = speaker
    processor._feed(user_id, name, session, b"\x00" * (vad_cfg.frame_bytes * frames))


def _trigger(processor: STTProcessor, speaker: tuple[int, str], on: bool) -> None:
    state = processor._users.get_or_create(speaker[0])
    state.vad_iterator.triggered = on


def _lines(tmp_path) -> list[dict]:
    lines = []
    for path in sorted(tmp_path.rglob("*.jsonl")):
        lines += [json.loads(line) for line in path.read_text().splitlines()]
    return lines


async def test_utterance_reaches_the_transcript(build, tmp_path, transcripts):
    processor, session = build()
    calls, _ = transcripts

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)

    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=1)

    await processor.drain()

    assert len(calls) == 1
    assert _lines(tmp_path) == [
        {
            "ts": _lines(tmp_path)[0]["ts"],
            "user_id": ALICE[0],
            "user": ALICE[1],
            "text": "hello there",
        }
    ]

    written = list(tmp_path.rglob("*.jsonl"))
    assert len(written) == 1
    assert written[0].relative_to(tmp_path).parent == SOURCE.relative_directory


async def test_pre_roll_is_prepended_on_speech_onset(build, transcripts):
    """Silence frames buffered before onset must lead the utterance."""
    processor, session = build()
    calls, _ = transcripts

    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=3)  # accumulates in the ring buffer

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=2)

    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=1)

    await processor.drain()

    pre_roll_frames = 3
    speech_frames = 2
    assert len(calls[0]) == (pre_roll_frames + speech_frames) * vad_cfg.frame_bytes


async def test_speakers_do_not_block_one_another(build, tmp_path, monkeypatch):
    """Two utterances in flight must overlap rather than serialize."""
    processor, session = build()
    concurrent = 0
    peak = 0

    async def slow_transcribe(pcm: bytes):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return "overlapping"

    monkeypatch.setattr(processor_module, "transcribe", slow_transcribe)

    for speaker in (ALICE, BOB):
        _trigger(processor, speaker, on=True)
        _speak(processor, session, speaker, frames=4)
        _trigger(processor, speaker, on=False)
        _speak(processor, session, speaker, frames=1)

    await processor.drain()

    assert peak == 2
    assert {line["user"] for line in _lines(tmp_path)} == {ALICE[1], BOB[1]}


async def test_concurrency_is_bounded_by_the_semaphore(build, monkeypatch):
    processor, session = build()
    limit = processor._semaphore._value
    concurrent = 0
    peak = 0

    async def slow_transcribe(pcm: bytes):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1
        return "bounded"

    monkeypatch.setattr(processor_module, "transcribe", slow_transcribe)

    for index in range(limit * 3):
        speaker = (1000 + index, f"speaker{index}")
        _trigger(processor, speaker, on=True)
        _speak(processor, session, speaker, frames=2)
        _trigger(processor, speaker, on=False)
        _speak(processor, session, speaker, frames=1)

    await processor.drain()

    assert peak <= limit


async def test_empty_transcript_writes_nothing(build, tmp_path, transcripts):
    processor, session = build()
    _, replies = transcripts
    replies["text"] = ""

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)
    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=1)

    await processor.drain()

    assert _lines(tmp_path) == []


async def test_forced_flush_resets_the_vad(build, transcripts):
    """
    A flush interrupts the VAD mid-utterance; leaving it triggered would make
    the next onset skip its pre-roll.
    """
    processor, session = build()
    calls, _ = transcripts

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)

    processor.flush_user(ALICE[0], "user left channel")
    await processor.drain()

    assert len(calls) == 1
    assert processor._users.active_count == 0


async def test_a_written_utterance_reaches_the_tools(build, tmp_path, transcripts):
    """A tool that reads the file rather than the utterance must see the same thing."""
    from miss_quote.config import ServerConfig, ToolSettings
    from miss_quote.tools.base import Tool

    seen = []

    class Watcher(Tool):
        name = "watcher"

        async def handle_utterance(self, utterance, session) -> None:
            seen.append((utterance.text, session.path.read_text().count("\n")))

    tools = ToolRunner(
        {
            SOURCE.guild_id: ServerConfig(
                alias=SOURCE.guild_alias,
                users={},
                tools={"watcher": ToolSettings(enabled=True, config={})},
            )
        },
        {"watcher": Watcher},
    )
    processor, session = build(tools)

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)
    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=1)

    await processor.drain()

    assert seen == [("hello there", 1)]


async def test_an_empty_transcription_reaches_no_tool(build, tmp_path, transcripts):
    from miss_quote.config import ServerConfig, ToolSettings
    from miss_quote.tools.base import Tool

    seen = []

    class Watcher(Tool):
        name = "watcher"

        async def handle_utterance(self, utterance, session) -> None:
            seen.append(utterance)

    tools = ToolRunner(
        {
            SOURCE.guild_id: ServerConfig(
                alias=SOURCE.guild_alias,
                users={},
                tools={"watcher": ToolSettings(enabled=True, config={})},
            )
        },
        {"watcher": Watcher},
    )
    processor, session = build(tools)
    _, replies = transcripts
    replies["text"] = ""

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)
    _trigger(processor, ALICE, on=False)
    _speak(processor, session, ALICE, frames=1)

    await processor.drain()

    assert seen == []


async def test_stop_flushes_buffered_speech(build, tmp_path, transcripts):
    processor, session = build()

    _trigger(processor, ALICE, on=True)
    _speak(processor, session, ALICE, frames=4)

    await processor.stop()

    assert [line["text"] for line in _lines(tmp_path)] == ["hello there"]
