from pathlib import Path

import pytest

import miss_quote.bot.client as client_module
from miss_quote.config import FileConfig, ServerConfig

KNOWN_SERVER = 123456789012345678
OTHER_SERVER = 111222333444555666
CHANNEL_ID = 5150


def _config(servers: dict[int, ServerConfig]) -> FileConfig:
    return FileConfig(
        path=Path("/config/config.yaml"),
        servers=servers,
        problems=(),
        found=True,
    )


def _known(*servers: int) -> FileConfig:
    return _config(
        {
            server: ServerConfig(alias=f"server-{index}", users={}, tools={})
            for index, server in enumerate(servers)
        }
    )


def _server(server_id: int, alias: str, users: dict | None = None) -> FileConfig:
    return _config({server_id: ServerConfig(alias=alias, users=users or {}, tools={})})


class FakeVoiceClient:
    def __init__(self) -> None:
        self.sinks = []

    def listen(self, sink) -> None:
        self.sinks.append(sink)


class FakeChannel:
    """A voice channel that records whether anything tried to connect to it."""

    def __init__(self, guild_id: int) -> None:
        self.guild = type("Guild", (), {"id": guild_id, "name": "somewhere"})()
        self.id = CHANNEL_ID
        self.name = "general-voice"
        self.attempts = 0
        self.voice_client = FakeVoiceClient()

    async def connect(self, **kwargs):
        self.attempts += 1
        return self.voice_client

    def __str__(self) -> str:
        return "General"


class FakeSession:
    def __init__(self, source) -> None:
        self.source = source
        self.capturing = True


class FakeWriter:
    """A writer that hands out sessions without touching a disk."""

    def __init__(self) -> None:
        self.opened = []

    def open(self, source):
        session = FakeSession(source)
        self.opened.append(session)
        return session


@pytest.fixture
def bot(monkeypatch):
    """An STTBot with the transcript and STT machinery stubbed out."""
    monkeypatch.setattr(client_module, "TranscriptWriter", FakeWriter)
    monkeypatch.setattr(client_module, "STTProcessor", lambda tools: object())
    monkeypatch.setattr(client_module, "STTAudioSink", lambda processor, session: session)
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


async def test_a_refused_server_opens_no_transcript(bot, monkeypatch):
    """A refusal must leave no trace in the tree, not even an empty file."""
    monkeypatch.setattr(client_module, "file_cfg", _known(KNOWN_SERVER))

    await bot._connect(FakeChannel(OTHER_SERVER))

    assert bot._writer.opened == []


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


def test_transcript_path_uses_the_alias_not_the_discord_name(monkeypatch):
    """Renaming a server on Discord must not start a new transcript directory."""
    monkeypatch.setattr(client_module, "file_cfg", _server(KNOWN_SERVER, "first-server"))

    channel = FakeChannel(KNOWN_SERVER)
    channel.guild.name = "Some Server Nobody Named Consistently"

    source = client_module.source_for(channel)

    assert source.guild_alias == "first-server"
    assert str(source.relative_directory) == "first-server/general-voice"


def test_speaker_names_come_from_the_servers_own_roster(monkeypatch):
    import miss_quote.bot.audio_sink as sink_module

    speaker = 234567890123456789
    monkeypatch.setattr(
        sink_module,
        "file_cfg",
        _server(KNOWN_SERVER, "first-server", {speaker: "Speaker One"}),
    )
    monkeypatch.setattr(client_module, "file_cfg", _server(KNOWN_SERVER, "first-server"))

    session = FakeSession(client_module.source_for(FakeChannel(KNOWN_SERVER)))
    session.path = Path("/transcripts/first-server/general-voice/session.jsonl")

    submitted = []
    processor = type(
        "P", (), {"submit": lambda self, uid, name, session, pcm: submitted.append(name)}
    )()
    sink = sink_module.STTAudioSink(processor=processor, session=session)

    user = type("U", (), {"id": speaker, "display_name": "xX_nickname_Xx"})()
    sink.write(user, type("D", (), {"pcm": b"\x00" * 3840})())

    assert submitted == ["Speaker One"]
