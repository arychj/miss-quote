"""
The standing tally, and getting it where the channel can see it.

A tool that hears nothing and says nothing. What it does is keep one server's
balances, write them down, and put them under the name of whatever voice channel
the bot is sitting in — `Eli: -9 Erik: -2`, which makes the topic the scoreboard,
visible without asking the bot anything.

It is enabled per server like any other tool, and separately from whatever is
counting. A server that wants fines announced but not tallied leaves this off;
`verbal-morality` says so once at startup and goes on announcing them.

**Other tools do the counting through it.** `credit` and `debit` are what a tool
that has decided somebody owes something calls, and they are the whole interface:
where the balance is kept, when it is written, and who is on the board are this
tool's business and not the caller's. Reach it the way anything reaches a
neighbour — `self.tools.find(Scoreboard)`, at the moment you need it.

The tally itself is `ledger.credits`, which knows nothing about Discord, and the
publishing goes through a `Topic`, which is somewhere to put a line. Neither
half of this file imports discord either.

Writing and publishing run on their own intervals, because they are limited by
different things. Writing a few hundred bytes costs nothing, so it happens every
few seconds and a pod killed outright loses seconds of fines. A status edit is
rate-limited, though not nearly as hard as a channel rename: Discord reports a
bucket of roughly six a second, so how often it runs is a question of how often a
tally is worth reading rather than of what the API will tolerate.

Both are driven off the ledger's revision rather than a flag, so a tally that
changed four times between two ticks costs one write and one edit. Somebody
swearing four times in a sentence is one revision bump per utterance and a topic
that is only ever set when it would say something new.

Saving happens first on every tick regardless. An edit that lands in a rate-limit
bucket can hold this task while discord.py sleeps it out, and what that must not
delay is the persistence: a pod terminated while an edit is waiting should still
have the tally on disk from the tick before.
"""

from __future__ import annotations

import asyncio
import time

from miss_quote.config import scoreboard_cfg
from miss_quote.ledger.credits import UNWRITTEN, shared_ledger
from miss_quote.tools.base import Tool, ToolContext
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# An interval at or below this is off rather than continuous.
NEVER = 0.0

# So the first tick publishes, rather than waiting out a topic interval to say
# what the tally already was at startup.
IMMEDIATELY = 0.0

# What a caller that has not said how much means.
SINGLE_CREDIT = 1


class Scoreboard(Tool):
    """Keeps one server's balances, on disk and under the channel's name."""

    name = "scoreboard"

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)

        self._ledger = shared_ledger()
        self._interval = scoreboard_cfg.save_interval_seconds
        self._topic_interval = scoreboard_cfg.topic_interval_seconds
        self._next_topic = IMMEDIATELY
        self._published = UNWRITTEN

        # Enrolled at construction so the topic reads `Eli: 0 Erik: 0` before
        # anybody has been fined, rather than filling in one name at a time as
        # each person earns their first.
        self._ledger.enroll(self.server, self.users)

    # ── what other tools call ─────────────────────

    def debit(self, user_id: int, name: str, amount: int = SINGLE_CREDIT) -> int:
        """
        Take credits off somebody, and report what they have left.

        The name comes in with the change rather than being looked up, because
        the caller has just heard from them and the board prints whatever it was
        last told.
        """
        return self._ledger.debit(self.server, user_id, name, amount)

    def credit(self, user_id: int, name: str, amount: int = SINGLE_CREDIT) -> int:
        """Put credits back on somebody, and report what they have left."""
        return self._ledger.credit(self.server, user_id, name, amount)

    def balance(self, user_id: int) -> int:
        """What somebody has left, which is nothing until they have earned worse."""
        return self._ledger.total(self.server, user_id)

    def standings(self) -> str:
        """The board as it would be published, for anything that wants to say it."""
        return self._ledger.topic(self.server)

    # ── the loop ──────────────────────────────────

    async def run(self) -> None:
        """
        Save and publish whatever has changed, for as long as the bot is up.

        Ticking on the save interval, which is the shorter of the two, and
        publishing on the ticks the topic's own interval has come round for.

        A tick that raises is logged and the loop carries on: a tally is worth
        less than the task that keeps it, and the next tick will pick up whatever
        this one failed to write.
        """
        if not self._counting():
            return

        while True:
            await asyncio.sleep(self._interval)

            try:
                await self._ledger.flush()

                if self._topic_turn_has_come():
                    await self.publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[%s] Could not publish the tally: %s", self.server, exc, exc_info=exc
                )

    def _counting(self) -> bool:
        """
        Whether there is a loop to run at all, saying so either way.

        A deployment that has turned the interval off still counts and still
        writes the tally down on the way out; what it does not do is wake up to
        check. Reported rather than assumed, because a tally that is only ever
        seen after a clean shutdown is a surprising thing to discover.
        """
        if self._interval <= NEVER:
            logger.info(
                "[%s] CREDITS_SAVE_SECONDS is %s; the tally will be kept in memory "
                "and written only on shutdown.",
                self.server,
                self._interval,
            )
            return False

        if self._topic_interval <= NEVER:
            logger.info(
                "[%s] CREDITS_TOPIC_SECONDS is %s; the tally will be kept and "
                "written, but never published to a channel topic.",
                self.server,
                self._topic_interval,
            )

        return True

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

    async def publish(self) -> None:
        """
        Put this server's tally up, if it has changed since it was last up.

        A tally the topic would not take is left unpublished rather than marked
        done, so it lands in the next channel the bot joins instead of waiting
        for somebody to swear again.

        An empty board is not published at all. Only the roster is eligible, so a
        server that has written nobody down has a board that says nothing, and
        setting the status to nothing would wipe whatever a person put there.
        """
        revision = self._ledger.revision_for(self.server)
        if self._published >= revision:
            return

        standings = self.standings()
        if not standings:
            return

        if await self.topic.publish(self.server, standings):
            self._published = revision

    async def close(self) -> None:
        """
        Write the tally down before the loop goes away.

        Saved and not published: the runner has cancelled this tool's own task by
        now, and a channel edit that landed in a rate-limit bucket would sit on
        SIGTERM until the pod was killed outright. What matters at this point is
        the file, which is nobody's rate limit.
        """
        await self._ledger.flush()
