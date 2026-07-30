"""
What everybody owes, per server.

Fines used to be announced and forgotten. They are now added up, kept on disk,
and published to the voice channel topic as `Eli: 0 Erik: 0 Luke: 0 Ryan: 0` —
which makes the topic the scoreboard, visible without asking the bot anything.

The count is per server. The same person swearing in two servers owes two
separate debts, because a server's tally is its own business and its words are
too: one may object to nothing the other does.

Keyed by user ID, and only rendered by name. Discord names change, roster names
get rewritten, and neither should hand somebody else's debt to whoever inherited
their nickname; the name stored beside a total is the one to print, refreshed
each time its owner earns something.

Nothing here touches Discord or the event loop. Changes bump a revision, and
whoever is publishing or persisting compares that against what it last wrote
out; see `bot.scoreboard`. That is what keeps a tally that changed four times
between two ticks to one topic edit and one write.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import morality_cfg
from utils.logging import get_logger

logger = get_logger(__name__)

NAME_FIELD = "name"
CREDITS_FIELD = "credits"

NO_CREDITS = 0

# Where a revision starts, and so what a consumer that has published nothing
# yet can hold as its mark: the first change is the first thing worth writing.
UNWRITTEN = 0
FIRST_CHANGE = 1

ENCODING = "utf-8"
INDENT = 2
PARTIAL_SUFFIX = ".partial"

# "{alias}: {credits}", one per person, in one line.
ENTRY = "{name}: {credits}"
ENTRY_SEPARATOR = " "

SERVER_SEPARATOR = ", "
NO_SERVERS = "none"

# Discord's ceiling on a channel topic. A tally that outgrows it is cut on an
# entry boundary rather than mid-number, which would read as a wrong total.
TOPIC_LIMIT = 1024
TOPIC_TRUNCATED = "…"


@dataclass
class Account:
    """One person's debt to one server, and the name to announce it under."""

    name: str
    credits: int = NO_CREDITS


class CreditLedger:
    """
    Every server's tally, loaded at startup and written back as it changes.

    One instance serves the process. Tools are built per server and hold their
    own handle on it, keyed by the alias they were built with, so two servers
    never see each other's totals.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(morality_cfg.credits_file if path is None else path)
        self._servers: dict[str, dict[int, Account]] = {}
        self._revision = UNWRITTEN
        self._changed: dict[str, int] = {}

        self._load()

    # ── the tally ─────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def revision(self) -> int:
        """Bumped by every change, so a consumer can tell it has fallen behind."""
        return self._revision

    def revision_for(self, server: str) -> int:
        """The revision one server's tally last changed at."""
        return self._changed.get(server, UNWRITTEN)

    def servers(self) -> tuple[str, ...]:
        return tuple(self._servers)

    def enroll(self, server: str, users: Mapping[int, str]) -> None:
        """
        Put a server's roster on the board at nothing owed.

        So a channel topic reads `Eli: 0 Erik: 0` before anybody has sworn, which
        is both the point of a scoreboard and the only way to tell the tool is
        watching. A total already on the books survives, and a name that has been
        rewritten in the roster since is brought up to date.

        Anybody not on the roster is added when they earn something, under
        whatever Discord reports.
        """
        accounts = self._servers.setdefault(server, {})
        changed = False

        for user_id, name in users.items():
            account = accounts.get(user_id)

            if account is None:
                accounts[user_id] = Account(name=str(name))
                changed = True
            elif account.name != str(name):
                account.name = str(name)
                changed = True

        if changed:
            self._bump(server)

    def award(self, server: str, user_id: int, name: str, credits: int) -> int:
        """
        Add to what somebody owes, and report the new total.

        The name is refreshed on the way past: it is what the topic prints, and
        the one that arrived with the fine is the most recent thing anybody knows
        about what to call them.
        """
        accounts = self._servers.setdefault(server, {})
        account = accounts.get(user_id)

        if account is None:
            account = Account(name=name)
            accounts[user_id] = account

        account.name = name
        account.credits += credits
        self._bump(server)

        return account.credits

    def total(self, server: str, user_id: int) -> int:
        account = self._servers.get(server, {}).get(user_id)

        return NO_CREDITS if account is None else account.credits

    def topic(self, server: str) -> str:
        """
        One server's tally, as the line that goes in the channel topic.

        Ordered by name rather than by what is owed. A leaderboard would be the
        more natural read, but it reshuffles every time somebody passes somebody
        else, and a topic that rearranges itself is one nobody can read at a
        glance to find themselves.
        """
        accounts = sorted(
            self._servers.get(server, {}).values(), key=lambda account: account.name.casefold()
        )

        return _within_limit(
            ENTRY.format(name=account.name, credits=account.credits)
            for account in accounts
        )

    def _bump(self, server: str) -> None:
        self._revision += FIRST_CHANGE
        self._changed[server] = self._revision

    # ── disk ──────────────────────────────────────

    def _load(self) -> None:
        """
        Read the tally back, or start from nothing.

        A file that will not parse is reported and ignored rather than raised on.
        The alternative is a pod that will not start because a tally of imaginary
        money got corrupted, and the tally is the one thing here that can be
        rebuilt by swearing.
        """
        if not self._path.is_file():
            logger.info("No credit ledger at %s yet; starting from nothing.", self._path)
            return

        try:
            raw = json.loads(self._path.read_text(encoding=ENCODING))
        except (OSError, ValueError) as exc:
            logger.error("Could not read the credit ledger at %s: %s", self._path, exc)
            return

        if not isinstance(raw, Mapping):
            logger.error("The credit ledger at %s is not a mapping; ignoring it.", self._path)
            return

        for server, accounts in raw.items():
            self._servers[str(server)] = self._accounts(str(server), accounts)

        logger.info(
            "Loaded credits for %d server(s) from %s: %s.",
            len(self._servers),
            self._path,
            SERVER_SEPARATOR.join(sorted(self._servers)) or NO_SERVERS,
        )

    def _accounts(self, server: str, raw: Any) -> dict[int, Account]:
        """
        One server's block, skipping whatever will not parse.

        A line nobody can read costs one person's total rather than the file: the
        rest of the server is still owed what it is owed.
        """
        if not isinstance(raw, Mapping):
            logger.error("Server '%s' in the credit ledger is not a mapping; ignoring it.", server)
            return {}

        accounts: dict[int, Account] = {}

        for user, entry in raw.items():
            try:
                accounts[int(user)] = Account(
                    name=str(entry[NAME_FIELD]), credits=int(entry[CREDITS_FIELD])
                )
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                logger.error(
                    "Ignoring '%s' in server '%s' of the credit ledger: %s", user, server, exc
                )

        return accounts

    def save(self) -> None:
        """
        Write the tally out whole, or not at all.

        Synchronous, and small: the caller is expected to hand it to a thread.
        Written through a temporary file and a rename, so a process killed
        mid-write leaves the previous tally rather than half of this one.
        """
        payload = {
            server: {
                str(user_id): {NAME_FIELD: account.name, CREDITS_FIELD: account.credits}
                for user_id, account in sorted(accounts.items())
            }
            for server, accounts in sorted(self._servers.items())
        }

        partial = self._path.with_suffix(PARTIAL_SUFFIX)

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            partial.write_text(
                json.dumps(payload, indent=INDENT, ensure_ascii=False), encoding=ENCODING
            )
            partial.replace(self._path)
        except OSError as exc:
            logger.error("Could not write the credit ledger to %s: %s", self._path, exc)


def _within_limit(entries: Iterable[str]) -> str:
    """
    As many entries as Discord will take in a topic, and a mark if any were cut.

    The limit is generous enough that reaching it takes a server with a hundred
    people in it, all of whom swear; the alternative to cutting is an edit
    Discord rejects, which loses the whole tally rather than the tail of it.
    """
    line = ""

    for entry in entries:
        candidate = f"{line}{ENTRY_SEPARATOR}{entry}" if line else entry
        if len(candidate) > TOPIC_LIMIT - len(TOPIC_TRUNCATED):
            return f"{line}{TOPIC_TRUNCATED}" if line else TOPIC_TRUNCATED
        line = candidate

    return line


_shared: CreditLedger | None = None


def shared_ledger() -> CreditLedger:
    """
    The one ledger in the process.

    Built on first use rather than at import, so nothing reads a file for a
    deployment that has enabled no tool that fines anybody.
    """
    global _shared

    if _shared is None:
        _shared = CreditLedger()

    return _shared
