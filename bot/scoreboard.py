"""
Publishing the credit tally, and keeping it on disk.

The tally itself is `ledger.credits`, which knows nothing about Discord. This is
the other half: a task that wakes on an interval, writes a changed tally out, and
puts it in the topic of whatever voice channel the bot is sitting in.

Both are driven off the ledger's revision rather than a flag, so a tally that
changed four times between two ticks costs one write and one edit. Somebody
swearing four times in a sentence is one revision bump per utterance and a topic
that is only ever edited when it would say something new.

The two run on their own intervals, because they are limited by different things.
Writing a few hundred bytes costs nothing and happens every few seconds, so a pod
killed outright loses seconds of fines. A channel topic edit is rate-limited to
roughly a couple per ten minutes per channel, and discord.py answers a 429 by
sleeping until it clears, so five minutes is as fast as the topic can honestly go
— it converges on the tally rather than tracking it.

Saving therefore happens first on every tick. An edit that lands in a bucket can
hold this task for minutes, and what that must not delay is the persistence: a
pod terminated while an edit is waiting should still have the tally on disk from
the tick before.
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
        """Put each server's changed tally in the topic of the channel the bot is in."""
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

    async def _post(self, server: str, channel: Any, topic: str) -> bool:
        """
        Edit one channel topic, reporting whether the tally can be considered up.

        A refusal is treated as final and a failure as temporary. Missing
        `Manage Channels` is not going to resolve itself, and retrying it every
        tick would spend the channel's rate limit on a request that cannot
        succeed; anything else — a 500, a timeout, a rate limit — is worth
        another go on the next tick.
        """
        try:
            await channel.edit(topic=topic)
        except discord.Forbidden:
            logger.warning(
                "Not allowed to set the topic of '%s'; the tally for %s will stay off it. "
                "The bot needs Manage Channels.",
                channel,
                server,
            )
            return True
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
            logger.warning("Could not set the topic of '%s': %s", channel, exc)
            return False

        logger.debug("Published the tally for %s to '%s': %s", server, channel, topic)
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
