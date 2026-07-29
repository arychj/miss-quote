"""
The Verbal Morality Bot, after Demolition Man.

Listens for words the server has decided against and, on hearing one, announces
the fine out loud in the channel it was said in. The credits are imaginary and
no tally is kept: the point is being caught, not the accounting.

The name in the announcement is the one the transcript uses, which is the roster
name from `users` where a server has set one and the Discord display name
otherwise. Nothing has to be configured twice.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from tools.base import Speaker, Tool
from transcript.writer import TranscriptSession, Utterance
from tts.cache import shared_cache
from utils.logging import get_logger

logger = get_logger(__name__)

WORDS_KEY = "words"
ANNOUNCEMENT_KEY = "announcement"

# The default lives here rather than in the config file so a server electing
# into the tool only has to say which words it objects to.
DEFAULT_ANNOUNCEMENT = (
    "{user}, you are fined one credit for a violation of the verbal morality statute."
)

USER_FIELD = "user"

# Matching on whole words only. A substring match fines the innocent, and the
# canonical example — Scunthorpe — is a place people live.
WORD_BOUNDARY = r"\b"
ALTERNATION = "|"

# Stands in for a speaker while the announcement is checked at startup.
PROBE_NAME = "someone"


class VerbalMorality(Tool):
    """Fines a speaker, out loud, for saying something the server forbids."""

    name = "verbal-morality"

    def __init__(self, server: str, config: Mapping[str, Any], speaker: Speaker) -> None:
        super().__init__(server, config, speaker)

        self._forbidden = _pattern(config.get(WORDS_KEY))
        self._announcement = _checked(config.get(ANNOUNCEMENT_KEY) or DEFAULT_ANNOUNCEMENT)
        self._speech = shared_cache()

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None:
        """
        Announce one fine for an offending utterance.

        One announcement however many words were in it. A speaker who strings
        four together earns four credits in spirit, but four announcements over
        the top of each other is a denial of service on the channel.
        """
        offence = self._forbidden.search(utterance.text)
        if offence is None:
            return

        logger.info(
            "🚨 [%s] %s said '%s'; announcing the fine.",
            self.server,
            utterance.user,
            offence.group(),
        )

        announcement = self._announcement.format(**{USER_FIELD: utterance.user})
        await self.speaker.play(session.source, self._speech.stream(announcement))


def _pattern(words: Any) -> re.Pattern[str]:
    """
    One expression matching any forbidden word.

    Compiled once here rather than per utterance, and raised on rather than
    tolerated when empty: a tool listening for nothing is configured, enabled,
    and useless, which is worth a line at startup instead of silence forever.
    """
    if isinstance(words, str):
        words = [words]

    if not isinstance(words, Sequence):
        raise ValueError(f"'{WORDS_KEY}' must be a list of words to listen for.")

    unique = sorted({str(word).strip().casefold() for word in words if str(word).strip()})
    if not unique:
        raise ValueError(f"'{WORDS_KEY}' is empty, so there is nothing to listen for.")

    alternatives = ALTERNATION.join(re.escape(word) for word in unique)
    return re.compile(
        f"{WORD_BOUNDARY}(?:{alternatives}){WORD_BOUNDARY}", re.IGNORECASE
    )


def _checked(announcement: str) -> str:
    """
    An announcement template that will interpolate.

    Checked at construction because the alternative is discovering a stray brace
    at the moment someone swears, by which point the tool has one job and cannot
    do it.
    """
    announcement = str(announcement)

    try:
        announcement.format(**{USER_FIELD: PROBE_NAME})
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"'{ANNOUNCEMENT_KEY}' has a placeholder nothing fills: {exc}. "
            f"Only '{{{USER_FIELD}}}' is available."
        ) from exc

    return announcement
