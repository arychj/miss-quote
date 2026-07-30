"""
The Verbal Morality Bot, after Demolition Man.

Listens for words the server has decided against and, on hearing one, announces
the fine out loud in the channel it was said in. The credits are imaginary but
they are added up: what everybody owes goes in the voice channel topic, and
`ledger.credits` keeps it across restarts.

A repeat offender is announced more and more quietly. Being fined is the joke,
and a joke told fifteen times in five minutes is a denial of service on the
conversation, so the announcement backs off toward `VIOLATION_VOLUME_FLOOR` as
somebody keeps earning them. See `RecentViolations`.

What the server writes down are stems. Each is expanded at startup into the
endings it is said with, so a list stays a list of words rather than a list of
conjugations; `utils.stems` does the growing.

The name in the announcement is the one the transcript uses, which is the roster
name from `users` where a server has set one and the Discord display name
otherwise. Nothing has to be configured twice.

Because the roster is known before anybody speaks, and so is the shape of the
sentence, most of what the tool will ever have to say can be rendered at startup
rather than while the channel waits for it. See `prewarm`.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from config import UNITY_VOLUME, morality_cfg, tts_cfg
from ledger.credits import shared_ledger
from tools.base import Speaker, Tool
from transcript.writer import TranscriptSession, Utterance
from tts.cache import shared_cache
from utils.logging import get_logger
from utils.stems import expand

logger = get_logger(__name__)

WORDS_KEY = "words"
ANNOUNCEMENT_KEY = "announcement"
CHIME_KEY = "chime"

# The default lives here rather than in the config file so a server electing
# into the tool only has to say which words it objects to.
DEFAULT_ANNOUNCEMENT = (
    "{user}, you are fined {credits} for {violations} of the verbal morality statute."
)

USER_FIELD = "user"
CREDITS_FIELD = "credits"
VIOLATIONS_FIELD = "violations"

FIELD_SEPARATOR = ", "

NO_AUDIO = b""

# Matching on whole words only. A substring match fines the innocent, and the
# canonical example — Scunthorpe — is a place people live.
WORD_BOUNDARY = r"\b"
ALTERNATION = "|"

SINGLE_CREDIT = 1
CREDIT_NOUN = "credit"
CREDITS_NOUN = "credits"

SINGLE_OFFENCE = 1
SINGLE_VIOLATION = "a violation"
MULTIPLE_VIOLATIONS = "multiple violations"

# Violations in one utterance the pre-warm is prepared for. Three covers what a
# sentence usually holds; past it a speaker has said something remarkable and can
# wait for the synthesizer.
FORESEEN_OFFENCES = 3

OFFENCE_SEPARATOR = ", "

# How long a violation counts against how loudly the next one is announced, and
# how much of the announcement each one inside that window costs. Fifteen of
# them reaches the floor, which is a quarter of the way there by default; the
# floor itself is a deployment setting, since a server may want a repeat
# offender silenced rather than merely quietened.
VIOLATION_WINDOW_SECONDS = 300.0
BACKOFF_PER_VIOLATION = 0.05

# A fraction is what the speaker is handed; a percentage is what reads in a log.
PERCENT = 100

# Stand in for a speaker and their fine while the announcement is checked at
# startup.
PROBE_NAME = "someone"
PROBE_CREDITS = f"{SINGLE_CREDIT} {CREDIT_NOUN}"
PROBE_VIOLATIONS = SINGLE_VIOLATION


class RecentViolations:
    """
    How much somebody has sworn lately, and how loudly to say so.

    In memory only, and per tool instance, which is per server: one server's
    patience is not another's, and a tally that survives a restart is the
    credits, not this. Five minutes after the last one, a speaker is back to
    being announced at whatever loudness the channel asked for.

    Timestamps rather than a count, because the window slides: a count would
    have to be reset on a schedule, and the reset would land mid-argument and
    hand somebody a fresh full-volume announcement for their fifteenth swear.
    Kept per user and pruned on the way past, so nothing has to sweep it.
    """

    def __init__(
        self,
        window_seconds: float = VIOLATION_WINDOW_SECONDS,
        step: float = BACKOFF_PER_VIOLATION,
        floor: float | None = None,
    ) -> None:
        self._window = window_seconds
        self._step = step
        self._floor = morality_cfg.volume_floor if floor is None else floor
        self._seen: dict[int, list[float]] = {}

    def scale(self, user_id: int, now: float | None = None) -> float:
        """
        How loud the next announcement for a speaker should be, as a fraction.

        Read before the violation being announced is recorded, so somebody's
        first swear in five minutes is announced at full volume: the backoff is
        for saying it again, and a floor that applied from the first word would
        just be a quieter bot.
        """
        backoff = self._step * self.count(user_id, now)

        return max(self._floor, UNITY_VOLUME - backoff)

    def count(self, user_id: int, now: float | None = None) -> int:
        """Violations still inside the window, dropping the ones that have aged out."""
        recent = self._recent(user_id, now)

        if recent:
            self._seen[user_id] = recent
        else:
            self._seen.pop(user_id, None)

        return len(recent)

    def record(self, user_id: int, violations: int, now: float | None = None) -> None:
        """
        Note violations against a speaker, one timestamp each.

        Each forbidden word counts, on the same terms as the fine: somebody who
        strings four together has earned four credits and four steps of backoff,
        however few announcements it took to say so.
        """
        moment = time.monotonic() if now is None else now
        recent = self._recent(user_id, moment)
        recent.extend([moment] * violations)

        self._seen[user_id] = recent

    def _recent(self, user_id: int, now: float | None) -> list[float]:
        """
        A speaker's violations that are still inside the window.

        Monotonic rather than wall clock, so a clock correction cannot make a
        violation look like it happened in the future and stay in the window
        until it arrives.
        """
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window

        return [seen for seen in self._seen.get(user_id, []) if seen > cutoff]


class VerbalMorality(Tool):
    """Fines a speaker, out loud, for saying something the server forbids."""

    name = "verbal-morality"

    def __init__(
        self,
        server: str,
        config: Mapping[str, Any],
        speaker: Speaker,
        users: Mapping[int, str] | None = None,
    ) -> None:
        super().__init__(server, config, speaker, users)

        self._vocabulary = _vocabulary(config.get(WORDS_KEY))
        self._forbidden = _pattern(self._vocabulary)
        self._announcement = _checked(config.get(ANNOUNCEMENT_KEY) or DEFAULT_ANNOUNCEMENT)
        self._speech = shared_cache()
        self._chime = self._located(config.get(CHIME_KEY))
        self._recent = RecentViolations()

        # Enrolled at construction so the topic reads `Eli: 0 Erik: 0` before
        # anybody has sworn, rather than filling in one name at a time as each
        # person earns their first fine.
        self._credits = shared_ledger()
        self._credits.enroll(self.server, self.users)

        logger.debug(
            "[%s] Listening for %d words: %s",
            self.server,
            len(self._vocabulary),
            OFFENCE_SEPARATOR.join(self._vocabulary),
        )

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

    async def prewarm(self) -> None:
        """
        Render the fines this server can already see coming.

        Every name on the roster against the first few counts of violations,
        which between them are most of what anybody earns. Synthesis is the slow
        part of answering: paying for it at startup is what lets the fine land
        while the offence is still what the channel is talking about.

        Only the roster can be warmed. A speaker the server has not named is
        announced under whatever Discord reports, which is not knowable from here
        and not a closed set; they pay for their first fine, and nobody pays for
        it again.

        Serial, and unhurried. Nothing is waiting on this, and a synthesizer
        asked for a hundred phrases at once is a synthesizer not answering
        whoever is speaking right now.
        """
        names = sorted(set(self.users.values()))
        if not names:
            logger.debug(
                "[%s] No roster, so there are no fines to render in advance.", self.server
            )
            return

        offences = range(SINGLE_OFFENCE, FORESEEN_OFFENCES + 1)
        rendered = 0

        for name in names:
            for count in offences:
                if await self._speech.warm(self._wording(name, count)):
                    rendered += 1

        logger.info(
            "[%s] Pre-warmed announcements for %d speaker(s): "
            "%d rendered, %d already cached.",
            self.server,
            len(names),
            rendered,
            len(names) * len(offences) - rendered,
        )

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None:
        """
        Announce one fine for an offending utterance, and add it to the tally.

        One announcement however many words were in it, and one credit for
        each. A speaker who strings four together has earned four credits, but
        four announcements over the top of each other is a denial of service on
        the channel.

        The loudness is read before the violations are recorded, so the first
        swear in five minutes is announced at full volume and each one after it
        is quieter. The tally is added to whether or not the announcement is
        audible: what somebody owes is not a function of how loudly they were
        told about it.
        """
        offences = self._forbidden.findall(utterance.text)
        if not offences:
            return

        scale = self._recent.scale(utterance.user_id)
        self._recent.record(utterance.user_id, len(offences))

        fine = _fine(len(offences))
        owed = self._credits.award(
            self.server, utterance.user_id, utterance.user, len(offences)
        )

        logger.info(
            "🚨 [%s] %s said %s; announcing a fine of %s at %d%% volume (%d owed).",
            self.server,
            utterance.user,
            OFFENCE_SEPARATOR.join(f"'{offence}'" for offence in offences),
            fine,
            round(scale * PERCENT),
            owed,
        )

        await self.speaker.play(
            session.source,
            self._announce(self._wording(utterance.user, len(offences))),
            scale,
        )

    def _wording(self, user: str, offences: int) -> str:
        """
        The announcement as it will be said, for one speaker and one count.

        The pre-warm renders exactly this, so the two must agree down to the
        character: a phrase that differs by a space is a phrase that was
        synthesized at startup and then synthesized again on the way to being
        played.
        """
        return self._announcement.format(
            **{
                USER_FIELD: user,
                CREDITS_FIELD: _fine(offences),
                VIOLATIONS_FIELD: _violations(offences),
            }
        )

    async def _announce(self, announcement: str) -> AsyncIterator[bytes]:
        """
        The chime and then the words, as one clip rather than two.

        Two calls to the speaker would play in order — it holds one lock per
        server — but each arms the player afresh, and the gap between them is
        audible. Chaining them puts the chime in front of the same stream.

        The words are given a head start before the chime is handed over. A
        synthesizer is free to render a phrase whole before sending any of it,
        and the chime is short; starting it the moment it is read would leave
        the player waiting between the flourish and the sentence it introduces.
        Waiting first spends that time before anything is playing.
        """
        chime = await self._speech.clip(self._chime) if self._chime else NO_AUDIO
        words = self._speech.stream(announcement)
        lead = await _lead(words, tts_cfg.lead_bytes)

        if chime:
            yield chime

        for chunk in lead:
            yield chunk

        async for chunk in words:
            yield chunk


async def _lead(speech: AsyncIterator[bytes], wanted: int) -> list[bytes]:
    """
    Pull from a stream until it has given up `wanted` bytes or run out.

    The chunks are handed back rather than joined, so nothing is copied and a
    short phrase that ends inside the head start is not padded out to it. The
    stream is left where it stopped for the caller to finish draining.
    """
    if wanted <= 0:
        return []

    lead: list[bytes] = []
    held = 0

    async for chunk in speech:
        lead.append(chunk)
        held += len(chunk)
        if held >= wanted:
            break

    return lead


def _vocabulary(words: Any) -> tuple[str, ...]:
    """
    Every form of every word a server objects to.

    What the config file lists are stems, not the whole conjugation: a server
    that objects to a word objects to it in the past tense as well, and a list
    that has to spell out every ending is one somebody will get around a week
    after writing it.

    Raised on rather than tolerated when empty: a tool listening for nothing is
    configured, enabled, and useless, which is worth a line at startup instead
    of silence forever.
    """
    if isinstance(words, str):
        words = [words]

    if not isinstance(words, Sequence):
        raise ValueError(f"'{WORDS_KEY}' must be a list of words to listen for.")

    stems = {str(word).strip().casefold() for word in words if str(word).strip()}
    if not stems:
        raise ValueError(f"'{WORDS_KEY}' is empty, so there is nothing to listen for.")

    return tuple(sorted({form for stem in stems for form in expand(stem)}))


def _pattern(vocabulary: Sequence[str]) -> re.Pattern[str]:
    """
    One expression matching any forbidden word.

    Compiled once at startup rather than per utterance. The order of the
    alternatives does not matter despite the leftmost-first match: the trailing
    boundary rejects a short form that has landed inside a longer one, so
    "fucking" is not matched as "fuck" with a tail left over.
    """
    alternatives = ALTERNATION.join(re.escape(word) for word in vocabulary)

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


def _violations(offences: int) -> str:
    """
    What the announcement calls the offence, in the plural where it earned one.

    A phrase rather than a count: the number is already in the fine, and saying
    it twice makes the announcement sound like an invoice.
    """
    return SINGLE_VIOLATION if offences == SINGLE_OFFENCE else MULTIPLE_VIOLATIONS


def _checked(announcement: str) -> str:
    """
    An announcement template that will interpolate.

    Checked at construction because the alternative is discovering a stray brace
    at the moment someone swears, by which point the tool has one job and cannot
    do it.
    """
    announcement = str(announcement)

    try:
        announcement.format(
            **{
                USER_FIELD: PROBE_NAME,
                CREDITS_FIELD: PROBE_CREDITS,
                VIOLATIONS_FIELD: PROBE_VIOLATIONS,
            }
        )
    except (IndexError, KeyError, ValueError) as exc:
        available = FIELD_SEPARATOR.join(
            f"'{{{field}}}'" for field in (USER_FIELD, CREDITS_FIELD, VIOLATIONS_FIELD)
        )
        raise ValueError(
            f"'{ANNOUNCEMENT_KEY}' has a placeholder nothing fills: {exc}. "
            f"Only {available} are available."
        ) from exc

    return announcement
