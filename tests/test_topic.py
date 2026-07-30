"""Getting one line under the name of the channel the bot is sitting in."""

from pathlib import Path

import discord
import pytest

import miss_quote.bot.topic as topic_module
from miss_quote.bot.topic import DiscordTopic
from miss_quote.config import FileConfig, ServerConfig

SERVER_ID = 123456789012345678
SERVER = "first-server"
UNCONFIGURED_SERVER = "nobody-configured-this"

ELI, ELI_ID = "Eli", 1

TALLY = f"{ELI}: -2"
OTHER_TALLY = f"{ELI}: -3"

FORBIDDEN_STATUS = 403
REJECTED_STATUS = 400
SERVER_ERROR_STATUS = 500

PUBLISHED = True
TRY_AGAIN = False


class FakeChannel:
    """
    A voice channel that keeps what it was asked to change.

    The whole edit is kept rather than just the value, because which field is
    written is the thing most worth guarding: a voice channel has no topic, and
    Discord refuses one with an error that reads like a profanity filter.
    """

    def __init__(self, failure: Exception | None = None) -> None:
        self.edits: list[dict] = []
        self.failure = failure
        self.name = "general-voice"

    async def edit(self, **fields) -> None:
        if self.failure is not None:
            raise self.failure

        self.edits.append(fields)

    @property
    def statuses(self) -> list[str]:
        return [edit["status"] for edit in self.edits]

    def __str__(self) -> str:
        return self.name


class FakeVoiceClient:
    def __init__(self, channel: FakeChannel, connected: bool = True) -> None:
        self.channel = channel
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class FakeGuild:
    def __init__(self, voice_client) -> None:
        self.voice_client = voice_client


def _response(status: int) -> discord.HTTPException:
    """A failure shaped the way discord.py raises one."""
    return discord.HTTPException(
        type("Response", (), {"status": status, "reason": "because"})(), "no"
    )


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """One server in the mounted file, so an alias resolves back to an ID."""
    monkeypatch.setattr(
        topic_module,
        "file_cfg",
        FileConfig(
            path=Path("/config/config.yaml"),
            servers={
                SERVER_ID: ServerConfig(alias=SERVER, users={ELI_ID: ELI}, tools={})
            },
            problems=(),
            found=True,
        ),
    )


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


def _topic(channel=None, connected: bool = True) -> DiscordTopic:
    voice_client = None if channel is None else FakeVoiceClient(channel, connected)

    return DiscordTopic(lambda server_id: FakeGuild(voice_client))


# ── where it goes ─────────────────────────────────


async def test_a_line_is_set_as_the_status_and_not_the_topic(channel):
    """
    A voice channel has no topic.

    Discord refuses one with CHANNEL_TOPIC_INVALID, "Field contains at least one
    word that is not allowed", which reads like a profanity filter and is not
    one — it refuses a topic of "test" the same way. The status is the line the
    client shows under the channel name.
    """
    assert await _topic(channel).publish(SERVER, TALLY) is PUBLISHED
    assert channel.edits == [{"status": TALLY}]


async def test_nothing_is_published_when_the_bot_is_in_no_voice_channel():
    """Reported as not up, so the line lands in the next channel the bot joins."""
    assert await _topic().publish(SERVER, TALLY) is TRY_AGAIN


async def test_nothing_is_published_while_the_bot_is_disconnected(channel):
    assert await _topic(channel, connected=False).publish(SERVER, TALLY) is TRY_AGAIN
    assert channel.statuses == []


async def test_a_server_nobody_configured_is_not_published(channel):
    """The alias cannot be resolved to a guild, so there is nowhere to put it."""
    assert await _topic(channel).publish(UNCONFIGURED_SERVER, TALLY) is TRY_AGAIN
    assert channel.statuses == []


# ── what Discord says back ────────────────────────


async def test_a_forbidden_edit_is_not_retried(caplog):
    """The permission is not going to appear on its own, and retries cost the bucket."""
    channel = FakeChannel(
        failure=discord.Forbidden(_response(FORBIDDEN_STATUS).response, "no")
    )

    with caplog.at_level("WARNING"):
        published = await _topic(channel).publish(SERVER, TALLY)

    assert published is PUBLISHED
    assert any("Set Voice Channel Status" in record.message for record in caplog.records)


async def test_a_rejected_status_is_not_retried(caplog):
    """
    A request Discord will not parse cannot come good on the next tick.

    Retrying one spends the channel's rate limit, every interval, for the life of
    the process.
    """
    channel = FakeChannel(failure=_response(REJECTED_STATUS))

    with caplog.at_level("ERROR"):
        published = await _topic(channel).publish(SERVER, TALLY)

    assert published is PUBLISHED
    assert len(caplog.records) == 1


async def test_a_rejection_says_what_it_tried_to_set(caplog):
    """A rejection caused by a name in the tally cannot be diagnosed without it."""
    channel = FakeChannel(failure=_response(REJECTED_STATUS))

    with caplog.at_level("ERROR"):
        await _topic(channel).publish(SERVER, TALLY)

    assert TALLY in caplog.records[0].getMessage()


async def test_a_failed_edit_is_worth_another_go(channel, caplog):
    channel.failure = _response(SERVER_ERROR_STATUS)

    with caplog.at_level("WARNING"):
        published = await _topic(channel).publish(SERVER, TALLY)

    assert published is TRY_AGAIN


async def test_an_unreachable_discord_is_worth_another_go(channel, caplog):
    channel.failure = OSError("the network is a lie")

    with caplog.at_level("WARNING"):
        published = await _topic(channel).publish(SERVER, TALLY)

    assert published is TRY_AGAIN
    assert any(TALLY in record.getMessage() for record in caplog.records)


async def test_a_line_that_was_refused_is_still_sent_when_it_changes(channel, caplog):
    """What was refused was that text; the next text is not that text."""
    channel.failure = _response(REJECTED_STATUS)
    topic = _topic(channel)

    with caplog.at_level("ERROR"):
        await topic.publish(SERVER, TALLY)

    channel.failure = None

    assert await topic.publish(SERVER, OTHER_TALLY) is PUBLISHED
    assert channel.statuses == [OTHER_TALLY]
