"""
Matching a set phrase against something an ASR wrote down.

Two tools now listen for wordings a person said out loud and a transcriber
guessed at, and both of them want the same two things: text with the punctuation
taken out of it, and one expression that matches any of a list of phrases. The
functions were `tools.quotes`' and are here so that the second tool to need them
is not a second copy of them.

Normalizing is what makes "What's Firefly?" and "what is firefly" the same
sentence, and what lets a phrase be written down with the apostrophes it
deserves. An apostrophe closes the gap rather than opening one, so a possessive
survives a transcript that dropped the mark.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

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
