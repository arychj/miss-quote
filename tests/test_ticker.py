"""The one message a room watches, and what happens to it when Discord says no."""

import discord
import pytest

from miss_quote.bot.announcer import MESSAGE_LIMIT
from miss_quote.bot.ticker import ELLIPSIS, DiscordTicker, trimmed

ALIAS = "first-server"
CHANNEL = "session-summaries"
ELSEWHERE = "somewhere-else"

FIRST = "```\nErik: We open the door.\n```"
SECOND = "```\nErik: We open the door.\nEli: There is nothing behind it.\n```"

SERVER_ERROR = 500
REFUSED = 400


class Message:
    """A message that remembers every version of itself."""

    def __init__(self, content: str, failing: Exception | None = None) -> None:
        self.content = content
        self.edits: list[str] = []
        self._failing = failing

    async def edit(self, content: str, allowed_mentions=None) -> None:
        if self._failing is not None:
            raise self._failing

        self.content = content
        self.edits.append(content)


class Channel:
    """A text channel that hands back the messages it was asked to post."""

    def __init__(self, name: str = CHANNEL, failing: Exception | None = None) -> None:
        self.name = name
        self.posted: list[Message] = []
        self._failing = failing

        # What the next message posted here will do when it is edited, so a test
        # about a message that has gone does not have to reach into one.
        self.editing: Exception | None = None

    async def send(self, content: str, allowed_mentions=None) -> Message:
        if self._failing is not None:
            raise self._failing

        message = Message(content, self.editing)
        self.posted.append(message)

        return message


class Finder:
    """The announcer, as much of it as the ticker uses."""

    def __init__(self, *channels: Channel) -> None:
        self._channels = {channel.name: channel for channel in channels}

    def resolve(self, server: str, channel: str):
        return self._channels.get(channel)


def _ticker(*channels: Channel) -> DiscordTicker:
    return DiscordTicker(Finder(*channels))


def _http(status: int) -> discord.HTTPException:
    """What discord.py raises for a status, without a response to build one from."""
    return discord.HTTPException(_Response(status), {"message": "no"})


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "because"


# ── the first one and every one after ─────────


async def test_the_first_showing_posts_a_message():
    channel = Channel()

    assert await _ticker(channel).show(ALIAS, CHANNEL, FIRST)
    assert [message.content for message in channel.posted] == [FIRST]


async def test_the_next_showing_edits_the_same_message():
    channel = Channel()
    ticker = _ticker(channel)

    await ticker.show(ALIAS, CHANNEL, FIRST)
    await ticker.show(ALIAS, CHANNEL, SECOND)

    assert len(channel.posted) == 1
    assert channel.posted[0].edits == [SECOND]


async def test_two_channels_keep_two_messages():
    """What tells two rooms' feeds apart is the channel they are shown in."""
    here = Channel()
    there = Channel(ELSEWHERE)
    ticker = _ticker(here, there)

    await ticker.show(ALIAS, CHANNEL, FIRST)
    await ticker.show(ALIAS, ELSEWHERE, SECOND)

    assert [message.content for message in here.posted] == [FIRST]
    assert [message.content for message in there.posted] == [SECOND]


async def test_a_message_somebody_deleted_is_posted_again():
    """Deleting the block asks for it to move, not for the feed to stop."""
    channel = Channel()
    channel.editing = discord.NotFound(_Response(404), "gone")
    ticker = _ticker(channel)

    await ticker.show(ALIAS, CHANNEL, FIRST)

    assert await ticker.show(ALIAS, CHANNEL, SECOND)
    assert [message.content for message in channel.posted] == [FIRST, SECOND]


# ── when it cannot be shown ───────────────────


async def test_a_channel_that_is_not_there_is_reported():
    assert not await _ticker().show(ALIAS, CHANNEL, FIRST)


@pytest.mark.parametrize(
    "failure",
    [
        discord.Forbidden(_Response(403), "no"),
        _http(REFUSED),
        _http(SERVER_ERROR),
        OSError("the network"),
    ],
)
async def test_a_post_that_will_not_land_is_reported(failure):
    assert not await _ticker(Channel(failing=failure)).show(ALIAS, CHANNEL, FIRST)


async def test_a_channel_that_refused_the_first_post_is_tried_again():
    """Nothing is held that was not posted, so the next line is a fresh attempt."""
    channel = Channel(failing=_http(SERVER_ERROR))
    ticker = _ticker(channel)

    await ticker.show(ALIAS, CHANNEL, FIRST)
    channel._failing = None

    assert await ticker.show(ALIAS, CHANNEL, SECOND)
    assert [message.content for message in channel.posted] == [SECOND]


@pytest.mark.parametrize(
    "failure",
    [
        discord.Forbidden(_Response(403), "no"),
        _http(REFUSED),
        _http(SERVER_ERROR),
        OSError("the network"),
    ],
)
async def test_an_edit_that_will_not_land_is_reported(failure):
    channel = Channel()
    channel.editing = failure
    ticker = _ticker(channel)
    await ticker.show(ALIAS, CHANNEL, FIRST)

    assert not await ticker.show(ALIAS, CHANNEL, SECOND)


# ── what Discord will take ────────────────────


def test_a_body_inside_the_limit_is_left_alone():
    assert trimmed(FIRST) == FIRST


def test_a_body_over_the_limit_keeps_its_end():
    """The newest line is the one being watched, so the front is what goes."""
    body = "x" * (MESSAGE_LIMIT + 100) + "the last thing said"

    trimmed_body = trimmed(body)

    assert len(trimmed_body) == MESSAGE_LIMIT
    assert trimmed_body.startswith(ELLIPSIS)
    assert trimmed_body.endswith("the last thing said")


async def test_what_is_shown_is_cut_to_the_limit():
    channel = Channel()

    await _ticker(channel).show(ALIAS, CHANNEL, "x" * (MESSAGE_LIMIT + 1))

    assert len(channel.posted[0].content) == MESSAGE_LIMIT
