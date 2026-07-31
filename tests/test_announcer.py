from pathlib import Path

import discord
import pytest

import miss_quote.bot.announcer as announcer_module
from miss_quote.bot.announcer import MESSAGE_LIMIT, DiscordAnnouncer, split
from miss_quote.config import FileConfig, ServerConfig

SERVER_ID = 123456789012345678
ALIAS = "first-server"
CHANNEL = "session-summaries"

SUMMARY = "They argued about the rules for an hour and nobody won."

SERVER_ERROR = 500


class Channel:
    """A text channel that remembers what it was sent."""

    def __init__(self, name: str, refuses: Exception | None = None) -> None:
        self.name = name
        self.messages: list[str] = []
        self._refuses = refuses

    async def send(self, text: str) -> None:
        if self._refuses is not None:
            raise self._refuses

        self.messages.append(text)


class Guild:
    def __init__(self, *channels: Channel) -> None:
        self.text_channels = list(channels)


@pytest.fixture(autouse=True)
def known_server(monkeypatch):
    """One server in the mounted file, so an alias resolves back to an ID."""
    monkeypatch.setattr(
        announcer_module,
        "file_cfg",
        FileConfig(
            path=Path("/config/config.yaml"),
            servers={SERVER_ID: ServerConfig(alias=ALIAS, users={}, tools={})},
            problems=(),
            found=True,
        ),
    )


def _announcer(guild: Guild | None) -> DiscordAnnouncer:
    return DiscordAnnouncer(lambda server_id: guild)


async def test_a_named_channel_gets_the_text():
    channel = Channel(CHANNEL)

    posted = await _announcer(Guild(channel)).post(ALIAS, CHANNEL, SUMMARY)

    assert posted
    assert channel.messages == [SUMMARY]


async def test_only_the_named_channel_gets_it():
    wanted = Channel(CHANNEL)
    other = Channel("general")

    await _announcer(Guild(other, wanted)).post(ALIAS, CHANNEL, SUMMARY)

    assert wanted.messages == [SUMMARY]
    assert other.messages == []


async def test_a_name_that_points_nowhere_is_reported(caplog):
    posted = await _announcer(Guild(Channel("general"))).post(ALIAS, CHANNEL, SUMMARY)

    assert not posted
    assert CHANNEL in caplog.text


async def test_a_server_that_is_not_configured_posts_nothing():
    assert not await _announcer(Guild(Channel(CHANNEL))).post(
        "somewhere-else", CHANNEL, SUMMARY
    )


async def test_resolve_answers_without_sending_anything():
    """What `prewarm` asks, so a typo is a startup line rather than a lost summary."""
    channel = Channel(CHANNEL)
    announcer = _announcer(Guild(channel))

    assert announcer.resolve(ALIAS, CHANNEL) is channel
    assert announcer.resolve(ALIAS, "nowhere") is None
    assert channel.messages == []


async def test_a_missing_permission_is_a_failure_that_names_the_channel(caplog):
    channel = Channel(CHANNEL, refuses=discord.Forbidden(_response(403), "nope"))

    assert not await _announcer(Guild(channel)).post(ALIAS, CHANNEL, SUMMARY)
    assert "Send Messages" in caplog.text


async def test_a_server_error_is_a_failure():
    channel = Channel(
        CHANNEL, refuses=discord.HTTPException(_response(SERVER_ERROR), "later")
    )

    assert not await _announcer(Guild(channel)).post(ALIAS, CHANNEL, SUMMARY)


async def test_a_long_body_arrives_in_order_and_within_the_limit():
    channel = Channel(CHANNEL)
    paragraphs = "\n\n".join(f"Paragraph {number}. " + "word " * 100 for number in range(20))

    assert await _announcer(Guild(channel)).post(ALIAS, CHANNEL, paragraphs)

    assert len(channel.messages) > 1
    assert all(len(message) <= MESSAGE_LIMIT for message in channel.messages)
    assert channel.messages[0].startswith("Paragraph 0.")
    assert "Paragraph 19." in channel.messages[-1]


async def test_half_a_summary_is_not_a_success():
    """A partial post reads as a whole one, so it is reported as a failure."""
    channel = Channel(
        CHANNEL, refuses=discord.HTTPException(_response(SERVER_ERROR), "later")
    )
    paragraphs = "\n\n".join("word " * 200 for _ in range(20))

    assert not await _announcer(Guild(channel)).post(ALIAS, CHANNEL, paragraphs)


def test_a_short_body_is_one_message():
    assert split(SUMMARY) == [SUMMARY]


def test_an_empty_body_is_no_messages():
    assert split("   ") == []


def test_splitting_prefers_a_paragraph_break():
    first = "a" * (MESSAGE_LIMIT - 100)
    second = "b" * 200

    assert split(f"{first}\n\n{second}") == [first, second]


def test_splitting_falls_back_to_a_line_then_a_word():
    lines = "\n".join("c" * 100 for _ in range(30))
    words = " ".join("d" * 10 for _ in range(300))

    for body in (lines, words):
        pieces = split(body)
        assert all(len(piece) <= MESSAGE_LIMIT for piece in pieces)
        assert "".join(pieces.copy()).replace(" ", "").replace("\n", "") == body.replace(
            " ", ""
        ).replace("\n", "")


def test_an_unbroken_run_is_cut_at_the_limit():
    """Not something prose does, but it must not be left to be refused."""
    body = "e" * (MESSAGE_LIMIT * 2 + 10)

    pieces = split(body)

    assert all(len(piece) <= MESSAGE_LIMIT for piece in pieces)
    assert "".join(pieces) == body


def _response(status: int):
    """The minimum discord.py wants to build one of its HTTP errors around."""

    class Response:
        def __init__(self) -> None:
            self.status = status
            self.reason = "because"

    return Response()
