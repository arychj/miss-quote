"""
Which evening somebody meant, from the words they said after asking.

The tool matches the bot's name and then one of the channel's trigger stems;
everything left over is this module's problem. "What happened" on its own means
the last one, and so does "what happened last time" — the difference between
them is a clause, not a different question, which is why there is one list of
stems rather than one list of whole phrases per date somebody might name.

Three things can follow a stem, and the middle one is the reason this exists:

- nothing, or `last time` — the most recent evening.
- `two weeks ago` — a date, approximately. A channel that meets on a night of
  the week does not meet on the same date, so this lands on the nearest evening
  within a few days rather than on the day itself.
- `on the twelfth` — a date, exactly.

Everything arrives through `phrases.normalized`, so it is lowercase, has no
punctuation, and — this is the part that decides how the ordinals are written —
comes from a transcriber rather than a keyboard. Nobody's ASR writes "the 12th"
when somebody says "the twelfth"; it writes the words out. So the words are what
is matched, and the digits are here only for the transcriber that does not.

A date is never read as further back than the previous month. "The twelfth" is
said by somebody who means one of the last two of them, and a bot that answered
with an evening from the spring would be answering a question nobody asked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

DAYS_IN_A_WEEK = 7
FIRST_DAY_OF_A_MONTH = 1
LAST_DAY_OF_ANY_MONTH = 31

# How near an evening has to be to a date somebody named. Naming a day is exact
# and gets no latitude; counting back weeks is approximate and gets a few days
# either way, because "two weeks ago" means the session two weeks ago and a
# channel that met on a Thursday and then on a Wednesday still met twice.
EXACT_DAY = 0
NEAREST_DAYS = 3


@dataclass(frozen=True)
class When:
    """One answer to "which evening", as something a store can look up."""

    target: date | None
    tolerance_days: int

    # Whether the evening was worked out from silence rather than said. Only
    # true of a trigger with nothing after it at all, which is the one answer
    # here that the next few seconds could still change: an ASR breaks an
    # utterance wherever the speaker paused, so "Miss Quote, what happened" is
    # sometimes the whole question and sometimes the front of one. Anything
    # spelled out — a date, a count of weeks, "last session" — is finished.
    assumed: bool = False

    @property
    def latest(self) -> bool:
        """Whether this is "the last one" rather than a date."""
        return self.target is None


# Asking for the most recent evening, which is also what asking for none of
# them means. One object rather than one per phrase, so a caller can tell the
# default apart from a date without unpacking anything.
#
# Two of them, because those are two situations wherever somebody is waiting:
# `LATEST` is a channel that said "last session" and `UNSAID` is one that has
# not said anything yet and may be about to. They look the same to a lookup and
# different to whatever decides how long to wait; see `Summary._pending`.
LATEST = When(target=None, tolerance_days=EXACT_DAY)
UNSAID = When(target=None, tolerance_days=EXACT_DAY, assumed=True)


# ── the vocabulary ────────────────────────────

# 1 through 9, which are also the tail of 21 through 31.
SINGLE_ORDINALS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
)

# 10 through 19, which are the ones that are not built out of anything.
TEEN_ORDINALS = (
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
)

# The round ones, and the word each of them puts in front of a single.
ROUND_ORDINALS = {"twentieth": 20, "thirtieth": 30}
ROUND_PREFIXES = {"twenty": 20, "thirty": 30}

WEEK_WORDS = {
    "a": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}

# Said instead of "one week ago", and often enough to be worth its own branch.
LAST_WEEK = "last week"

# What "the most recent one" sounds like. `last one` is here because somebody
# who has just been told a story asks for the one before it that way.
LATEST_PHRASES = ("last time", "last session", "last night", "last one")

# Words that can sit between a stem and a clause without changing either. A
# bounded list rather than "any words", because that is the whole defence
# against "what happened to my beer" being read as a question about an evening.
FILLERS = ("the", "on", "in", "from", "back", "about", "of")


def _ordinals() -> Mapping[str, int]:
    """
    Every way of saying a day of the month, and which day it is.

    Composed rather than written out, because thirty-one lines of vocabulary is
    thirty-one chances to give two of them the same number.
    """
    words = {word: day for day, word in enumerate(SINGLE_ORDINALS, start=1)}
    words.update({word: day for day, word in enumerate(TEEN_ORDINALS, start=10)})
    words.update(ROUND_ORDINALS)

    for prefix, base in ROUND_PREFIXES.items():
        for offset, word in enumerate(SINGLE_ORDINALS, start=1):
            if base + offset <= LAST_DAY_OF_ANY_MONTH:
                words[f"{prefix} {word}"] = base + offset

    return words


ORDINALS = _ordinals()


def _weeks() -> Mapping[str, int]:
    """Every way of saying how many weeks back, and how many that is."""
    weeks = {LAST_WEEK: 1}
    weeks.update(
        {
            f"{word} {'week' if count == 1 else 'weeks'} ago": count
            for word, count in WEEK_WORDS.items()
        }
    )

    return weeks


WEEKS = _weeks()


# ── the expression ────────────────────────────

LATEST_GROUP = "latest"
WEEKS_GROUP = "weeks"
DAY_GROUP = "day"

ALTERNATION = "|"

# A day written in digits, for a transcriber that wrote digits. The suffix is
# required: a bare number after a stem is as likely to be a count of something
# as a day of the month, and "recap the three things" is not a date.
DIGIT_ORDINAL = r"\d{1,2}(?:st|nd|rd|th)"
DIGIT_SUFFIX_LENGTH = len("st")


def _alternation(phrases: Iterable[str]) -> str:
    """
    One branch matching any of several phrases, longest first.

    `phrases.pattern`'s rule and for its reason: Python takes the first branch
    that matches at a position rather than the longest, so with "first" ahead of
    "twenty first" the twenty-first would be read as the first.
    """
    ordered = sorted(phrases, key=len, reverse=True)

    return ALTERNATION.join(re.escape(phrase) for phrase in ordered)


def expression() -> re.Pattern[str]:
    """
    What may follow a trigger stem, anchored to the start of what follows it.

    Anchored, not searched. A clause is what the asker said *next*; one found
    four words later belongs to a different part of the sentence, and matching
    it is how "recap the rules, I was out until the twelfth" becomes a request
    for the twelfth.
    """
    fillers = _alternation(FILLERS)

    return re.compile(
        rf"^\s*(?:(?:{fillers})\s+)*"
        rf"(?:"
        rf"(?P<{LATEST_GROUP}>{_alternation(LATEST_PHRASES)})"
        rf"|(?P<{WEEKS_GROUP}>{_alternation(WEEKS)})"
        rf"|(?P<{DAY_GROUP}>{_alternation(ORDINALS)}|{DIGIT_ORDINAL})"
        rf")\b"
    )


_EXPRESSION = expression()


# ── reading one ───────────────────────────────


def parse(said: str, start: int, today: date) -> When | None:
    """
    Which evening the rest of a sentence asked for, if it asked for one.

    `start` is where the trigger stem ended. Nothing after it is the most recent
    evening — somebody who says "Miss Quote, what happened" is asking the same
    question as somebody who adds "last time". Anything else has to be a clause
    this understands, and text that is neither is somebody talking about
    something else in a sentence that happened to begin like a question.

    Nothing after it comes back as `UNSAID` rather than `LATEST`, which is the
    same evening held less firmly: the two are indistinguishable to a lookup,
    and only one of them is an answer the next utterance could still change.
    """
    tail = said[start:]

    if not tail.strip():
        return UNSAID

    matched = _EXPRESSION.match(tail)
    if matched is None:
        return None

    if matched.group(LATEST_GROUP):
        return LATEST

    weeks = matched.group(WEEKS_GROUP)
    if weeks:
        return When(
            target=today - timedelta(days=WEEKS[weeks] * DAYS_IN_A_WEEK),
            tolerance_days=NEAREST_DAYS,
        )

    return _on_day(matched.group(DAY_GROUP), today)


def _on_day(spoken: str, today: date) -> When | None:
    """
    A day of the month, in whichever of the last two months it fell in.

    Strictly earlier in the month than today is this month; anything else is the
    previous one. Strictly, because a day that has not finished is not an
    evening anybody has notes from, and today's own is being had right now.

    A day the resolved month does not have — the thirty-first of a month with
    thirty — is nobody's evening, and answers nothing rather than sliding to a
    neighbouring date somebody did not name.
    """
    day = ORDINALS.get(spoken)
    if day is None:
        day = int(spoken[:-DIGIT_SUFFIX_LENGTH])

    opening = today.replace(day=FIRST_DAY_OF_A_MONTH)
    named = opening if day < today.day else opening - timedelta(days=1)

    try:
        return When(target=named.replace(day=day), tolerance_days=EXACT_DAY)
    except ValueError:
        return None
