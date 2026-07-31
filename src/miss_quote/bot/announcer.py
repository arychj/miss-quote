"""
Posting a body of text into a named text channel.

The other half of `tools.summary`, which knows what the text says and nothing
about where it goes, and the counterpart to `bot.topic`: a topic is one line that
replaces the last one under a voice channel's name, and this is a message that
joins the ones before it in a channel somebody scrolls back through.

**Channels are named rather than identified.** A tool holds a server alias and a
channel name, so a name is what it can ask for, and a name is also what a person
writing the config file has in front of them. The cost is that a channel renamed
on Discord silently stops receiving posts, and that two categories may hold
channels of the same name — the first match wins. Both are why a name that
resolves to nothing is a warning that says which name it was, and why the tool
that posts checks its channel once at startup instead of at the end of the first
session it summarizes.

Discord will not take a message longer than 2000 characters, and a summary is
occasionally longer than that, so a body is cut into pieces on the largest
boundary it has: a blank line, then a line, then — for a wall of text with
neither — a word. Never mid-word, because the seam between two messages is
already visible enough.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import discord

from miss_quote.config import file_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# Discord's ceiling on one message. Not a setting: it is the API's number, and a
# deployment that lowered it would only post more messages than it had to.
MESSAGE_LIMIT = 2000

PARAGRAPH_BREAK = "\n\n"
LINE_BREAK = "\n"
WORD_BREAK = " "

# Tried in order, largest first, so a body is cut where a reader would have
# paused anyway and only falls back to a word when it has nothing else.
BOUNDARIES = (PARAGRAPH_BREAK, LINE_BREAK, WORD_BREAK)

# A request Discord will not accept however many times it is sent — the same
# distinction `bot.topic` draws, and for the same reason.
REFUSED = 400


class DiscordAnnouncer:
    """Posts what a tool has written into a text channel named in its config."""

    def __init__(self, guilds: Callable[[int], Any | None]) -> None:
        # Resolved through a callable rather than the bot itself, for the same
        # reason the speaker and the topic are: this is built before the bot
        # whose guilds it looks things up in.
        self._guilds = guilds

    def resolve(self, server: str, channel: str) -> Any | None:
        """
        The text channel one name points at, if there is one.

        Public because the point of naming a channel rather than identifying it
        is that the name can be wrong, and a tool wants to say so at startup
        rather than at the end of the first conversation it has to file.
        """
        server_id = file_cfg.id_for(server)
        if server_id is None:
            return None

        guild = self._guilds(server_id)
        if guild is None:
            return None

        return discord.utils.get(getattr(guild, "text_channels", ()), name=channel)

    async def post(self, server: str, channel: str, text: str) -> bool:
        """
        Put a body of text in one channel, saying whether all of it landed.

        Every piece has to land for this to be True. Half a summary in a channel
        is worse than none, in that it reads as a whole one, so a failure partway
        through is reported as a failure rather than as a partial success.
        """
        target = self.resolve(server, channel)
        if target is None:
            logger.warning(
                "No text channel called '%s' in %s; %d characters were not posted.",
                channel,
                server,
                len(text),
            )
            return False

        for piece in split(text):
            if not await self._send(target, piece, server):
                return False

        logger.info(
            "Posted %d characters for %s to '#%s'.", len(text), server, channel
        )

        return True

    @staticmethod
    async def _send(channel: Any, text: str, server: str) -> bool:
        """
        One message, saying whether it landed.

        False for every failure, unlike `Topic.publish`, because the two answer
        different questions: a topic is asked again on the next tick and wants to
        know whether to bother, while a summary is posted once and its caller
        only wants to know whether it got there. The distinction between a
        refusal and a failure survives as the level it is logged at — a missing
        permission is a deployment to go and fix, and a 500 is Discord having a
        moment.
        """
        try:
            await channel.send(text)
        except discord.Forbidden:
            logger.warning(
                "Not allowed to post in '%s'; %s will not get its summaries there. "
                "The bot needs Send Messages on the channel.",
                channel,
                server,
            )
            return False
        except discord.HTTPException as exc:
            if exc.status == REFUSED:
                logger.error("Discord will not take a message for '%s': %s", channel, exc)
                return False

            logger.warning("Could not post to '%s': %s", channel, exc)
            return False
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("Could not reach Discord to post to '%s': %s", channel, exc)
            return False

        return True


def split(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """
    One body as the messages it has to be sent in.

    Cut at the largest boundary that falls inside the limit, so a summary breaks
    between paragraphs wherever it can and between words at worst. A run of text
    longer than the limit with no boundary in it at all — which is not something
    prose does — is cut at the limit rather than left to be refused.
    """
    remaining = text.strip()
    if len(remaining) <= limit:
        return [remaining] if remaining else []

    pieces: list[str] = []

    while len(remaining) > limit:
        cut = _boundary(remaining, limit)
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        pieces.append(remaining)

    return pieces


def _boundary(text: str, limit: int) -> int:
    """Where to cut, preferring the boundary a reader would have paused at."""
    for boundary in BOUNDARIES:
        cut = text.rfind(boundary, 0, limit)
        if cut > 0:
            return cut

    return limit
