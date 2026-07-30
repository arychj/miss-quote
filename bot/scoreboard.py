"""
Publishing the credit tally, and keeping it on disk.

The tally itself is `ledger.credits`, which knows nothing about Discord. This is
the other half: a task that wakes on an interval, writes a changed tally out, and
puts it under the name of whatever voice channel the bot is sitting in.

What it sets is the channel's **status**, not its topic. A voice channel has no
topic — `PATCH /channels/{id}` with one is refused, and refused with
`CHANNEL_TOPIC_INVALID`, "Field contains at least one word that is not allowed",
which reads like a profanity filter and is nothing of the kind: the same request
is refused for a topic of "test". The status is the line the client shows beneath
a voice channel's name, which is what a topic looks like on a voice channel and
what somebody setting one by hand would set. Settings and prose here say topic,
because that is what it is to everybody looking at it; only the call itself knows
the difference. Do not "fix" this back to `topic=`.

Both halves are driven off the ledger's revision rather than a flag, so a tally
that changed four times between two ticks costs one write and one edit. Somebody
swearing four times in a sentence is one revision bump per utterance and a status
that is only ever set when it would say something new.

They run on their own intervals, because they are limited by different things.
Writing a few hundred bytes costs nothing, so it happens every few seconds and a
pod killed outright loses seconds of fines. A status edit is rate-limited, though
not nearly as hard as a channel rename: Discord reports a bucket of roughly six a
second, so how often it runs is a question of how often a tally is worth reading
rather than of what the API will tolerate.

Saving happens first on every tick regardless. An edit that lands in a rate-limit
bucket can hold this task while discord.py sleeps it out, and what that must not
delay is the persistence: a pod terminated while an edit is waiting should still
have the tally on disk from the tick before.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import discord

from config import file_cfg, morality_cfg
from ledger.credits import UNWRITTEN, CreditLedger
from utils.logging import get_logger

logger = get_logger(__name__)

# An interval at or below this is off rather than continuous.
NEVER = 0.0

# A request Discord will not accept however many times it is sent. Its own case
# because the alternative is retrying it for the life of the process, at the cost
# of the channel's rate limit, for an answer that cannot come out differently.
REFUSED = 400

# So the first tick publishes, rather than waiting out a topic interval to say
# what the tally already was at startup.
IMMEDIATELY = 0.0


class Scoreboard:
    """Writes the tally to disk and to the voice channel topic as it changes."""

    def __init__(
        self,
        ledger: CreditLedger,
        guilds: Callable[[int], Any | None],
        save_seconds: float | None = None,
        topic_seconds: float | None = None,
    ) -> None:
        # Resolved through a callable for the same reason the speaker is: this is
        # built before the bot whose guilds it looks things up in.
        self._ledger = ledger
        self._guilds = guilds
        self._interval = (
            morality_cfg.save_interval_seconds if save_seconds is None else save_seconds
        )
        self._topic_interval = (
            morality_cfg.topic_interval_seconds if topic_seconds is None else topic_seconds
        )
        self._next_topic = IMMEDIATELY
        self._saved = UNWRITTEN
        self._published: dict[str, int] = {}

    async def run(self) -> None:
        """
        Save and publish whatever has changed, for as long as the bot is up.

        Ticking on the save interval, which is the shorter of the two, and
        publishing on the ticks the topic's own interval has come round for.

        A tick that raises is logged and the loop carries on: a tally is worth
        less than the task that keeps it, and the next tick will pick up whatever
        this one failed to write.
        """
        while True:
            await asyncio.sleep(self._interval)

            try:
                await self.persist()

                if self._topic_turn_has_come():
                    await self.publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Could not publish the credit tally: %s", exc, exc_info=exc)

    def _topic_turn_has_come(self, now: float | None = None) -> bool:
        """
        Whether the topic's interval has come round again, claiming it if so.

        A query that books the next turn, which is what keeps the cadence to the
        interval rather than to the interval plus however long an edit spent
        waiting out a rate limit.
        """
        if self._topic_interval <= NEVER:
            return False

        moment = time.monotonic() if now is None else now
        if moment < self._next_topic:
            return False

        self._next_topic = moment + self._topic_interval

        return True

    async def persist(self) -> None:
        """
        Write the tally out if it has changed since the last time it was written.

        The revision is read before the write rather than after, so a fine landing
        while the file is being written is left looking unsaved and is picked up
        by the next tick, rather than being marked as written and lost.

        On a thread, because this is the event loop the audio arrives on and a
        volume that has gone away can make a small write take a long time.
        """
        revision = self._ledger.revision
        if revision == self._saved:
            return

        await asyncio.to_thread(self._ledger.save)
        self._saved = revision

    async def publish(self) -> None:
        """Put each server's changed tally under the name of the channel the bot is in."""
        for server in self._ledger.servers():
            revision = self._ledger.revision_for(server)
            if self._published.get(server, UNWRITTEN) >= revision:
                continue

            channel = self._channel_for(server)
            if channel is None:
                # Nowhere to publish to. Left unpublished rather than marked
                # done, so the tally lands in the topic of the next channel the
                # bot joins instead of waiting for somebody to swear again.
                continue

            if await self._post(server, channel, self._ledger.topic(server)):
                self._published[server] = revision

    async def _post(self, server: str, channel: Any, tally: str) -> bool:
        """
        Put one tally under a channel's name, reporting whether it can be
        considered up.

        A refusal is treated as final and a failure as temporary. Neither a
        missing permission nor a request Discord will not parse resolves itself,
        and retrying either every tick spends the channel's rate limit on an
        answer that cannot change; anything else — a 500, a timeout, a rate limit
        — is worth another go on the next tick. A tally that changes is published
        either way, because what was refused was this text and the next text is
        not this one.

        Every failure carries the string it was trying to set. A rejection whose
        cause is a name in the tally cannot be diagnosed from the fact of it.
        """
        try:
            await channel.edit(status=tally)
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
                    tally,
                    channel,
                    exc,
                    server,
                )
                return True

            logger.warning(
                "Could not set the status of '%s' to '%s': %s", channel, tally, exc
            )
            return False
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Could not reach Discord to set the status of '%s' to '%s': %s",
                channel,
                tally,
                exc,
            )
            return False

        logger.debug("Published the tally for %s to '%s': %s", server, channel, tally)
        return True

    def _channel_for(self, server: str) -> Any | None:
        """
        The voice channel a server's tally goes in, if the bot is in one.

        Whichever one the bot is currently sitting in: the tally is per server,
        and the bot holds one voice connection per server, so the channel it is
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
