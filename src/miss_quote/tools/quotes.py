"""
Answers the channel with the film line it just walked into.

Listens for a trigger phrase and, on hearing one, says the associated quote out
loud where it was said. The pairs come from a CSV — a film, the phrase that sets
it off, and the line — so adding a quote is a row rather than a deployment.

A trigger that has just fired goes quiet for a while — five minutes by default.
The joke is the recognition, and a channel that says "cool" four times in a
minute does not want "Shiny." four times back. The backoff is per trigger rather
than per speaker: what wears out is the line, not the person who set it off. See
`RecentQuotes`.

Because both the triggers and the lines are a closed set known before anybody
speaks, the whole list can be rendered at startup rather than while the channel
waits for it. See `prewarm`.
"""

from __future__ import annotations

import csv
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miss_quote.config import quotes_cfg
from miss_quote.tools.base import Speaker, Tool
from miss_quote.transcript.writer import TranscriptSession, Utterance
from miss_quote.tts.cache import shared_cache
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

MOVIE_COLUMN = "movie"
TRIGGER_COLUMN = "trigger"
QUOTE_COLUMN = "quote"
COLUMNS = (MOVIE_COLUMN, TRIGGER_COLUMN, QUOTE_COLUMN)

FILE_ENCODING = "utf-8"
COLUMN_SEPARATOR = ", "

# Row 1 is the header, which `DictReader` consumes, so the first row of data is
# the second line of the file. Counting the way an editor does is the point: a
# reported line number nobody can go and look at is not worth reporting.
FIRST_ROW = 2

# A line that names whoever set it off. The only field a quote can interpolate:
# the roster is the one thing knowable about a speaker, and a quote that could
# reach anything else would be a template rather than a line from a film.
USER_FIELD = "user"
USER_PLACEHOLDER = f"{{{USER_FIELD}}}"

# Stands in for a speaker while a quote is checked at load.
PROBE_NAME = "someone"

# Matching on whole words only, so "real" does not fire inside "really". The
# triggers are ordinary English and several are substrings of ordinary speech.
WORD_BOUNDARY = r"\b"
ALTERNATION = "|"

TRIGGER_SEPARATOR = ", "


@dataclass(frozen=True)
class Quote:
    """One row of the file: what sets a line off, and the line."""

    movie: str
    trigger: str
    text: str

    @property
    def personal(self) -> bool:
        """Whether the line names whoever set it off."""
        return USER_PLACEHOLDER in self.text

    def wording(self, user: str) -> str:
        """
        The line as it will be said, for one speaker.

        The pre-warm renders exactly this, so the two must agree down to the
        character: a phrase that differs by a space is one that was synthesized
        at startup and then synthesized again on the way to being played.
        """
        return self.text.format(**{USER_FIELD: user})


class RecentQuotes:
    """
    Which triggers are spent, and for how long.

    In memory only, and per tool instance, which is per server: two channels
    arriving at the same line ten seconds apart have each made the joke once.

    Keyed on the trigger rather than the quote, so two phrases that answer with
    the same line are two jokes and cool down separately. Nothing sweeps this;
    a trigger's timestamp is dropped when it is next read, and there are only as
    many keys as the file has rows.
    """

    def __init__(self, window_seconds: float | None = None) -> None:
        self._window = (
            quotes_cfg.backoff_seconds if window_seconds is None else window_seconds
        )
        self._fired: dict[str, float] = {}

    @property
    def window(self) -> float:
        """How long a fired trigger stays spent, for whoever has to explain it."""
        return self._window

    def ready(self, trigger: str, now: float | None = None) -> bool:
        """
        Whether a trigger may fire, forgetting it if its window has passed.

        Read before the firing is recorded, so the first utterance of a phrase
        is answered and the next one inside the window is not.
        """
        fired = self._fired.get(trigger)
        if fired is None:
            return True

        # Monotonic rather than wall clock, so a clock correction cannot park a
        # trigger in the future and silence it until the clock arrives.
        moment = time.monotonic() if now is None else now
        if moment - fired < self._window:
            return False

        self._fired.pop(trigger, None)
        return True

    def record(self, trigger: str, now: float | None = None) -> None:
        """Note that a trigger has just fired."""
        self._fired[trigger] = time.monotonic() if now is None else now


class Quotes(Tool):
    """Answers a trigger phrase with the film line it belongs to."""

    name = "quotes"

    def __init__(
        self,
        server: str,
        config: Mapping[str, Any],
        speaker: Speaker,
        users: Mapping[int, str] | None = None,
    ) -> None:
        super().__init__(server, config, speaker, users)

        self._quotes = _load(quotes_cfg.file)
        self._triggers = _pattern(self._quotes)
        self._speech = shared_cache()
        self._recent = RecentQuotes()

        logger.debug(
            "[%s] Listening for %d triggers: %s",
            self.server,
            len(self._quotes),
            TRIGGER_SEPARATOR.join(self._quotes),
        )

    async def prewarm(self) -> None:
        """
        Render every line the file holds.

        Unlike a fine, a quote is knowable in full before anybody speaks: the
        triggers are a closed set and so are the answers. Synthesis is the slow
        part of answering, and a callback that arrives four seconds after the
        line it answers is not a callback.

        The exception is a line that names whoever set it off, which is rendered
        once per name on the roster. Somebody the server has not written down
        waits for the synthesizer the first time, and nobody waits again.

        Serial, and unhurried. Nothing is waiting on this, and a synthesizer
        asked for fifty phrases at once is one not answering whoever is speaking
        right now.
        """
        names = sorted(set(self.users.values()))
        rendered = 0
        wanted = 0

        for quote in self._quotes.values():
            for wording in self._wordings(quote, names):
                wanted += 1
                if await self._speech.warm(wording):
                    rendered += 1

        logger.info(
            "[%s] Pre-warmed %d quotes for %d speaker(s): %d rendered, %d already cached.",
            self.server,
            len(self._quotes),
            len(names),
            rendered,
            wanted - rendered,
        )

    @staticmethod
    def _wordings(quote: Quote, names: Sequence[str]) -> tuple[str, ...]:
        """
        Every way one quote can come out, given who is on the roster.

        One phrase for a line that names nobody, however many people are in the
        channel, and one per name for a line that does.
        """
        if not quote.personal:
            return (quote.text,)

        return tuple(quote.wording(name) for name in names)

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None:
        """
        Answer one trigger in an utterance, if any of them is still fresh.

        One line however many triggers were in the sentence: two quotes over the
        top of each other is a denial of service on the channel, and the pause
        while the second one plays has outlasted the joke either way. The
        earliest trigger that is not on backoff wins, so a spent phrase does not
        swallow a live one later in the same sentence.

        The firing is recorded before the line is played rather than after,
        because playing it waits for the channel: a phrase said twice while the
        first answer is still going out should still only be answered once.
        """
        quote = self._match(utterance.text)
        if quote is None:
            return

        self._recent.record(quote.trigger)
        wording = quote.wording(utterance.user)

        logger.info(
            "🎬 [%s] %s said '%s'; quoting %s: %s",
            self.server,
            utterance.user,
            quote.trigger,
            quote.movie,
            wording,
        )

        await self.speaker.play(session.source, self._speech.stream(wording))

    def _match(self, text: str) -> Quote | None:
        """
        The quote to answer an utterance with, or None.

        Matches are walked in the order they were said rather than the order the
        file lists them, so the line that answers is the one whoever spoke
        arrived at first.
        """
        for match in self._triggers.finditer(text):
            trigger = match.group().casefold()
            quote = self._quotes.get(trigger)

            if quote is None:
                continue

            if self._recent.ready(trigger):
                return quote

            logger.debug(
                "[%s] '%s' has been quoted inside the last %.0f seconds; letting it lie.",
                self.server,
                trigger,
                self._recent.window,
            )

        return None


def _load(path: Path) -> Mapping[str, Quote]:
    """
    Every quote in the file, by the trigger that sets it off.

    One trigger per row, and a line may be reached by more than one of them: two
    rows sharing an answer is how the file says that two phrases deserve the same
    reply. The trigger is folded for matching, so a file may write it however it
    reads best.

    A row that is unusable is reported and dropped rather than raised on: a
    typo in one of fifty lines should cost that line. A file that is missing,
    unreadable, or holds no usable row at all is raised on, because a tool
    listening for nothing is enabled and useless, which is worth a line at
    startup instead of silence forever.
    """
    rows = _rows(path)
    quotes: dict[str, Quote] = {}

    for number, row in enumerate(rows, start=FIRST_ROW):
        quote = _quote(path, number, row)
        if quote is None:
            continue

        if quote.trigger in quotes:
            logger.warning(
                "%s line %d: '%s' already answers with %r; ignoring this one.",
                path,
                number,
                quote.trigger,
                quotes[quote.trigger].text,
            )
            continue

        quotes[quote.trigger] = quote

    if not quotes:
        raise ValueError(f"{path} holds no usable quotes, so there is nothing to listen for.")

    logger.info("Loaded %d quotes from %s.", len(quotes), path)

    return quotes


def _rows(path: Path) -> list[Mapping[str, str]]:
    """
    The file's data rows, with its header read as the column names.

    Anything short of the three columns is raised on rather than reported: it is
    not a file with a bad row in it, it is not this file.
    """
    try:
        with path.open(encoding=FILE_ENCODING, newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in COLUMNS if column not in (reader.fieldnames or ())]

            if missing:
                raise ValueError(
                    f"{path} has no {COLUMN_SEPARATOR.join(missing)} column; "
                    f"the header must name {COLUMN_SEPARATOR.join(COLUMNS)}."
                )

            return list(reader)
    except OSError as exc:
        raise ValueError(f"Could not read the quotes at {path}: {exc}") from exc


def _quote(path: Path, number: int, row: Mapping[str, str]) -> Quote | None:
    """
    One row as a quote, or None with a line in the log saying why not.

    A row with nothing to listen for or nothing to say is dropped, as is a line
    carrying a placeholder nothing fills — which is checked here rather than at
    the moment somebody says the trigger, by which point the tool has one job
    and cannot do it.
    """
    trigger = (row.get(TRIGGER_COLUMN) or "").strip()
    text = (row.get(QUOTE_COLUMN) or "").strip()
    movie = (row.get(MOVIE_COLUMN) or "").strip()

    if not trigger or not text:
        logger.warning(
            "%s line %d: a quote needs both a %s and a %s; skipping it.",
            path,
            number,
            TRIGGER_COLUMN,
            QUOTE_COLUMN,
        )
        return None

    try:
        text.format(**{USER_FIELD: PROBE_NAME})
    except (IndexError, KeyError, ValueError) as exc:
        logger.warning(
            "%s line %d: %r has a placeholder nothing fills (%s); "
            "only '%s' is available. Skipping it.",
            path,
            number,
            text,
            exc,
            USER_PLACEHOLDER,
        )
        return None

    return Quote(movie=movie, trigger=trigger.casefold(), text=text)


def _pattern(triggers: Iterable[str]) -> re.Pattern[str]:
    """
    One expression matching any trigger.

    Compiled once at startup rather than per utterance. Longest first, because
    Python's alternation takes the first branch that matches at a position
    rather than the longest: with "monday" ahead of "case of the mondays", a
    case of the Mondays would answer the more general line, and the more
    specific trigger is in the file precisely because it deserves its own.
    """
    ordered = sorted(triggers, key=len, reverse=True)
    alternatives = ALTERNATION.join(re.escape(trigger) for trigger in ordered)

    return re.compile(f"{WORD_BOUNDARY}(?:{alternatives}){WORD_BOUNDARY}", re.IGNORECASE)
