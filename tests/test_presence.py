import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from discord.ext import commands

import miss_quote.bot.client as client_module
import miss_quote.transcript.writer as writer_module
from miss_quote.bot.presence import DiscordPresence
from miss_quote.config import FileConfig, ServerConfig, transcript_cfg
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.schedule import ALWAYS, Schedule
from miss_quote.transcript.writer import TranscriptWriter

SERVER = 123456789012345678
ALIAS = "first-server"
CHANNEL_ID = 5150

TIMEZONE = "America/Los_Angeles"
ZONE = ZoneInfo(TIMEZONE)
KEEP_FOREVER = -1

WORDING = "🎙️ transcribing..."
NO_RESUMING = 0.0
A_LONG_WINDOW = 30.0

# 2026-07-29 is a Wednesday; the window is the same one the writer's tests use.
EVENING = Schedule.parse(["Wed 17:00-00:00"])
INSIDE = datetime(2026, 7, 29, 18, 0, 0, tzinfo=ZONE)
OUTSIDE = datetime(2026, 7, 29, 11, 0, 0, tzinfo=ZONE)

USER_ID = 1234567890
USER = "someone"


# ── doubles ───────────────────────────────────────


class FakeClient:
    """A client that records what it was asked to publish."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.published = []
        self.failures = 0

    def is_ready(self) -> bool:
        return self.ready

    async def change_presence(self, *, activity=None) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("the gateway went away")

        self.published.append(activity)


class RecordingPresence:
    """Stands in for the presence, keeping the sequence of states asked for."""

    def __init__(self) -> None:
        self.states = []

    async def transcribing(self, keeping: bool) -> None:
        self.states.append(keeping)


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
        self.listening = False

    def listen(self, sink) -> None:
        self.listening = True

    def is_listening(self) -> bool:
        return self.listening

    def stop_listening(self) -> None:
        self.listening = False

    async def disconnect(self) -> None:
        return None


class FakeProcessor:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def flush_all(self, reason: str) -> None:
        return None

    async def drain(self) -> None:
        return None


class FakeContext:
    """Enough of a command context for the callbacks and the permission check."""

    def __init__(self, voice_client=None, administrator: bool = True) -> None:
        self.voice_client = voice_client
        self.permissions = type("Permissions", (), {"administrator": administrator})()
        self.sent = []
        self.prefix = "!"
        self.invoked_with = "start-transcribing"

    async def send(self, message: str) -> None:
        self.sent.append(message)


class FrozenDatetime(datetime):
    current: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


# ── the presence itself ───────────────────────────


async def test_the_status_is_set_when_a_conversation_is_kept():
    client = FakeClient()
    presence = DiscordPresence(client, WORDING)

    await presence.transcribing(True)

    assert [activity.name for activity in client.published] == [WORDING]


async def test_the_status_is_cleared_when_none_is():
    client = FakeClient()
    presence = DiscordPresence(client, WORDING)

    await presence.transcribing(True)
    await presence.transcribing(False)

    assert client.published[-1] is None


async def test_the_same_state_is_not_published_twice():
    """Every caller is a lifecycle event, and the gateway budget is a handful."""
    client = FakeClient()
    presence = DiscordPresence(client, WORDING)

    await presence.transcribing(True)
    await presence.transcribing(True)
    await presence.transcribing(False)
    await presence.transcribing(False)

    assert len(client.published) == 2


async def test_nothing_is_published_before_the_gateway_is_up():
    """Presence rides the websocket; a session may open before there is one."""
    client = FakeClient(ready=False)
    presence = DiscordPresence(client, WORDING)

    await presence.transcribing(True)

    assert client.published == []


async def test_a_failure_is_tried_again_on_the_next_transition():
    """Deduplicating against something that never landed would lose the state."""
    client = FakeClient()
    client.failures = 1
    presence = DiscordPresence(client, WORDING)

    await presence.transcribing(True)
    assert client.published == []

    await presence.transcribing(True)
    assert [activity.name for activity in client.published] == [WORDING]


async def test_empty_wording_turns_the_signal_off():
    """Which needs no second setting to say."""
    client = FakeClient()
    presence = DiscordPresence(client, "")

    await presence.transcribing(True)

    assert not presence.enabled
    assert client.published == []


# ── what drives it ────────────────────────────────


@pytest.fixture
def make_bot(monkeypatch, tmp_path):
    """An STTBot writing to tmp_path, with its presence recorded rather than sent."""

    def _build(schedule: Schedule = ALWAYS, resume_seconds: float = NO_RESUMING):
        config = FileConfig(
            path=Path("/config/config.yaml"),
            servers={SERVER: ServerConfig(alias=ALIAS, users={}, tools={})},
            problems=(),
            found=True,
        )

        monkeypatch.setattr(client_module, "file_cfg", config)
        monkeypatch.setattr(
            client_module,
            "transcript_cfg",
            replace(transcript_cfg, resume_window_seconds=resume_seconds),
        )
        monkeypatch.setattr(
            client_module,
            "TranscriptWriter",
            lambda: TranscriptWriter(
                directory=tmp_path,
                timezone=TIMEZONE,
                retention_days=KEEP_FOREVER,
                schedules=lambda guild_id, channel: schedule,
            ),
        )
        monkeypatch.setattr(
            client_module,
            "ToolRunner",
            lambda speaker, topic, announcer: ToolRunner(
                config.servers, {}, speaker, topic, announcer
            ),
        )
        monkeypatch.setattr(client_module, "STTProcessor", FakeProcessor)
        monkeypatch.setattr(
            client_module, "STTAudioSink", lambda processor, session: session
        )

        bot = client_module.STTBot()
        bot._presence = RecordingPresence()

        return bot

    return _build


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(writer_module, "datetime", FrozenDatetime)

    def move_to(moment: datetime) -> None:
        FrozenDatetime.current = moment

    return move_to


async def test_joining_a_channel_on_the_record_says_so(make_bot, frozen_clock):
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING)

    await bot._connect(FakeChannel())

    assert bot._presence.states == [True]


async def test_joining_off_the_record_says_nothing(make_bot, frozen_clock):
    """Being in a channel already implies listening; only retention is signalled."""
    frozen_clock(OUTSIDE)
    bot = make_bot(schedule=EVENING)

    await bot._connect(FakeChannel())

    assert bot._presence.states == [False]


async def test_leaving_clears_the_status(make_bot, frozen_clock):
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING)
    channel = FakeChannel()

    await bot._connect(channel)
    await bot._disconnect(channel.voice_client)

    assert bot._presence.states[-1] is False


async def test_a_session_held_open_for_a_reconnect_still_counts(make_bot, frozen_clock):
    """
    It will be appended to if they come back, so the room should still be told.
    The alternative flickers the status through every resume window.
    """
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING, resume_seconds=A_LONG_WINDOW)
    channel = FakeChannel()

    await bot._connect(channel)
    await bot._disconnect(channel.voice_client)

    assert CHANNEL_ID in bot._sessions
    assert bot._presence.states[-1] is True

    bot._cancel_expiry(CHANNEL_ID)


async def test_shutting_down_clears_the_status(make_bot, frozen_clock):
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING, resume_seconds=A_LONG_WINDOW)

    await bot._connect(FakeChannel())
    await bot._close_all_sessions()

    assert bot._presence.states[-1] is False


# ── the commands ──────────────────────────────────


def _command(bot, name: str):
    return bot._bot.get_command(name)


async def test_start_transcribing_puts_a_session_on_the_record(make_bot, frozen_clock):
    frozen_clock(OUTSIDE)
    bot = make_bot(schedule=EVENING)
    channel = FakeChannel()
    await bot._connect(channel)

    session = bot._sessions[CHANNEL_ID]
    assert not session.capturing

    ctx = FakeContext(voice_client=channel.voice_client)
    await _command(bot, "start-transcribing").callback(ctx)

    assert session.capturing
    assert bot._presence.states[-1] is True
    assert ctx.sent


async def test_start_transcribing_does_not_backfill(make_bot, frozen_clock):
    """Nothing said off the record was kept anywhere to be written down now."""
    frozen_clock(OUTSIDE)
    bot = make_bot(schedule=EVENING)
    channel = FakeChannel()
    await bot._connect(channel)

    session = bot._sessions[CHANNEL_ID]
    session.write(USER_ID, USER, "before the command")

    await _command(bot, "start-transcribing").callback(
        FakeContext(voice_client=channel.voice_client)
    )
    session.write(USER_ID, USER, "after the command")

    assert [utterance.text for utterance in session.close().read()] == [
        "after the command"
    ]


async def test_stop_transcribing_keeps_what_was_already_written(make_bot, frozen_clock):
    """Stopping is a decision about what happens next, not a retraction."""
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING)
    channel = FakeChannel()
    await bot._connect(channel)

    session = bot._sessions[CHANNEL_ID]
    session.write(USER_ID, USER, "on the record")

    await _command(bot, "stop-transcribing").callback(
        FakeContext(voice_client=channel.voice_client)
    )
    session.write(USER_ID, USER, "after the command")

    assert not session.capturing
    assert bot._presence.states[-1] is False
    assert [utterance.text for utterance in session.close().read()] == ["on the record"]


async def test_asking_twice_says_so_and_changes_nothing(make_bot, frozen_clock):
    frozen_clock(INSIDE)
    bot = make_bot(schedule=EVENING)
    channel = FakeChannel()
    await bot._connect(channel)

    published = len(bot._presence.states)
    ctx = FakeContext(voice_client=channel.voice_client)
    await _command(bot, "start-transcribing").callback(ctx)

    assert bot._sessions[CHANNEL_ID].capturing
    assert len(bot._presence.states) == published
    assert "Already transcribing" in ctx.sent[0]


async def test_the_commands_say_so_when_the_bot_is_in_no_channel(make_bot):
    bot = make_bot()
    ctx = FakeContext(voice_client=None)

    await _command(bot, "stop-transcribing").callback(ctx)

    assert ctx.sent == [client_module.NOT_IN_A_CHANNEL]


@pytest.mark.parametrize("name", ["start-transcribing", "stop-transcribing"])
async def test_the_commands_need_administrator(make_bot, name):
    """What these decide is whether everybody in the room is on the record."""
    bot = make_bot()
    checks = _command(bot, name).checks

    assert checks

    with pytest.raises(commands.MissingPermissions):
        for check in checks:
            check(FakeContext(administrator=False))


@pytest.mark.parametrize("name", ["start-transcribing", "stop-transcribing"])
async def test_an_administrator_passes_the_check(make_bot, name):
    bot = make_bot()

    assert all(check(FakeContext()) for check in _command(bot, name).checks)


async def test_a_refusal_is_said_out_loud(make_bot):
    """A command that does nothing and says nothing is one somebody keeps trying."""
    bot = make_bot()
    ctx = FakeContext(administrator=False)

    await bot._refuse_without_permission(ctx, commands.MissingPermissions(["administrator"]))

    assert "Administrator" in ctx.sent[0]


async def test_anything_else_is_re_raised(make_bot):
    """A real failure should still reach the log it would have reached."""
    bot = make_bot()

    with pytest.raises(RuntimeError):
        await bot._refuse_without_permission(FakeContext(), RuntimeError("something else"))
