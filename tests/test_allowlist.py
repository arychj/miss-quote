import pytest

import bot.client as client_module
from config import FileConfig

ALLOWED_SERVER = 123456789012345678
OTHER_SERVER = 111222333444555666


def _allowlist(*servers: int) -> FileConfig:
    from pathlib import Path

    return FileConfig(
        path=Path("/config/config.yaml"),
        allowed_servers=frozenset(servers),
        user_names={},
        found=True,
    )


class FakeVoiceClient:
    def __init__(self) -> None:
        self.sinks = []

    def listen(self, sink) -> None:
        self.sinks.append(sink)


class FakeChannel:
    """A voice channel that records whether anything tried to connect to it."""

    def __init__(self, guild_id: int) -> None:
        self.guild = type("Guild", (), {"id": guild_id, "name": "somewhere"})()
        self.attempts = 0
        self.voice_client = FakeVoiceClient()

    async def connect(self, **kwargs):
        self.attempts += 1
        return self.voice_client

    def __str__(self) -> str:
        return "General"


@pytest.fixture
def bot(monkeypatch):
    """An STTBot with the transcript and STT machinery stubbed out."""
    monkeypatch.setattr(client_module, "TranscriptWriter", lambda: object())
    monkeypatch.setattr(client_module, "STTProcessor", lambda writer: object())
    monkeypatch.setattr(client_module, "STTAudioSink", lambda processor, channel: object())
    return client_module.STTBot()


async def test_connect_refuses_a_server_outside_the_allowlist(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _allowlist(ALLOWED_SERVER))
    channel = FakeChannel(OTHER_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 0, "the bot must not connect to an unlisted server"


async def test_connect_joins_a_server_in_the_allowlist(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _allowlist(ALLOWED_SERVER))
    channel = FakeChannel(ALLOWED_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 1
    assert channel.voice_client.sinks, "a joined channel must be listened to"


async def test_an_empty_allowlist_joins_nothing(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _allowlist())
    channel = FakeChannel(ALLOWED_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 0


async def test_a_channel_without_a_guild_is_refused(bot, monkeypatch):
    """A DM or group call has no guild, so no allowlist entry can cover it."""
    monkeypatch.setattr(client_module, "file_cfg", _allowlist(ALLOWED_SERVER))
    channel = FakeChannel(ALLOWED_SERVER)
    channel.guild = None

    await bot._connect(channel)

    assert channel.attempts == 0
