import pytest

import bot.client as client_module
from config import FileConfig

KNOWN_SERVER = 123456789012345678
OTHER_SERVER = 111222333444555666


def _known(*servers: int) -> FileConfig:
    from pathlib import Path

    return FileConfig(
        path=Path("/config/config.yaml"),
        known_servers={server: f"server-{index}" for index, server in enumerate(servers)},
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


async def test_connect_refuses_an_unknown_server(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _known(KNOWN_SERVER))
    channel = FakeChannel(OTHER_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 0, "the bot must not connect to an unknown server"


async def test_connect_joins_a_known_server(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _known(KNOWN_SERVER))
    channel = FakeChannel(KNOWN_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 1
    assert channel.voice_client.sinks, "a joined channel must be listened to"


async def test_no_known_servers_joins_nothing(bot, monkeypatch):
    monkeypatch.setattr(client_module, "file_cfg", _known())
    channel = FakeChannel(KNOWN_SERVER)

    await bot._connect(channel)

    assert channel.attempts == 0


async def test_a_channel_without_a_guild_is_refused(bot, monkeypatch):
    """A DM or group call has no guild, so no allowlist entry can cover it."""
    monkeypatch.setattr(client_module, "file_cfg", _known(KNOWN_SERVER))
    channel = FakeChannel(KNOWN_SERVER)
    channel.guild = None

    await bot._connect(channel)

    assert channel.attempts == 0


def _sink_config(server_id: int, alias: str, names: dict | None = None) -> FileConfig:
    from pathlib import Path

    return FileConfig(
        path=Path("/config/config.yaml"),
        known_servers={server_id: alias},
        user_names={alias: names or {}},
        found=True,
    )


def test_transcript_path_uses_the_alias_not_the_discord_name(monkeypatch):
    """Renaming a server on Discord must not start a new transcript directory."""
    import bot.audio_sink as sink_module

    monkeypatch.setattr(
        sink_module, "file_cfg", _sink_config(KNOWN_SERVER, "first-server")
    )

    channel = FakeChannel(KNOWN_SERVER)
    channel.guild.name = "Some Server Nobody Named Consistently"
    channel.id = 5150
    channel.name = "general-voice"

    sink = sink_module.STTAudioSink(processor=object(), channel=channel)

    assert sink._source.guild_alias == "first-server"
    assert str(sink._source.relative_directory).startswith(
        f"{KNOWN_SERVER}-first-server/"
    )


def test_speaker_names_come_from_the_servers_own_roster(monkeypatch):
    import bot.audio_sink as sink_module

    speaker = 234567890123456789
    monkeypatch.setattr(
        sink_module,
        "file_cfg",
        _sink_config(KNOWN_SERVER, "first-server", {speaker: "Speaker One"}),
    )

    channel = FakeChannel(KNOWN_SERVER)
    channel.id = 5150
    channel.name = "general-voice"

    submitted = []
    processor = type(
        "P", (), {"submit": lambda self, uid, name, source, pcm: submitted.append(name)}
    )()
    sink = sink_module.STTAudioSink(processor=processor, channel=channel)

    user = type("U", (), {"id": speaker, "display_name": "xX_nickname_Xx"})()
    sink.write(user, type("D", (), {"pcm": b"\x00" * 3840})())

    assert submitted == ["Speaker One"]
