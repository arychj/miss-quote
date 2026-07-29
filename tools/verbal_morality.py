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
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from tools.base import Speaker, Tool
from transcript.writer import TranscriptSession, Utterance
from tts.cache import shared_cache
from utils.logging import get_logger

logger = get_logger(__name__)

WORDS_KEY = "words"
ANNOUNCEMENT_KEY = "announcement"
CHIME_KEY = "chime"

# The default lives here rather than in the config file so a server electing
# into the tool only has to say which words it objects to.
DEFAULT_ANNOUNCEMENT = (
    "{user}, you are fined {credits} for a violation of the verbal morality statute."
)

USER_FIELD = "user"
CREDITS_FIELD = "credits"

# Matching on whole words only. A substring match fines the innocent, and the
# canonical example — Scunthorpe — is a place people live.
WORD_BOUNDARY = r"\b"
ALTERNATION = "|"

SINGLE_CREDIT = 1
CREDIT_NOUN = "credit"
CREDITS_NOUN = "credits"

OFFENCE_SEPARATOR = ", "

# Stand in for a speaker and their fine while the announcement is checked at
# startup.
PROBE_NAME = "someone"
PROBE_CREDITS = f"{SINGLE_CREDIT} {CREDIT_NOUN}"


class VerbalMorality(Tool):
    """Fines a speaker, out loud, for saying something the server forbids."""

    name = "verbal-morality"

    def __init__(self, server: str, config: Mapping[str, Any], speaker: Speaker) -> None:
        super().__init__(server, config, speaker)

        self._forbidden = _pattern(config.get(WORDS_KEY))
        self._announcement = _checked(config.get(ANNOUNCEMENT_KEY) or DEFAULT_ANNOUNCEMENT)
        self._speech = shared_cache()
        self._chime = self._located(config.get(CHIME_KEY))

    def _located(self, chime: Any) -> str | None:
        """
        The clip to play ahead of an announcement, if one is configured.

        Looked for at startup so a name that is not there is a line in the log
        on the way up rather than a discovery made the first time someone
        swears, but reported rather than raised on, and kept either way: a
        missing chime should cost the chime, and the file may yet arrive in a
        directory that is usually a mounted volume.
        """
        if chime is None:
            return None

        name = str(chime).strip()
        if not name:
            return None

        path = self._speech.clip_path(name)
        if path is None or not path.is_file():
            logger.warning(
                "[%s] No chime at '%s'; fines will be announced without one.",
                self.server,
                path or name,
            )

        return name

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None:
        """
        Announce one fine for an offending utterance.

        One announcement however many words were in it, and one credit for
        each. A speaker who strings four together has earned four credits, but
        four announcements over the top of each other is a denial of service on
        the channel.
        """
        offences = self._forbidden.findall(utterance.text)
        if not offences:
            return

        fine = _fine(len(offences))
        logger.info(
            "🚨 [%s] %s said %s; announcing a fine of %s.",
            self.server,
            utterance.user,
            OFFENCE_SEPARATOR.join(f"'{offence}'" for offence in offences),
            fine,
        )

        announcement = self._announcement.format(
            **{USER_FIELD: utterance.user, CREDITS_FIELD: fine}
        )
        await self.speaker.play(session.source, self._announce(announcement))

    async def _announce(self, announcement: str) -> AsyncIterator[bytes]:
        """
        The chime and then the words, as one clip rather than two.

        Two calls to the speaker would play in order — it holds one lock per
        server — but each arms the player afresh, and the gap between them is
        audible. Chaining them puts the chime in front of the same stream.
        """
        if self._chime is not None:
            chime = await self._speech.clip(self._chime)
            if chime:
                yield chime

        async for chunk in self._speech.stream(announcement):
            yield chunk


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


def _fine(offences: int) -> str:
    """
    The fine as it will be said out loud: one credit per forbidden word.

    The count stays a numeral, which every synthesizer worth pointing this at
    reads as a number. The noun does not get the same treatment — "1 credits"
    is wrong in a way a listener hears.
    """
    noun = CREDIT_NOUN if offences == SINGLE_CREDIT else CREDITS_NOUN

    return f"{offences} {noun}"


def _checked(announcement: str) -> str:
    """
    An announcement template that will interpolate.

    Checked at construction because the alternative is discovering a stray brace
    at the moment someone swears, by which point the tool has one job and cannot
    do it.
    """
    announcement = str(announcement)

    try:
        announcement.format(**{USER_FIELD: PROBE_NAME, CREDITS_FIELD: PROBE_CREDITS})
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"'{ANNOUNCEMENT_KEY}' has a placeholder nothing fills: {exc}. "
            f"Only '{{{USER_FIELD}}}' and '{{{CREDITS_FIELD}}}' are available."
        ) from exc

    return announcement
