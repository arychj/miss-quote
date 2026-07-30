"""
Putting a line under the name of the voice channel the bot is sitting in.

The other half of `tools.scoreboard`, which knows what the line says and nothing
about where it goes.

What this sets is the channel's **status**, not its topic. A voice channel has no
topic — `PATCH /channels/{id}` with one is refused, and refused with
`CHANNEL_TOPIC_INVALID`, "Field contains at least one word that is not allowed",
which reads like a profanity filter and is nothing of the kind: the same request
is refused for a topic of "test". The status is the line the client shows beneath
a voice channel's name, which is what a topic looks like on a voice channel and
what somebody setting one by hand would set. Settings and prose elsewhere say
topic, because that is what it is to everybody looking at it; only the call
itself knows the difference. Do not "fix" this back to `topic=`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import discord

from miss_quote.config import file_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# A request Discord will not accept however many times it is sent. Its own case
# because the alternative is retrying it for the life of the process, at the cost
# of the channel's rate limit, for an answer that cannot come out differently.
REFUSED = 400


class DiscordTopic:
    """Puts a server's line under the name of the channel the bot is in."""

    def __init__(self, guilds: Callable[[int], Any | None]) -> None:
        # Resolved through a callable rather than the bot itself, for the same
        # reason the speaker is: this is built before the bot whose guilds it
        # looks things up in.
        self._guilds = guilds

    async def publish(self, server: str, line: str) -> bool:
        """
        Put one line under a channel's name, reporting whether it can be
        considered up.

        A refusal is treated as final and a failure as temporary. Neither a
        missing permission nor a request Discord will not parse resolves itself,
        and retrying either every tick spends the channel's rate limit on an
        answer that cannot change; anything else — a 500, a timeout, a rate limit
        — is worth another go later. A line that changes is published either way,
        because what was refused was this text and the next text is not this one.

        Every failure carries the string it was trying to set. A rejection whose
        cause is a name in the line cannot be diagnosed from the fact of it.
        """
        channel = self._channel_for(server)
        if channel is None:
            return False

        try:
            await channel.edit(status=line)
        except discord.Forbidden:
            logger.warning(
                "Not allowed to set the status of '%s'; the tally for %s will stay off "
                "it. The bot needs Set Voice Channel Status on the channel.",
                channel,
                server,
            )
            return True
        except discord.HTTPException as exc:
            if exc.status == REFUSED:
                logger.error(
                    "Discord will not take '%s' as the status of '%s': %s. "
                    "The tally for %s stays off it until it changes.",
                    line,
                    channel,
                    exc,
                    server,
                )
                return True

            logger.warning(
                "Could not set the status of '%s' to '%s': %s", channel, line, exc
            )
            return False
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Could not reach Discord to set the status of '%s' to '%s': %s",
                channel,
                line,
                exc,
            )
            return False

        logger.debug("Published the tally for %s to '%s': %s", server, channel, line)

        return True

    def _channel_for(self, server: str) -> Any | None:
        """
        The voice channel a server's line goes in, if the bot is in one.

        Whichever one the bot is currently sitting in: a tally is per server, and
        the bot holds one voice connection per server, so the channel it is
        listening in is the one the people it is counting are in.
        """
        server_id = file_cfg.id_for(server)
        if server_id is None:
            return None

        guild = self._guilds(server_id)
        voice_client = getattr(guild, "voice_client", None)

        if voice_client is None or not voice_client.is_connected():
            return None

        return voice_client.channel
