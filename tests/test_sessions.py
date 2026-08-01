import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import miss_quote.bot.client as client_module
from miss_quote.config import FileConfig, ServerConfig, ToolSettings, transcript_cfg
from miss_quote.tools.base import Tool
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.schedule import ALWAYS
from miss_quote.transcript.writer import TranscriptWriter

SERVER = 123456789012345678
ALIAS = "first-server"
CHANNEL_ID = 5150
OTHER_CHANNEL_ID = 5151

TIMEZONE = "America/Los_Angeles"
KEEP_FOREVER = -1
TOOL_NAME = "collector"

# Resume windows the tests drive the lifecycle with. The brief one has to expire
# during a test; the long one must not.
NO_RESUMING = 0.0
A_BRIEF_WINDOW = 0.02
A_LONG_WINDOW = 30.0
WINDOWS_TO_WAIT = 10


class Collector(Tool):
    """Remembers every finished transcript it is handed."""

    name = TOOL_NAME
    transcripts: list = []

    async def handle_finished(self, transcript) -> None:
        Collector.transcripts.append(transcript)


class FakeChannel:
    def __init__(self, channel_id: int = CHANNEL_ID, name: str = "general-voice") -> None:
        self.guild = type("Guild", (), {"id": SERVER, "name": "Somewhere"})()
        self.id = channel_id
        self.name = name
        self.voice_client = FakeVoiceClient(self)

    async def connect(self, **kwargs):
        return self.voice_client

    def __str__(self) -> str:
        return self.name


class FakeVoiceClient:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.sinks = []
        self.listening = False
        self.disconnected = False

    def listen(self, sink) -> None:
        self.sinks.append(sink)
        self.listening = True

    def is_listening(self) -> bool:
        return self.listening

    def stop_listening(self) -> None:
        self.listening = False

    async def disconnect(self) -> None:
        self.disconnected = True

    async def move_to(self, channel) -> None:
        self.channel = channel


class FakeProcessor:
    def __init__(self, *args, **kwargs) -> None:
        self.flushes = []

    def flush_all(self, reason: str) -> None:
        self.flushes.append(reason)

    async def drain(self) -> None:
        return None


@pytest.fixture(autouse=True)
def collected():
    Collector.transcripts = []
    return Collector.transcripts


@pytest.fixture
def resume_window(monkeypatch):
    """Set how long a transcript is held open for a reconnect."""

    def _set(seconds: float) -> None:
        monkeypatch.setattr(
            client_module,
            "transcript_cfg",
            replace(transcript_cfg, resume_window_seconds=seconds),
        )

    return _set


@pytest.fixture
def bot(monkeypatch, tmp_path):
    """An STTBot writing to tmp_path, with one server elected into one tool."""
    config = FileConfig(
        path=Path("/config/config.yaml"),
        servers={
            SERVER: ServerConfig(
                alias=ALIAS,
                users={},
                tools={TOOL_NAME: ToolSettings(enabled=True, config={})},
            )
        },
        problems=(),
        found=True,
    )

    monkeypatch.setattr(client_module, "file_cfg", config)
    monkeypatch.setattr(
        client_module,
        "TranscriptWriter",
        lambda: TranscriptWriter(
            directory=tmp_path,
            timezone=TIMEZONE,
            retention_days=KEEP_FOREVER,
            # Every room on the record: these tests are about when a session
            # seals, not about which rooms a deployment listed.
            schedules=lambda guild_id, channel: ALWAYS,
        ),
    )
    monkeypatch.setattr(
        client_module,
        "ToolRunner",
        lambda speaker, topic, announcer: ToolRunner(
            config.servers, {TOOL_NAME: Collector}, speaker, topic, announcer
        ),
    )
    monkeypatch.setattr(client_module, "STTProcessor", FakeProcessor)
    monkeypatch.setattr(client_module, "STTAudioSink", lambda processor, session: session)

    return client_module.STTBot()


def _spoken_in(bot, channel_id: int = CHANNEL_ID) -> None:
    """
    Put something in an open session, so sealing it produces a transcript.

    A session nobody spoke in takes its own file away and is never handed to a
    tool. These tests are about when a session seals rather than what is in one,
    and the dispatch is how they can tell.
    """
    bot._sessions[channel_id].write(1, "someone", "said a thing")


async def test_joining_opens_a_session(bot):
    channel = FakeChannel()

    await bot._connect(channel)

    assert CHANNEL_ID in bot._sessions
    assert bot._sessions[CHANNEL_ID].path.is_file()


async def test_leaving_seals_the_transcript_when_resuming_is_disabled(
    bot, collected, resume_window
):
    resume_window(NO_RESUMING)
    channel = FakeChannel()
    await bot._connect(channel)
    session = bot._sessions[CHANNEL_ID]
    session.write(1, "someone", "said a thing")

    await bot._disconnect(channel.voice_client)

    assert bot._sessions == {}
    assert len(collected) == 1
    assert collected[0].path == session.path
    assert collected[0].utterances == 1
    assert [utterance.text for utterance in collected[0].read()] == ["said a thing"]


async def test_the_transcript_is_complete_before_the_tools_see_it(
    bot, collected, resume_window
):
    """The processor drains first, so nothing lands after a tool has read the file."""
    resume_window(NO_RESUMING)
    channel = FakeChannel()
    await bot._connect(channel)

    await bot._disconnect(channel.voice_client)

    assert bot._processor.flushes == ["leaving channel"]


async def test_rejoining_starts_a_second_transcript_once_the_window_has_passed(
    bot, collected, resume_window
):
    resume_window(NO_RESUMING)
    channel = FakeChannel()

    await bot._connect(channel)
    first = bot._sessions[CHANNEL_ID].path
    _spoken_in(bot)
    await bot._disconnect(channel.voice_client)

    await bot._connect(channel)
    second = bot._sessions[CHANNEL_ID].path

    assert first != second
    assert len(collected) == 1, "only the first visit has ended"


async def test_a_re_attached_sink_keeps_the_same_transcript(bot):
    """The watchdog covers an internal failure; the bot never left the channel."""
    channel = FakeChannel()
    await bot._connect(channel)
    session = bot._sessions[CHANNEL_ID]

    assert bot._session_for(channel) is session


# ── the resume window ─────────────────────────────


async def test_leaving_holds_the_transcript_open_for_a_reconnect(
    bot, collected, resume_window
):
    """A tool must not see a fragment of a conversation that is about to continue."""
    resume_window(A_LONG_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)

    await bot._disconnect(channel.voice_client)

    assert CHANNEL_ID in bot._sessions
    assert collected == []


async def test_reconnecting_inside_the_window_continues_the_transcript(
    bot, collected, resume_window
):
    resume_window(A_LONG_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)
    session = bot._sessions[CHANNEL_ID]
    session.write(1, "someone", "before the gap")

    await bot._disconnect(channel.voice_client)
    await bot._connect(channel)

    assert bot._sessions[CHANNEL_ID] is session
    bot._sessions[CHANNEL_ID].write(1, "someone", "after the gap")

    await bot._close_all_sessions()

    assert len(collected) == 1
    assert [utterance.text for utterance in collected[0].read()] == [
        "before the gap",
        "after the gap",
    ]


async def test_the_window_expiring_seals_the_transcript(bot, collected, resume_window):
    resume_window(A_BRIEF_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)
    _spoken_in(bot)

    await bot._disconnect(channel.voice_client)
    await asyncio.sleep(A_BRIEF_WINDOW * WINDOWS_TO_WAIT)

    assert bot._sessions == {}
    assert bot._expiries == {}
    assert len(collected) == 1


async def test_reconnecting_after_the_window_starts_a_new_transcript(
    bot, collected, resume_window
):
    resume_window(A_BRIEF_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)
    first = bot._sessions[CHANNEL_ID].path
    _spoken_in(bot)

    await bot._disconnect(channel.voice_client)
    await asyncio.sleep(A_BRIEF_WINDOW * WINDOWS_TO_WAIT)
    await bot._connect(channel)

    assert bot._sessions[CHANNEL_ID].path != first
    assert len(collected) == 1


async def test_a_sealed_transcript_ends_when_the_connection_did(
    bot, collected, resume_window
):
    """The conversation ended at the disconnect, not when the window ran out."""
    resume_window(A_BRIEF_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)
    _spoken_in(bot)

    await bot._disconnect(channel.voice_client)
    await asyncio.sleep(A_BRIEF_WINDOW * WINDOWS_TO_WAIT)
    sealed_at = datetime.now(ZoneInfo(TIMEZONE))

    assert collected[0].closed < sealed_at - timedelta(seconds=A_BRIEF_WINDOW)


async def test_a_second_disconnect_restarts_the_clock(bot, collected, resume_window):
    resume_window(A_LONG_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)

    await bot._disconnect(channel.voice_client)
    first_expiry = bot._expiries[CHANNEL_ID]

    await bot._connect(channel)
    await bot._disconnect(channel.voice_client)
    await asyncio.sleep(0)  # let the cancellation land

    assert first_expiry.cancelled()
    assert bot._expiries[CHANNEL_ID] is not first_expiry
    assert collected == []


# ── shutdown ──────────────────────────────────────


async def test_shutdown_closes_every_open_session(bot, collected):
    here = FakeChannel()
    elsewhere = FakeChannel(OTHER_CHANNEL_ID, "side-room")

    await bot._connect(here)
    await bot._connect(elsewhere)
    _spoken_in(bot)
    _spoken_in(bot, OTHER_CHANNEL_ID)

    await bot._close_all_sessions()

    assert bot._sessions == {}
    assert len(collected) == 2


async def test_shutdown_does_not_wait_out_the_resume_window(
    bot, collected, resume_window
):
    """A session held open for a reconnect that will never come must not be lost."""
    resume_window(A_LONG_WINDOW)
    channel = FakeChannel()
    await bot._connect(channel)
    _spoken_in(bot)
    await bot._disconnect(channel.voice_client)

    await bot._close_all_sessions()

    assert bot._sessions == {}
    assert len(collected) == 1


# ── moving and edge cases ─────────────────────────


async def test_moving_channels_ends_one_transcript_and_starts_another(
    bot, collected, resume_window
):
    resume_window(NO_RESUMING)
    channel = FakeChannel()
    await bot._connect(channel)
    first = bot._sessions[CHANNEL_ID]
    _spoken_in(bot)

    elsewhere = FakeChannel(OTHER_CHANNEL_ID, "side-room")
    await bot._move(channel.voice_client, elsewhere)

    assert CHANNEL_ID not in bot._sessions
    assert collected == [first.close()]
    assert bot._sessions[OTHER_CHANNEL_ID].path.parent.name == "side-room"


async def test_ending_a_channel_that_was_never_joined_is_harmless(bot, collected):
    await bot._end_session(CHANNEL_ID)

    assert collected == []
    assert bot._expiries == {}


async def test_a_refused_server_never_opens_a_session(bot, monkeypatch):
    monkeypatch.setattr(
        client_module,
        "file_cfg",
        FileConfig(path=Path("/config/config.yaml"), servers={}, problems=(), found=True),
    )

    await bot._connect(FakeChannel())

    assert bot._sessions == {}
