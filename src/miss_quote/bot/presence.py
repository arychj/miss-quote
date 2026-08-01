"""
Saying, under the bot's own name, that a conversation is being kept.

This is a transparency signal rather than a status readout. Being in a voice
channel already means the bot can hear the room — everybody can see it sitting
there — and hearing on its own retains nothing material: a fine is counted and
the words behind it are gone. What is worth announcing is the part that leaves
something afterwards, which is a transcript on disk and the summaries and
retellings written off it. So the status is set when a session is on the record
and cleared when none is, and there is deliberately no second wording for
listening.

It follows sessions rather than speech. A session that is being written down
shows the status whether or not anybody is talking; nothing here is driven by
utterances, which would flicker and spend a rate limit saying nothing new.

**The presence is one per bot, not one per server.** Discord has no per-guild
presence for bots — `change_presence` takes no guild — so a bot in two servers
that is recording in one says so in both. Accepted rather than worked around:
the alternative is one bot application per server. It errs toward saying a
conversation may be kept when it is not, which is the safe direction for a
signal whose whole purpose is that nobody is recorded without being told.
"""

from __future__ import annotations

import asyncio

import discord

from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# What is sent when nothing is being kept. Cleared rather than replaced, on the
# reasoning in the module docstring: there is nothing to say about a bot that is
# only listening, and a status that is always up is one nobody reads.
NOTHING = None


class DiscordPresence:
    """Sets the bot's own status while any conversation is being kept."""

    def __init__(self, client: discord.Client, wording: str) -> None:
        self._client = client
        self._wording = wording
        self._published: bool | None = None

    @property
    def enabled(self) -> bool:
        """
        Whether there is anything to say.

        Wording set to nothing turns the signal off, which is a deployment's own
        business to want and needs no second setting to express.
        """
        return bool(self._wording)

    async def transcribing(self, keeping: bool) -> None:
        """
        Say whether anything is being written down, if that has changed.

        Deduplicated against what was last published, because every caller is a
        lifecycle event rather than a decision to say something: a channel
        filling and emptying drives this several times a minute, and Discord's
        gateway budget for presence is a handful every twenty seconds. Nothing
        is sent for a state already up.

        A failure leaves the published state untouched, so the next transition
        tries again rather than deduplicating against something that never
        landed. A client with no gateway behind it yet is one such failure and
        is checked for rather than raised on: presence rides the websocket, and
        a session opening before the connection is up must not be what stops the
        bot joining the channel.
        """
        if not self.enabled or keeping == self._published:
            return

        if not self._client.is_ready():
            logger.debug("Not connected yet; leaving the status alone.")
            return

        activity = discord.CustomActivity(name=self._wording) if keeping else NOTHING

        try:
            await self._client.change_presence(activity=activity)
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Could not set the bot's status to %s: %s",
                f"'{self._wording}'" if keeping else "nothing",
                exc,
            )
            return

        self._published = keeping

        if keeping:
            logger.info("Status set to '%s'.", self._wording)
        else:
            logger.info("Status cleared; nothing is being kept.")
