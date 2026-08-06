"""
Keeping one message in a text channel and rewriting it in place.

The third way words leave this process, and the counterpart to `bot.topic` and
`bot.announcer`: a topic is one line under a voice channel's name, an
announcement is a message that joins the ones before it, and this is one message
that keeps being edited. It is for text worth reading while it is current and not
worth a channel full of messages afterwards — a running transcript being the one
thing that wants it.

**The message is held in memory and nowhere else.** A restart posts a new one and
leaves the old one where it was, stale and no longer written to. Persisting the
ID would buy an edit across a redeploy at the cost of a file to keep in step with
a channel somebody may have cleared in the meantime, and a stale block that stops
updating is a thing a reader can see for themselves.

**Rate limits are why this exists at all.** Editing a message is a per-channel
bucket of roughly five requests every five seconds, where setting a voice
channel's status is two every ten minutes; that gap is the whole reason a running
transcript is a message rather than a topic. discord.py sleeps out a 429 rather
than raising, so going over does not fail — it silently lags, which is why the
tool that calls this waits out its own interval after each write rather than
writing on a fixed tick. See `Summary._ticking`.

Whatever is shown is cut to Discord's message limit rather than split across
several, unlike an announcement: what is being shown is the current state of
something, and a state cut in half across two messages is two states.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from miss_quote.bot.announcer import MESSAGE_LIMIT
from miss_quote.tools.base import Finder
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# A request Discord will not accept however many times it is sent — the same
# distinction `bot.topic` and `bot.announcer` draw, and for the same reason.
REFUSED = 400

# What a message cut to the limit ends with, so a reader can tell text that ran
# out from text that was cut off.
ELLIPSIS = "…"


class DiscordTicker:
    """Holds one message per channel and edits it as a tool changes its mind."""

    def __init__(self, finder: Finder) -> None:
        # The announcer, which already resolves a channel name against the
        # guilds and is the thing `Finder` was written for. Resolving it twice
        # would be two answers to one question the moment either changed.
        self._finder = finder

        # The message being rewritten, per server and channel. One per pair
        # rather than one per server: two rooms showing two transcripts are two
        # messages, and which channel they are in is what tells them apart.
        self._shown: dict[tuple[str, str], Any] = {}

    async def show(self, server: str, channel: str, text: str) -> bool:
        """
        Rewrite this channel's message, posting one if there is not one yet.

        A message that has gone — deleted by somebody tidying the channel, or
        lost with a channel that was cleared — is posted again rather than
        reported. The point of this is that a room can watch it, and a reader
        who deleted the block has not asked for the feed to stop; they have
        asked for it to stop being where it was.

        Everything else is reported the way an announcement is, since a caller
        that cannot show anything wants to know once rather than to keep being
        told: a missing permission and a body Discord will not parse are both a
        deployment to go and fix.
        """
        target = self._finder.resolve(server, channel)
        if target is None:
            logger.warning(
                "No text channel called '%s' in %s; %d characters were not shown.",
                channel,
                server,
                len(text),
            )
            return False

        held = self._shown.get((server, channel))
        body = trimmed(text)

        if held is None:
            return await self._post(server, channel, target, body)

        try:
            await held.edit(content=body, allowed_mentions=_unmentioned())
        except discord.NotFound:
            logger.info(
                "The message showing %s's transcript in '%s' is gone; posting another.",
                server,
                channel,
            )
            self._shown.pop((server, channel), None)

            return await self._post(server, channel, target, body)
        except discord.Forbidden:
            logger.warning(
                "Not allowed to edit in '%s'; %s will not keep a transcript there. "
                "The bot needs Manage Messages on the channel.",
                channel,
                server,
            )
            return False
        except discord.HTTPException as exc:
            if exc.status == REFUSED:
                logger.error(
                    "Discord will not take %d characters for '%s': %s",
                    len(body),
                    channel,
                    exc,
                )
                return False

            logger.warning("Could not edit the message in '%s': %s", channel, exc)
            return False
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Could not reach Discord to edit the message in '%s': %s", channel, exc
            )
            return False

        return True

    async def clear(self, server: str, channel: str) -> None:
        """
        Delete the message being rewritten, if there is one.

        What the feed is for is a room watching itself, and a room that has
        emptied is not watching anything: what would be left is the last thing
        said before everybody went to bed, sitting in the channel looking
        current. The summary is what the evening leaves behind.

        Nothing is reported. A message somebody deleted first is the state being
        asked for, and everything else is a channel the bot is on its way out of
        — there is no next attempt to make it worth telling anybody about, so a
        failure is a line in the log and a handle let go of either way.
        """
        held = self._shown.pop((server, channel), None)
        if held is None:
            return

        try:
            await held.delete()
        except discord.NotFound:
            logger.debug(
                "The message showing %s's transcript in '%s' was already gone.",
                server,
                channel,
            )
        except discord.Forbidden:
            logger.warning(
                "Not allowed to delete in '%s'; %s's transcript will stay up. "
                "The bot needs Manage Messages on the channel.",
                channel,
                server,
            )
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Could not take %s's transcript out of '%s': %s", server, channel, exc
            )
        else:
            logger.info("Took %s's transcript out of '#%s'.", server, channel)

    async def _post(self, server: str, channel: str, target: Any, body: str) -> bool:
        """
        Put the first message up, and hold on to it for every one after.

        Held only on success, so a channel the bot cannot post in is tried again
        next time rather than remembered as somewhere it already posted.
        """
        try:
            self._shown[(server, channel)] = await target.send(
                body, allowed_mentions=_unmentioned()
            )
        except discord.Forbidden:
            logger.warning(
                "Not allowed to post in '%s'; %s will not get a transcript there. "
                "The bot needs Send Messages on the channel.",
                channel,
                server,
            )
            return False
        except discord.HTTPException as exc:
            if exc.status == REFUSED:
                logger.error(
                    "Discord will not take %d characters for '%s': %s",
                    len(body),
                    channel,
                    exc,
                )
                return False

            logger.warning("Could not post to '%s': %s", channel, exc)
            return False
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("Could not reach Discord to post to '%s': %s", channel, exc)
            return False

        logger.info("Showing %s's transcript in '#%s'.", server, channel)

        return True


def _unmentioned() -> discord.AllowedMentions:
    """
    Nothing in a shown message pings anybody.

    Belt and braces beside the code fence the caller wraps a transcript in: a
    fence already stops a mention being parsed, and a caller that forgets one
    should still not be able to ping a room by transcribing somebody saying
    "at everyone" out loud.
    """
    return discord.AllowedMentions.none()


def trimmed(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """
    One body as much of it as Discord will take, cut at the end.

    Cut rather than split, unlike an announcement. What is being shown is the
    current state of something and the newest line is the one being watched, so
    a body over the limit loses its front rather than becoming a second message
    nobody is looking at. The caller does the real trimming, which is per line
    and knows what a line is; this is the ceiling underneath it.
    """
    if len(text) <= limit:
        return text

    return ELLIPSIS + text[-(limit - len(ELLIPSIS)) :]
