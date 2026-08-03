"""
Matching a set phrase against something an ASR wrote down.

Three tools now listen for wordings a person said out loud and a transcriber
guessed at, and all of them want the same three things: text with the punctuation
taken out of it, one expression that matches any of a list of phrases, and a way
to read that list out of a server's config file. The functions started out in
`tools.quotes` and `tools.summary` and are here so that the next tool to need one
is not another copy of it.

Normalizing is what makes "What's Firefly?" and "what is firefly" the same
sentence, and what lets a phrase be written down with the apostrophes it
deserves. An apostrophe closes the gap rather than opening one, so a possessive
survives a transcript that dropped the mark.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

ELIDED = re.compile(r"['‘’ʼ`]+")
UNSPOKEN = re.compile(r"[^a-z0-9]+")
SPACE = " "
NOTHING = ""

WORD_BOUNDARY = r"\b"
ALTERNATION = "|"


def normalized(text: str) -> str:
    """
    Text as it is matched: letters and digits, lowercase, single-spaced.

    Punctuation is dropped rather than escaped, which is what makes "What's
    Firefly?" and "what is firefly" the same answer, and what lets a title be
    written with the apostrophes and colons it deserves.
    """
    return UNSPOKEN.sub(SPACE, ELIDED.sub(NOTHING, text.casefold())).strip()


def pattern(phrases: Iterable[str]) -> re.Pattern[str]:
    """
    One expression matching any of several phrases.

    Compiled once at startup rather than per utterance. Longest first, because
    Python's alternation takes the first branch that matches at a position
    rather than the longest: with "monday" ahead of "case of the mondays", a
    case of the Mondays would answer the more general line, and the more
    specific phrase is in the list precisely because it deserves its own.
    """
    ordered = sorted(phrases, key=len, reverse=True)
    alternatives = ALTERNATION.join(re.escape(phrase) for phrase in ordered)

    return re.compile(f"{WORD_BOUNDARY}(?:{alternatives}){WORD_BOUNDARY}", re.IGNORECASE)


def spoken(key: str, value: Any, default: Sequence[str]) -> tuple[str, ...]:
    """
    A list of phrases a server said it listens for, or the shipped one.

    Replaced rather than added to: these are a matching vocabulary, and a server
    that renamed the bot or reworded a question means the old wording to stop
    working. A single string is read as a list of one, since writing one phrase
    should not require remembering it is a list.

    Normalized on the way in, so what is matched against a transcript was written
    down the way the transcript will be, and a phrase written with the apostrophe
    it deserves still matches text that lost one.
    """
    if value is None:
        return tuple(default)

    phrases = [value] if isinstance(value, str) else value

    try:
        said = tuple(normalized(str(phrase)) for phrase in phrases)
    except TypeError as exc:
        raise ValueError(f"'{key}' must be a phrase or a list of them: {exc}") from exc

    heard = tuple(phrase for phrase in said if phrase)
    if not heard:
        raise ValueError(f"'{key}' has nothing in it to listen for")

    return heard
