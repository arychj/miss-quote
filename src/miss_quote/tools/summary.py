"""
Writes down what happened in a voice channel, and reads it back when asked.

Two halves of one idea, which is that a transcript is raw material and nobody
wants to read one.

**Writing it down.** When a session seals, the JSONL is reduced to a speaker-and-
text script, handed to a model with a named prompt, filed beside the transcript
it came from, and posted in a text channel. That is `handle_finished`, and it is
the only tool that uses that moment: everything else here works on the utterance
stream while a conversation is still going.

**Reading it back.** Somebody says "Miss Quote, what happened last session" and
the bot tells them, out loud, having run the stored summary through a second
prompt that turns a thing you read into a thing you say. That is
`handle_utterance`, and the whole difficulty in it is the several seconds of
inference between the question and the answer. The bot fills them with a phrase
rendered at startup — and, crucially, **starts the inference before it starts
saying it**, so the announcement covers the wait rather than being followed by
one. See `_recall`.

Two things sit under that sentence and are not in it. One evening is not always
one session, so what is retold is the whole run of them rather than the newest;
`summary.store` is where that is put back together. And "last session" is not
always what is meant — sessions get skipped, and other things happen in the
channel in between — so a trigger is the start of a question rather than the
whole of one, and `summary.when` reads which evening out of what follows it.

**Everything is per voice channel, under `monitored_channels`.** A server's rooms
are not interchangeable: one is where a game night happens and one is where two
people are debugging something, and a bot that summarizes every room it was ever
dragged into is writing files nobody asked for and posting them where everybody
can read them. The mapping doubles as the switch — a channel that is not in it is
not summarized, is not posted, and does not answer the question either.

Keys are matched through the same `slugify` that names the transcript directory,
so what is written in the config file is exactly the directory the summaries land
in, and a channel called "General Voice" is `general-voice` in both places.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from miss_quote.config import transcript_cfg
from miss_quote.llm import client as llm
from miss_quote.summary import dialogue, prompts, when as clauses
from miss_quote.config import MONITORED_CHANNELS_KEY, SCHEDULE_KEY
from miss_quote.summary.store import Chain, SummaryStore
from miss_quote.summary.when import When
from miss_quote.tools.base import Finder, Tool, ToolContext
from miss_quote.tools.tts import Tts
from miss_quote.transcript.writer import Source, Transcript, TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger
from miss_quote.utils.phrases import normalized, pattern
from miss_quote.utils.slugs import slugify

logger = get_logger(__name__)

PROMPTS_KEY = "prompts"

CHANNEL_KEY = "channel"
PROMPT_KEY = "prompt"
RETELLING_PROMPT_KEY = "retelling_prompt"
RETELLING_WORDS_KEY = "retelling_words"
MINIMUM_UTTERANCES_KEY = "minimum_utterances"
BACKOFF_SECONDS_KEY = "backoff_seconds"
SESSION_GAP_MINUTES_KEY = "session_gap_minutes"
PREAMBLE_KEY = "preamble"
EMPTY_KEY = "empty"
MISSING_KEY = "missing"
CLOSING_KEY = "closing"
NAME_KEY = "name"
TRIGGERS_KEY = "triggers"

# Everything a channel block may say. Anything else in one is a setting nothing
# reads, on the same reasoning as a stray key in a tool block: a channel quietly
# summarizing on its defaults against a file that plainly asks for something else
# is the misconfiguration with no symptom.
CHANNEL_KEYS = (
    CHANNEL_KEY,
    # Read by the transcript writer rather than by this tool, but written here:
    # a room is listed once, and being listed is what puts it on the record at
    # all. See `config.schedule_for`.
    SCHEDULE_KEY,
    PROMPT_KEY,
    RETELLING_PROMPT_KEY,
    RETELLING_WORDS_KEY,
    MINIMUM_UTTERANCES_KEY,
    BACKOFF_SECONDS_KEY,
    SESSION_GAP_MINUTES_KEY,
    PREAMBLE_KEY,
    EMPTY_KEY,
    MISSING_KEY,
    CLOSING_KEY,
    NAME_KEY,
    TRIGGERS_KEY,
)

# How long a retelling has to sound like before it is worth the tokens, and how
# short a session has to be before it is not worth summarizing. A channel
# somebody joined, said "hello" in, and left is not a conversation.
DEFAULT_RETELLING_WORDS = 200
DEFAULT_MINIMUM_UTTERANCES = 5

# How soon after telling one the bot will tell it again. Long enough that a
# channel amusing itself does not spend a minute of narration per ask, short
# enough that somebody who arrived late can still ask.
DEFAULT_BACKOFF_SECONDS = 120.0

# A window of this or less asks the model every time, which is a deployment's
# own business to want.
NEVER = 0.0

# How long a channel can sit quiet before the rest of the night counts as a
# different one. A transcript is one connection, and `resume_window_seconds` is
# a handful of seconds — enough for a client dropping, not for a pod restart or
# for a room that empties while everybody refills a glass. Past that the evening
# is filed in pieces, and this is what puts it back together to be retold.
#
# Ten minutes, because that is a break rather than an ending. It is not the
# resume window and should not be set to match it: the resume window holds a
# session open and delays everything behind it, while this is read long after,
# from names and files that are already on disk.
DEFAULT_SESSION_GAP_MINUTES = 10.0

# What plays while the model is still thinking, and what plays when there is
# nothing to think about. Both are rendered at startup, along with the line
# below, so each is a file read rather than a synthesizer round trip at the
# moment somebody is waiting.
DEFAULT_PREAMBLE = "Sure! Let me go look at my notes."
DEFAULT_EMPTY = "I don't have any notes from this channel yet."

# And what plays when there are notes, just not from the evening somebody named.
# Separate from `empty` because they are different answers: one says the bot has
# never written anything down here, and the other says it was not listening that
# night. A channel told the first when the second is true goes looking for a
# misconfiguration that is not there.
DEFAULT_MISSING = "I don't have any notes from then."

# What is said once the story is told, for a channel that asks for one. A
# retelling runs to a minute or more and ends wherever the model decided to end
# it, so a channel that has been listening needs something that tells "finished"
# from "stopped" — but the retelling prompt already ends the story itself, and a
# fixed sentence after one that has just said goodbye is one goodbye too many.
# Unset, so a channel that wants the second one writes it.
NO_CLOSING = ""

# What the bot answers to. Several spellings because none of them is what an ASR
# will necessarily have written down: a name it has never been told is guessed
# at phonetically, and "Miss Quote" comes back as one word about as often as two.
#
# "missquote" is here because it is what actually came back the first time
# somebody asked out loud — the transcriber heard the two words, ran them
# together, and kept both esses. The list is the cheapest place to be generous:
# a spelling nobody ever says costs one branch of an alternation, and a spelling
# that is missing costs somebody asking a bot twice while it ignores them.
DEFAULT_NAME = (
    "miss quote",
    "misquote",
    "missquote",
    "mis quote",
    "ms quote",
    "mizquote",
)

# How asking starts. Matched after the name and in the same breath, so an
# unaddressed "what happened last session" in the middle of a conversation is
# somebody talking to the room rather than to the bot.
#
# Stems rather than whole phrases, because which evening is being asked about is
# a clause on the end rather than a different question: "what happened last
# session" is this list's first entry plus a clause `summary.when` reads, and so
# is "what happened on the twelfth". Writing the whole phrase out would mean one
# entry per date anybody might name.
#
# A stem on its own is still a question — somebody who says "Miss Quote, what
# happened" means the last one — but only when nothing follows it. See
# `Monitored.request` for why that restriction is what keeps the list honest.
DEFAULT_TRIGGERS = (
    "what happened",
    "what did we do",
    "recap",
    "read me your notes",
    "tell me about",
)

# What the post says above the summary, so a channel scrolling back knows which
# evening it is looking at.
HEADER = "**{channel}** — {when}"
HEADER_TIMESTAMP_FORMAT = "%a %d %b %Y, %H:%M %Z"
HEADER_SEPARATOR = "\n\n"

LIST_SEPARATOR = ", "

# What a window is measured in, for the complaint when one will not parse.
SECONDS = "seconds"
MINUTES = "minutes"


def _today() -> date:
    """
    The day a question is being asked on.

    The clock the transcripts were named by, not the process's. A date somebody
    says out loud is a date in the room they are sitting in, and resolving it
    anywhere else puts "the twelfth" a day out for half of every day.
    """
    return datetime.now(ZoneInfo(transcript_cfg.timezone)).date()


@dataclass(frozen=True)
class Monitored:
    """
    One voice channel's terms: what is summarized, how, and where it goes.

    Frozen and resolved at construction, so a prompt named by a name nothing
    answers to is a tool the runner reports as having refused to start, rather
    than a discovery made at the end of the first conversation worth keeping.
    """

    name: str
    channel: str | None
    prompt: str
    retelling_prompt: str
    minimum_utterances: int
    backoff_seconds: float
    session_gap: timedelta
    preamble: str
    empty: str
    missing: str
    closing: str
    address: re.Pattern[str]
    triggers: re.Pattern[str]

    @property
    def posting(self) -> bool:
        return bool(self.channel)

    def request(self, text: str, today: date) -> When | None:
        """
        Which evening one utterance asked about, if it asked about one.

        The name has to come first and a trigger stem after it, in the same
        sentence. Addressing the bot is what separates a question from a remark,
        and the order is what stops "what happened last session, and where is
        Miss Quote" from being read as one.

        What follows the stem decides which evening, and `summary.when` will
        only accept a clause it understands or nothing at all. That second half
        is doing more work than it looks like: the stems are short now that they
        no longer carry a date, and "what happened" is a thing people say about
        the beer as often as about last Thursday. Requiring the rest of the
        sentence to be either a date or absent is what keeps a shorter list from
        being a louder one.
        """
        said = normalized(text)

        addressed = self.address.search(said)
        if addressed is None:
            return None

        stem = self.triggers.search(said, addressed.end())
        if stem is None:
            return None

        return clauses.parse(said, stem.end(), today)


class Summary(Tool):
    """Files an account of a session, and tells it back when somebody asks."""

    name = "summary"
    requires = (Tts,)

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)

        available = prompts.library(_prompts(self.config.get(PROMPTS_KEY)))
        self._monitored = _monitored(self.config.get(MONITORED_CHANNELS_KEY), available)
        self._store = SummaryStore()
        self._store.prune()

        # One retelling at a time per server. A second ask while the first is
        # still being told is dropped rather than queued: what is queued behind
        # a minute of narration is a minute of the same narration.
        self._telling = asyncio.Lock()
        self._told: dict[tuple[str, str], float] = {}

        logger.debug(
            "[%s] Summarizing %d channel(s): %s",
            self.server,
            len(self._monitored),
            LIST_SEPARATOR.join(self._monitored) or "none",
        )

    # ── startup ───────────────────────────────────

    async def prewarm(self) -> None:
        """
        Render what the bot says while it is thinking, and complain about
        anything that will not work when it is asked to.

        The preamble is the whole reason the recall does not sound broken, and a
        preamble that has to be synthesized when somebody asks for it is silence
        where the announcement was supposed to be. The empty line is warmed on
        the same terms — it is said in exactly the case where nothing else is
        going to be.

        Everything else here is a complaint. This is the first moment at which
        every tool on the server exists and the bot is connected to Discord, so
        it is the first at which a missing neighbour or an unresolvable channel
        means anything.
        """
        if not self._monitored:
            logger.warning(
                "[%s] The summary tool is enabled with no '%s', so it will never "
                "summarize anything. List the voice channels it should watch.",
                self.server,
                MONITORED_CHANNELS_KEY,
            )
            return

        self._warn_on_missing_channels()

        speech = self._tts()
        if speech is None:
            logger.warning(
                "[%s] No '%s' tool is enabled, so sessions will be summarized and "
                "posted but never read out loud. Enable it to answer aloud.",
                self.server,
                Tts.name,
            )
            return

        # A channel that asked for no closing has nothing to render for it, and
        # an empty phrase is a synthesizer round trip for silence.
        wordings = [
            wording
            for monitored in self._monitored.values()
            for wording in (
                monitored.preamble,
                monitored.empty,
                monitored.missing,
                monitored.closing,
            )
            if wording
        ]

        logger.info(
            "[%s] Queued %d phrase(s) for %d monitored channel(s) to be rendered "
            "in advance.",
            self.server,
            speech.enqueue(wordings),
            len(self._monitored),
        )

    def _warn_on_missing_channels(self) -> None:
        """
        Say now which posting channels cannot be found.

        A channel is named rather than identified, so a rename or a typo is
        invisible until a summary has nowhere to go — by which point there is a
        conversation summarized and a file written and nothing in the channel
        anybody was watching. The announcer answers this without sending
        anything, so asking costs nothing.
        """
        if not isinstance(self.announcer, Finder):
            return

        for monitored in self._monitored.values():
            if not monitored.posting:
                continue

            if self.announcer.resolve(self.server, monitored.channel) is None:
                logger.warning(
                    "[%s] No text channel called '%s' to post '%s' summaries in; "
                    "they will be written to disk and nowhere else.",
                    self.server,
                    monitored.channel,
                    monitored.name,
                )

    # ── writing it down ───────────────────────────

    async def handle_finished(self, transcript: Transcript) -> None:
        """
        Summarize one sealed session, if it was in a channel anybody asked for.

        The gate comes first and costs nothing: a channel nobody listed is not
        read, not sent anywhere, and not written about. A session too short to
        have been a conversation is dropped just after, because a summary of
        four lines is longer than the four lines.

        A failure anywhere costs the summary and nothing else. The transcript is
        untouched and can be summarized again by hand, which is why nothing here
        writes a partial result or posts one.
        """
        monitored = self._for(transcript.source)
        if monitored is None:
            return

        utterances = transcript.read()
        if len(utterances) < monitored.minimum_utterances:
            logger.info(
                "[%s] %s had %d utterance(s), under the %d it takes to be worth "
                "summarizing.",
                self.server,
                transcript.path.name,
                len(utterances),
                monitored.minimum_utterances,
            )
            return

        try:
            text = await llm.complete(monitored.prompt, dialogue.script(utterances))
        except llm.CompletionError as exc:
            logger.error(
                "[%s] Could not summarize %s: %s", self.server, transcript.path.name, exc
            )
            return

        path = self._store.write(transcript, text)

        logger.info(
            "📝 [%s] Summarized %s (%d utterances) into %s.",
            self.server,
            transcript.path.name,
            len(utterances),
            path or "nowhere",
        )

        await self._post(transcript, monitored, text)

    async def _post(
        self, transcript: Transcript, monitored: Monitored, text: str
    ) -> None:
        """Put the summary where the channel can read it, if it asked for that."""
        if not monitored.posting:
            return

        header = HEADER.format(
            channel=transcript.source.channel,
            when=transcript.opened.strftime(HEADER_TIMESTAMP_FORMAT),
        )

        await self.announcer.post(
            self.server, monitored.channel, header + HEADER_SEPARATOR + text
        )

    # ── reading it back ───────────────────────────

    async def handle_utterance(
        self, utterance: Utterance, session: TranscriptSession
    ) -> None:
        """
        Answer somebody asking what happened, if they asked here.

        Gated on the same mapping as the summarizing: a channel nobody is
        writing about cannot be asked about either, which is one rule rather
        than two and means a room left off the list is left off it entirely.

        The backoff is checked inside `_recall` rather than here, because what
        it holds off is one story rather than one channel, and which story was
        asked for is not known until the notes have been looked in.
        """
        monitored = self._for(session.source)
        if monitored is None:
            return

        when = monitored.request(utterance.text, _today())
        if when is None:
            return

        if self._telling.locked():
            logger.debug(
                "[%s] %s asked mid-retelling; letting the first one finish.",
                self.server,
                utterance.user,
            )
            return

        async with self._telling:
            await self._recall(session.source, monitored, utterance.user, when)

    async def _recall(
        self, source: Source, monitored: Monitored, asker: str, when: When
    ) -> None:
        """
        Go and look at the notes, out loud.

        The order of these four steps is the feature, and each of them is where
        it is for a reason:

        The **lookup comes first**, because it is a file read and costs nothing.
        A bot that announced it was going to look and then found nothing has
        said something it has to take back.

        The **completion is started before the preamble is played**, not after.
        `Speaker.play` returns when the clip has finished, so starting the model
        on the next line would put the several seconds of inference *after* the
        announcement meant to cover them — which is the silence this whole
        arrangement exists to remove.

        The **preamble is a cached phrase**, rendered at startup, so it begins on
        a file read rather than a synthesizer round trip.

        And the **retelling is awaited last**, by which point the model has had
        the length of the announcement to work in. If it needed longer, the wait
        is what is left of it rather than all of it.

        The **backoff is checked between the lookup and the announcement**, once
        it is known which evening was asked for. Somebody asking twice for the
        same story is what it is there to stop; somebody asking for a different
        one is asking a different question and gets an answer.
        """
        speech = self._tts()
        if speech is None:
            return

        chain = self._store.find(source, when, monitored.session_gap)
        if chain is None:
            logger.info(
                "[%s] %s asked about %s, and there are no notes from it.",
                self.server,
                asker,
                "the last session" if when.latest else when.target,
            )
            await speech.play(source, monitored.empty if when.latest else monitored.missing)
            return

        if not self._ready(monitored, chain):
            logger.debug(
                "[%s] %s asked for %s again inside the backoff; not telling it twice.",
                self.server,
                asker,
                chain.name,
            )
            return

        telling = asyncio.create_task(self._retell(chain.read(), monitored))

        try:
            await speech.play(source, monitored.preamble)
            retelling = await telling
        except llm.CompletionError as exc:
            logger.error("[%s] Could not retell %s: %s", self.server, chain.name, exc)
            return
        finally:
            # A preamble that failed would otherwise leave the completion running
            # with nobody waiting on it, and its exception uncollected.
            telling.cancel()

        logger.info(
            "📖 [%s] %s asked what happened; retelling %s (%d part(s)).",
            self.server,
            asker,
            chain.name,
            chain.parts,
        )

        self._told[(monitored.name, chain.name)] = time.monotonic()

        # Not kept: this is one evening's account, composed for this moment, and
        # nobody will ever ask for those exact words again. See `SpeechCache.stream`.
        await speech.play(source, retelling, keep=False)

        # And a fixed line after it, for a channel that asked for one. The prompt
        # is what ordinarily says the story is over; this is for a server that
        # would rather hear the same sentence every time.
        if monitored.closing:
            await speech.play(source, monitored.closing)

    async def _retell(self, evening: str, monitored: Monitored) -> str:
        """One stored evening, as something to say rather than something to read."""
        return await llm.complete(monitored.retelling_prompt, evening)

    def _ready(self, monitored: Monitored, chain: Chain) -> bool:
        """
        Whether enough has passed since this channel last heard this evening.

        Per evening rather than per channel. What the window is for is a room
        amusing itself by asking the same thing repeatedly, and what it costs
        when it is per channel is somebody who asked about last Thursday being
        ignored for two minutes because somebody else just asked about last
        week — which is a second question, and has a different answer.
        """
        if monitored.backoff_seconds <= NEVER:
            return True

        last = self._told.get((monitored.name, chain.name))

        return last is None or time.monotonic() - last >= monitored.backoff_seconds

    # ── the rest ──────────────────────────────────

    def _for(self, source: Source) -> Monitored | None:
        """
        The terms for the channel something happened in, if it is one of ours.

        Slugified on the way in, so a config file written the way the transcript
        directory is named matches whatever Discord calls the channel today.
        """
        return self._monitored.get(slugify(source.channel))

    def _tts(self) -> Tts | None:
        """
        The tool that says things out loud, if the server has one.

        Looked for on the way past rather than held: a tool's neighbours are only
        all built once every one of them is; see `Toolbox`.
        """
        return self.tools.find(Tts)

    async def close(self) -> None:
        """Let go of the connection pool the completions went through."""
        await llm.close()


def _monitored(
    raw: Any, available: Mapping[str, str]
) -> Mapping[str, Monitored]:
    """
    Every channel a server asked to have summarized, by the name its transcripts
    are filed under.

    Raised on rather than defaulted past. A block that will not parse is a server
    that meant something by it, and a tool that started anyway would summarize
    the wrong rooms with the wrong prompt — which looks exactly like working.
    """
    if raw is None:
        return {}

    if not isinstance(raw, Mapping):
        raise ValueError(
            f"'{MONITORED_CHANNELS_KEY}' must be a mapping of voice channel names "
            f"to their settings, not {raw!r}"
        )

    channels: dict[str, Monitored] = {}

    for name, settings in raw.items():
        key = slugify(str(name))
        channels[key] = _channel(key, settings or {}, available)

    return channels


def _channel(
    name: str, raw: Any, available: Mapping[str, str]
) -> Monitored:
    """One channel's terms, with everything it did not say defaulted."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"'{name}' must be a mapping of settings, not {raw!r}")

    stray = [key for key in raw if str(key) not in CHANNEL_KEYS]
    if stray:
        raise ValueError(
            f"'{name}' has {LIST_SEPARATOR.join(repr(str(key)) for key in stray)}, "
            f"which nothing reads. A channel holds "
            f"{LIST_SEPARATOR.join(repr(key) for key in CHANNEL_KEYS)}."
        )

    words = _whole(RETELLING_WORDS_KEY, raw.get(RETELLING_WORDS_KEY), DEFAULT_RETELLING_WORDS)
    channel = raw.get(CHANNEL_KEY)

    return Monitored(
        name=name,
        channel=str(channel).strip() if channel else None,
        prompt=_prompt(PROMPT_KEY, raw, available, prompts.DEFAULT_SUMMARY_PROMPT, words),
        retelling_prompt=_prompt(
            RETELLING_PROMPT_KEY, raw, available, prompts.DEFAULT_RETELLING_PROMPT, words
        ),
        minimum_utterances=_whole(
            MINIMUM_UTTERANCES_KEY,
            raw.get(MINIMUM_UTTERANCES_KEY),
            DEFAULT_MINIMUM_UTTERANCES,
        ),
        backoff_seconds=_span(
            BACKOFF_SECONDS_KEY,
            raw.get(BACKOFF_SECONDS_KEY),
            DEFAULT_BACKOFF_SECONDS,
            SECONDS,
        ),
        session_gap=timedelta(
            minutes=_span(
                SESSION_GAP_MINUTES_KEY,
                raw.get(SESSION_GAP_MINUTES_KEY),
                DEFAULT_SESSION_GAP_MINUTES,
                MINUTES,
            )
        ),
        preamble=str(raw.get(PREAMBLE_KEY) or DEFAULT_PREAMBLE),
        empty=str(raw.get(EMPTY_KEY) or DEFAULT_EMPTY),
        missing=str(raw.get(MISSING_KEY) or DEFAULT_MISSING),
        closing=str(raw.get(CLOSING_KEY) or NO_CLOSING),
        address=pattern(_spoken(NAME_KEY, raw.get(NAME_KEY), DEFAULT_NAME)),
        triggers=pattern(_spoken(TRIGGERS_KEY, raw.get(TRIGGERS_KEY), DEFAULT_TRIGGERS)),
    )


def _prompt(
    key: str,
    raw: Mapping[str, Any],
    available: Mapping[str, str],
    default: str,
    words: int,
) -> str:
    """
    One of the channel's prompts, as the model will be given it.

    Resolved here rather than when it is needed, so a name nothing answers to
    stops the tool from starting. The alternative is a tool that runs for a week
    and then fails at the one moment there is a conversation worth keeping.
    """
    named = str(raw.get(key) or default)

    try:
        return prompts.resolve(named, available, words)
    except prompts.UnknownPrompt as exc:
        raise ValueError(f"'{key}': {exc}") from exc


def _prompts(raw: Any) -> Mapping[str, str]:
    """
    The prompts a server wrote for itself, added to the shipped ones.

    Server-wide rather than per channel, because a prompt is a library entry and
    restating a paragraph of instructions once per room is how two of them end up
    saying different things by accident.
    """
    if raw is None:
        return {}

    if not isinstance(raw, Mapping):
        raise ValueError(
            f"'{PROMPTS_KEY}' must be a mapping of names to prompts, not {raw!r}"
        )

    return {str(name): str(text) for name, text in raw.items()}


def _spoken(key: str, value: Any, default: Sequence[str]) -> tuple[str, ...]:
    """
    A list of phrases the channel can say, or the shipped one.

    Replaced rather than added to, unlike the prompts: these are a matching
    vocabulary, and a server that renamed the bot means the old name to stop
    working. A single string is read as a list of one, since writing one name
    should not require remembering it is a list.
    """
    if value is None:
        return tuple(default)

    phrases = [value] if isinstance(value, str) else value

    try:
        spoken = tuple(normalized(str(phrase)) for phrase in phrases)
    except TypeError as exc:
        raise ValueError(f"'{key}' must be a phrase or a list of them: {exc}") from exc

    said = tuple(phrase for phrase in spoken if phrase)
    if not said:
        raise ValueError(f"'{key}' has nothing in it to listen for")

    return said


def _whole(key: str, value: Any, default: int) -> int:
    """A count from the channel's settings, or the default it did not set."""
    if value is None:
        return default

    try:
        return max(0, int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be a whole number, not {value!r}: {exc}") from exc


def _span(key: str, value: Any, default: float, unit: str) -> float:
    """A window from the channel's settings, or the default it did not set."""
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"'{key}' must be a number of {unit}, not {value!r}: {exc}"
        ) from exc
