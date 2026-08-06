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
one. A channel that names a clip gets music over whatever is left of the wait
after the announcement runs out. See `_recall`.

Three things sit under that sentence and are not in it. One evening is not always
one session, so what is retold is the whole run of them rather than the newest;
`summary.store` is where that is put back together. "Last session" is not always
what is meant — sessions get skipped, and other things happen in the channel in
between — so a trigger is the start of a question rather than the whole of one,
and `summary.when` reads which evening out of what follows it. And the sentence
is not always one utterance: an ASR splits wherever the speaker paused, so it
arrives in halves about as often as whole. That break lands in one of two
places, and they are not the same problem. Before the trigger, neither half asks
anything, so the name is held for a few seconds and the next thing its speaker
says finishes the question — see `Summary._asked`. After it, the first half is a
whole question already, and answering it as it lands retells the wrong evening
rather than none, so a question that named no evening waits a moment to see
whether one is coming — see `Summary._clause`.

**Showing it as it is said.** A room may also watch itself: `post_transcripts`
keeps the last ten lines in one message in the same text channel, rewritten as
the room talks rather than posted line by line. It is off unless a channel asks,
and deliberately so — a transcript on disk is a file with a retention window,
while the same words in a text channel are permanent, searchable, and readable by
people who were never in the room.

It comes down when the room does. A sealed session is everybody having left, and
a feed left up from then on is the last thing said on the way out sitting in the
channel looking current — so the message is deleted as the session seals, before
the summary that replaces it is even asked for. What the evening leaves behind is
the summary.

The writing is a service rather than part of the utterance path, which is what
keeps it inside Discord's rate limit. Editing a message is about five requests
every five seconds per channel, so `handle_utterance` only adds to a ring and one
loop per room writes what has changed and then waits out
`transcript_refresh_seconds` — measured from the end of the write, so a slow
Discord slows the feed instead of queueing behind it. Nothing is written unless
something was said. See `Summary._ticking` and `bot.ticker`.

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
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from miss_quote.audio.hold import DEFAULT_HOLD_VOLUME
from miss_quote.config import SILENT_VOLUME, UNITY_VOLUME, transcript_cfg
from miss_quote.llm import client as llm
from miss_quote.summary import dialogue, prompts, when as clauses
from miss_quote.config import MONITORED_CHANNELS_KEY, SCHEDULE_KEY
from miss_quote.summary.store import Chain, SummaryStore
from miss_quote.summary.when import When
from miss_quote.tools.base import Finder, Tool, ToolContext
from miss_quote.tools.tts import Tts
from miss_quote.transcript.writer import Source, Transcript, TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger
from miss_quote.utils.phrases import NOTHING, normalized, pattern, spoken
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
HOLD_MUSIC_KEY = "hold_music"
HOLD_VOLUME_KEY = "hold_volume"
NAME_KEY = "name"
TRIGGERS_KEY = "triggers"
ADDRESS_WINDOW_SECONDS_KEY = "address_window_seconds"
CLAUSE_WINDOW_SECONDS_KEY = "clause_window_seconds"
POST_TRANSCRIPTS_KEY = "post_transcripts"
TRANSCRIPT_LINES_KEY = "transcript_lines"
TRANSCRIPT_REFRESH_SECONDS_KEY = "transcript_refresh_seconds"

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
    HOLD_MUSIC_KEY,
    HOLD_VOLUME_KEY,
    NAME_KEY,
    TRIGGERS_KEY,
    ADDRESS_WINDOW_SECONDS_KEY,
    CLAUSE_WINDOW_SECONDS_KEY,
    POST_TRANSCRIPTS_KEY,
    TRANSCRIPT_LINES_KEY,
    TRANSCRIPT_REFRESH_SECONDS_KEY,
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

# What a window of nothing means, wherever one is read here: the backoff asks
# the model every time, and the address window holds no name at all. Both are a
# deployment's own business to want.
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

# What plays under the wait, once the preamble has finished covering the start
# of it. Unset, because the audio is not the bot's: it is a clip in the chime
# directory that somebody has to put there, and a name defaulted to a file that
# is not going to exist is a warning on every start-up for a feature nobody
# asked for. A channel that wants music names one.
NO_HOLD_MUSIC = ""

# What the bot answers to. Several spellings because none of them is what an ASR
# will necessarily have written down: a name it has never been told is guessed
# at phonetically, and "Miss Quote" comes back as one word about as often as two.
#
# "missquote" is here because it is what actually came back the first time
# somebody asked out loud — the transcriber heard the two words, ran them
# together, and kept both esses. The list is the cheapest place to be generous:
# a spelling nobody ever says costs one branch of an alternation, and a spelling
# that is missing costs somebody asking a bot twice while it ignores them.
#
# The name arriving in an utterance of its own is where a transcriber is least
# reliable, and it is now a thing the tool listens for; see `Summary._asked`. Two
# words on their own give a model nothing either side to weigh them against, so
# it falls back on whatever real words are nearest — which is where the past
# tense and the plural come from, both being words where "misquote" is not.
#
# A spelling here still only does anything with a trigger after it, so a channel
# that genuinely says "you misquoted me" is not asking for a recap by accident.
DEFAULT_NAME = (
    "miss quote",
    "misquote",
    "missquote",
    "mis quote",
    "ms quote",
    "mizquote",
    "mrs quote",
    "miss quotes",
    "misquotes",
    "missquotes",
    "misquoted",
    "missquoted",
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

# How long the bot goes on listening for the question after somebody has said
# its name and nothing else.
#
# An ASR returns utterances, not sentences, and it splits a pause wherever the
# speaker left one — so "Miss Quote, what happened on the twenty ninth" arrives
# as two lines about as often as one. Neither half is a question on its own: the
# first has no trigger and the second is not addressed to anybody. Holding the
# name for a few seconds is what puts them back together.
#
# Fifteen seconds, which is a breath and a thought rather than a conversation.
# The trigger still has to be the start of a question `summary.when` can read
# the rest of, so the window is not the only thing standing between this and a
# room that says "recap" about something else.
DEFAULT_ADDRESS_WINDOW_SECONDS = 15.0

# How long a question that named no evening waits to see whether one is still
# coming.
#
# The same split, one word further along. "Miss Quote, what happened on the
# twenty ninth" also breaks after the trigger, and that half is worse than a
# half that asks nothing: "Miss Quote, what happened" is a complete question on
# its own, so answering it the moment it lands retells the *last* session and
# the date is never heard. A wrong answer, rather than none.
#
# Only an evening nobody named waits. Anything spelled out is finished, and a
# channel that said which night it meant is answered as immediately as it always
# was; see `When.assumed`.
#
# It costs nothing to listen to, because the preamble covers it — "let me go
# look at my notes" is true whichever evening is meant, so it plays while this
# runs rather than after it. A second and a half is a pause between two halves
# of a sentence rather than a gap between two sentences.
DEFAULT_CLAUSE_WINDOW_SECONDS = 1.5

# Whether the room can watch itself being transcribed: the last few lines of
# what has been said, in the channel the summary goes to, in one message that is
# rewritten rather than a channel full of them.
#
# Off, and off deliberately. A transcript on disk is a file with a retention
# window in a volume; the same words in a text channel are permanent,
# searchable, and readable by people who were never in the room. That is a
# decision a server makes rather than one it discovers, so it is written down
# per channel like everything else here.
POST_TRANSCRIPTS = False

# How many lines are up at once. Ten is about what a client shows without
# anybody scrolling, and the thing being watched is the last few seconds rather
# than the evening — what the evening said is the transcript, and there is a
# tool for asking about it.
DEFAULT_TRANSCRIPT_LINES = 10

# How long the feed waits after each write before it writes again.
#
# Editing a message is a per-channel bucket of about five requests every five
# seconds, so two seconds is a quarter of it — enough that a fine or a summary
# posted in the same channel is not queueing behind the transcript. It is a
# floor rather than a tick: nothing is written unless something was said, and
# what the wait bounds is how often a busy room can make it write.
#
# The gap to `Topic`, which is two writes every ten minutes, is why this feed is
# a message rather than a channel status.
DEFAULT_TRANSCRIPT_REFRESH_SECONDS = 2.0

# The fastest a server may ask for. discord.py sleeps out a rate limit rather
# than raising, so a file asking for a twentieth of a second does not fail — it
# silently lags, and a feed that reads as live while running a minute behind is
# worse than one that is plainly slow. Zero is not fast; it is off, which is
# what zero means everywhere else here.
MINIMUM_TRANSCRIPT_REFRESH_SECONDS = 0.25

# How much of one utterance goes up. Long enough for a sentence and short enough
# that one person reading out a paragraph cannot push the other nine lines off
# the message.
TRANSCRIPT_LINE_LIMIT = 180

# How a line of the feed reads, and what it is wrapped in. A fence is what stops
# a transcript of somebody saying "at everyone" from pinging the server, and
# what stops an ASR returning an asterisk from italicising the rest of the feed.
TRANSCRIPT_LINE = "{user}: {text}"
TRANSCRIPT_FENCE = "```"
TRANSCRIPT_BODY = TRANSCRIPT_FENCE + "\n{lines}\n" + TRANSCRIPT_FENCE

# What a fence cannot survive inside it, how a line that ran long says so, and
# what holds a line's words apart once whatever the ASR put between them has
# been collapsed.
BACKTICK = "`"
ELLIPSIS = "…"
LINE_BREAK = "\n"
WORD_SEPARATOR = " "

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
    hold_music: str
    hold_volume: float
    address: re.Pattern[str]
    triggers: re.Pattern[str]
    address_window_seconds: float
    clause_window_seconds: float
    post_transcripts: bool
    transcript_lines: int
    transcript_refresh_seconds: float

    @property
    def posting(self) -> bool:
        return bool(self.channel)

    @property
    def ticking(self) -> bool:
        """
        Whether this room's transcript is shown as it is said.

        A channel to show it in is as much a condition as asking for it: the
        feed goes where the summary goes, and a room that named nowhere to post
        has named nowhere to show either. An interval of nothing is off for the
        same reason it is off everywhere else here, and not a feed written as
        fast as the loop can go round.
        """
        return (
            self.post_transcripts
            and self.posting
            and self.transcript_refresh_seconds > NEVER
        )

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

        return self._asking(said, addressed.end(), today)

    def addressed(self, text: str) -> bool:
        """
        Whether an utterance says the bot's name at all.

        What an utterance that names the bot and asks nothing means is "I am
        about to ask you something", which is only worth anything because the
        transcriber splits a sentence wherever the speaker paused. See
        `DEFAULT_ADDRESS_WINDOW_SECONDS`.
        """
        return self.address.search(normalized(text)) is not None

    def continues(self, text: str, today: date) -> When | None:
        """
        Which evening an utterance asked about, given the name came before it.

        The same question as `request` with the addressing already satisfied, so
        the trigger is looked for from the start of what was said rather than
        after a name that is not in this utterance. Only ever reached with a
        fresh address behind it — on its own it would make an unaddressed "what
        happened" in the middle of a conversation a question for the bot, which
        is exactly what the addressing is there to prevent.
        """
        return self._asking(normalized(text), 0, today)

    def _asking(self, said: str, start: int, today: date) -> When | None:
        """The trigger and the clause after it, from somewhere in an utterance."""
        stem = self.triggers.search(said, start)
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

        # Who has said the bot's name and not yet asked anything, by channel and
        # by speaker. Per channel because one tool serves several, and per
        # speaker because somebody else's question is a different question.
        self._addressed: dict[tuple[str, int], float] = {}

        # Who has asked a question that named no evening, and is being given a
        # moment to name one. Keyed the same way, and holding the future the ask
        # is parked on rather than a timestamp: what ends this wait is usually
        # somebody speaking rather than the clock. See `_clause`.
        self._awaiting: dict[tuple[str, int], asyncio.Future[When]] = {}

        # The last few lines each watched room has said, and what its message
        # currently reads. The second is what makes this write on change rather
        # than on a tick: a feed nobody has added to renders the same text it
        # rendered last time, and the same text is not worth an edit.
        self._lines: dict[str, deque[str]] = {}
        self._showing: dict[str, str] = {}

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

        # Says now which hold clips are not where they were said to be. Kept
        # either way — the directory is usually a mounted volume, and a file
        # that arrives later should start playing without a restart.
        for monitored in self._monitored.values():
            speech.locate(monitored.hold_music or None)

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

        The live feed comes down first, before the model is asked anything. A
        sealed session is the room having emptied, and what a feed would show
        from then on is the last thing somebody said on their way out, sitting
        in the channel looking current — for as long as a summary takes to
        write, if it were taken down at the end instead.
        """
        monitored = self._for(transcript.source)
        if monitored is None:
            return

        await self._cleared(monitored)

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

        The clause an earlier question is still waiting on is taken first, and
        before the `_telling` gate: that ask is holding the lock while it waits,
        so anything checking the gate first would drop the very thing it is
        waiting for.
        """
        monitored = self._for(session.source)
        if monitored is None:
            return

        self._noted(monitored, utterance)

        if self._completes(monitored, utterance):
            return

        when = self._asked(monitored, utterance)
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
            await self._recall(session.source, monitored, utterance, when)

    def _completes(self, monitored: Monitored, utterance: Utterance) -> bool:
        """
        Hand this utterance to a question still waiting for its evening, if one is.

        The whole utterance has to be a clause and nothing else, read from its
        first word by the same parser that reads the tail of a one-breath
        question — "on the twenty ninth" is a date said on its own, and "so
        anyway" is not. A clause that says "last session" is a clause; a trigger
        with nothing after it is not one, which is what `assumed` rules out.

        Only the speaker who asked can finish their own question. Somebody else
        saying "last week" in the meantime is talking to the room.
        """
        waiting = self._awaiting.get((monitored.name, utterance.user_id))
        if waiting is None or waiting.done():
            return False

        named = clauses.parse(normalized(utterance.text), 0, _today())
        if named is None or named.assumed:
            return False

        logger.debug(
            "[%s] %s named the evening in a second breath.", self.server, utterance.user
        )
        waiting.set_result(named)

        return True

    def _asked(self, monitored: Monitored, utterance: Utterance) -> When | None:
        """
        Which evening somebody asked about, across as many utterances as it took.

        An ASR returns utterances rather than sentences, and it splits wherever
        the speaker paused, so "Miss Quote, what happened on the twenty ninth"
        arrives as two lines about as often as one. Neither half asks anything by
        itself. The name is held for `address_window_seconds` so that the next
        thing its speaker says can be the question.

        The whole sentence is tried first, so nothing about an ordinary
        single-utterance ask goes through the memory at all.

        A held name is let go once it has produced a question, and otherwise left
        to age out — so a name, a filler, and then a question is still one ask,
        and a name followed by something that is not a question does not have to
        be said again.

        What this does not recover is the two halves arriving the other way
        round. Transcription runs several at a time (`stt.processor`), so a short
        second utterance can be returned before a long first one, and an
        `Utterance` is stamped when it is written rather than when it was said —
        there is nothing on it to sort by. The order it does recover is the one
        an ASR actually produces.
        """
        today = _today()
        held = (monitored.name, utterance.user_id)

        when = monitored.request(utterance.text, today)
        if when is not None:
            self._addressed.pop(held, None)
            return when

        if self._holding(monitored, held):
            when = monitored.continues(utterance.text, today)
            if when is not None:
                self._addressed.pop(held, None)
                return when

        if monitored.address_window_seconds > NEVER and monitored.addressed(
            utterance.text
        ):
            logger.debug(
                "[%s] %s said the name and asked nothing; listening for the rest "
                "of it for %.0f seconds.",
                self.server,
                utterance.user,
                monitored.address_window_seconds,
            )
            self._addressed[held] = time.monotonic()

        return None

    def _holding(self, monitored: Monitored, held: tuple[str, int]) -> bool:
        """
        Whether a speaker's name is still worth waiting on, forgetting it if not.

        Monotonic rather than wall clock, so a clock correction cannot park a
        name in the future and leave the bot listening until the clock arrives.
        Dropped on the way past rather than swept: nothing else reads this, and
        there are only ever as many keys as the room has had speakers.

        A window of nothing holds nothing, which is a channel asking for the
        whole question in one breath — said outright rather than left to a
        comparison against however much of a second has elapsed.
        """
        if monitored.address_window_seconds <= NEVER:
            return False

        said = self._addressed.get(held)
        if said is None:
            return False

        if time.monotonic() - said <= monitored.address_window_seconds:
            return True

        self._addressed.pop(held, None)

        return False

    async def _clause(
        self, monitored: Monitored, utterance: Utterance, when: When
    ) -> When:
        """
        The evening a question named a moment after asking, or the one assumed.

        Only an ask that named none waits. "Miss Quote, what happened on the
        twenty ninth" breaks after the trigger about as often as it breaks after
        the name, and that half is worse than a half that asks nothing: it is a
        whole question by itself, so answering it as it lands retells the wrong
        evening rather than none.

        A clause that says "last session" names what was already assumed, so it
        comes back as the assumption rather than as a second evening to go and
        look up.

        The window is short and it is free, because the preamble runs over the
        top of it; see `_recall`. What ends it is normally `_completes` rather
        than the clock — a channel that was not going to say anything else pays
        the full wait, and pays it while being told the bot is looking.
        """
        if not when.assumed or monitored.clause_window_seconds <= NEVER:
            return when

        held = (monitored.name, utterance.user_id)
        waiting: asyncio.Future[When] = asyncio.get_running_loop().create_future()
        self._awaiting[held] = waiting

        try:
            named = await asyncio.wait_for(
                waiting, monitored.clause_window_seconds
            )
        except TimeoutError:
            return when
        finally:
            self._awaiting.pop(held, None)

        return when if named.latest else named

    async def _recall(
        self, source: Source, monitored: Monitored, utterance: Utterance, when: When
    ) -> None:
        """
        Go and look at the notes, out loud.

        The order of these steps is the feature, and each of them is where it is
        for a reason:

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
        is what is left of it rather than all of it — which is what the music is
        for. `Tts.play_held` takes the completion rather than its result and
        plays the two as one clip, so a channel that named a clip hears
        something for the rest of the wait and a channel that did not hears
        exactly what it always did.

        The **backoff is checked between the lookup and the announcement**, once
        it is known which evening was asked for. Somebody asking twice for the
        same story is what it is there to stop; somebody asking for a different
        one is asking a different question and gets an answer. It is *recorded*
        at the end, once the story has actually been told: the ask that arrives
        during one is dropped by `_telling` rather than by this, and a window
        measured from the start of a minute of narration is most of the way
        through by the time anybody could use it.

        And an ask that **named no evening waits to see whether one is coming**,
        for as long as `_clause` says. The wait runs *beside* the preamble
        rather than in front of it, which is what makes it free: "let me go look
        at my notes" is true whichever night is meant, so the channel hears the
        bot answer immediately either way.

        The completion is started on the evening in hand **before** that wait
        finishes, and thrown away in the one case where the channel names a
        different one. A question that named its evening in one breath, and one
        that never names it at all, both go exactly the way they always did; the
        only ask that pays for a second lookup is the one that changed its mind
        mid-sentence, and it pays while the preamble is still playing.

        What is **not** waited for is an evening the backoff has already
        blocked. It is dropped where it always was — the alternative is holding
        the channel open on the chance that the clause names some other night,
        which is a wait nobody hears the end of, and the ask was going to be
        dropped today regardless.
        """
        speech = self._tts()
        if speech is None:
            return

        chain = self._store.find(source, when, monitored.session_gap)
        if chain is None:
            logger.info(
                "[%s] %s asked about %s, and there are no notes from it.",
                self.server,
                utterance.user,
                "the last session" if when.latest else when.target,
            )
            await speech.play(source, monitored.empty if when.latest else monitored.missing)
            return

        if not self._ready(monitored, chain):
            logger.debug(
                "[%s] %s asked for %s again inside the backoff; not telling it twice.",
                self.server,
                utterance.user,
                chain.name,
            )
            return

        telling = asyncio.create_task(self._retell(chain.read(), monitored))

        try:
            _, named = await asyncio.gather(
                speech.play(source, monitored.preamble),
                self._clause(monitored, utterance, when),
            )

            if named is not when:
                telling.cancel()

                chain = self._store.find(source, named, monitored.session_gap)
                if chain is None:
                    logger.info(
                        "[%s] %s went on to name %s, and there are no notes from it.",
                        self.server,
                        utterance.user,
                        named.target,
                    )
                    await speech.play(source, monitored.missing)
                    return

                if not self._ready(monitored, chain):
                    logger.debug(
                        "[%s] %s went on to name %s, which is inside the backoff.",
                        self.server,
                        utterance.user,
                        chain.name,
                    )
                    return

                telling = asyncio.create_task(self._retell(chain.read(), monitored))

            # Not kept: this is one evening's account, composed for this moment,
            # and nobody will ever ask for those exact words again. See
            # `SpeechCache.stream`.
            await speech.play_held(
                source,
                telling,
                hold=monitored.hold_music or None,
                hold_volume=monitored.hold_volume,
                keep=False,
            )
        except llm.CompletionError as exc:
            logger.error("[%s] Could not retell %s: %s", self.server, chain.name, exc)
            return
        finally:
            # A preamble that failed would otherwise leave the completion running
            # with nobody waiting on it, and its exception uncollected.
            telling.cancel()

        logger.info(
            "📖 [%s] %s asked what happened; retold %s (%d part(s)).",
            self.server,
            utterance.user,
            chain.name,
            chain.parts,
        )

        self._told[(monitored.name, chain.name)] = time.monotonic()

        # A fixed line after it, for a channel that asked for one. The prompt
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

    # ── showing it as it is said ──────────────────

    def _noted(self, monitored: Monitored, utterance: Utterance) -> None:
        """
        Keep one line for the feed, if this room is showing one.

        Every utterance, before anything decides what it meant: what a room
        watching itself wants is what was said, and a question put to the bot is
        as much a thing somebody said as anything else. Nothing is written here
        — the ring is what the service reads, and writing on the utterance is
        what would put an edit behind every line of speech.
        """
        if not monitored.ticking:
            return

        held = self._lines.get(monitored.name)

        # Bounded by the room's own setting, so the ring is the message: what
        # falls off the back has scrolled out of the block rather than being
        # kept for something else to find.
        if held is None or held.maxlen != monitored.transcript_lines:
            held = deque(held or (), maxlen=monitored.transcript_lines)
            self._lines[monitored.name] = held

        held.append(_said(utterance))

    async def run(self) -> None:
        """
        Keep each watched room's transcript up to date, for as long as the bot is.

        One loop per room rather than one for all of them, because the interval
        is per room and a shared loop would run every feed at whichever of them
        asked for the least. A server showing nothing returns immediately, which
        the runner treats as a service deciding it has nothing to do.
        """
        watched = [
            monitored for monitored in self._monitored.values() if monitored.ticking
        ]

        if not watched:
            return

        logger.info(
            "[%s] Showing the transcript of %d channel(s): %s",
            self.server,
            len(watched),
            LIST_SEPARATOR.join(monitored.name for monitored in watched),
        )

        await asyncio.gather(*(self._ticking(monitored) for monitored in watched))

    async def _ticking(self, monitored: Monitored) -> None:
        """
        One room's feed, written when it has changed and never faster than asked.

        The wait is measured from the end of the write rather than on a fixed
        tick, which is what keeps a slow Discord from building a queue: an edit
        that spent a second sitting out a rate limit is followed by the whole
        interval, so the cadence degrades with the API instead of racing it.

        Nothing here raises past the service. `Ticker.show` reports rather than
        throws, and a room whose message cannot be written is a warning per
        write and a loop that goes on trying — the next thing said may be the
        one that lands.
        """
        while True:
            await self._refresh(monitored)
            await asyncio.sleep(monitored.transcript_refresh_seconds)

    async def _refresh(self, monitored: Monitored) -> None:
        """
        Write this room's message, if what it would say has changed.

        The comparison is against what was last shown rather than a flag
        something else sets, so nothing can mark the feed clean while it is
        dirty: what is on the message either matches what the room has said or
        it does not.

        A room that has said nothing yet shows nothing. An empty block is not a
        transcript, and posting one would put a message up before there was
        anything to watch.
        """
        held = self._lines.get(monitored.name)
        if not held:
            return

        body = _fenced(held)
        if body == self._showing.get(monitored.name):
            return

        if await self.ticker.show(self.server, monitored.channel, body):
            self._showing[monitored.name] = body

    async def _cleared(self, monitored: Monitored) -> None:
        """
        Take one room's feed down, and forget what was on it.

        Both, and in that order, because the loop is still running: a ring left
        behind would be written straight back up by the next pass, and the same
        block posted as a second message. An empty ring shows nothing, which is
        what the room is now.

        A room that was never showing anything has nothing to take down, and
        `Ticker.clear` says so by doing nothing rather than by being asked
        first — but the state is dropped either way, since a channel that turned
        the feed off between one session and the next should not keep the last
        one's lines.
        """
        self._lines.pop(monitored.name, None)
        self._showing.pop(monitored.name, None)

        if monitored.posting:
            await self.ticker.clear(self.server, monitored.channel)

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
        hold_music=str(raw.get(HOLD_MUSIC_KEY) or NO_HOLD_MUSIC).strip(),
        hold_volume=_loudness(
            HOLD_VOLUME_KEY, raw.get(HOLD_VOLUME_KEY), DEFAULT_HOLD_VOLUME
        ),
        address=pattern(spoken(NAME_KEY, raw.get(NAME_KEY), DEFAULT_NAME)),
        triggers=pattern(spoken(TRIGGERS_KEY, raw.get(TRIGGERS_KEY), DEFAULT_TRIGGERS)),
        address_window_seconds=_span(
            ADDRESS_WINDOW_SECONDS_KEY,
            raw.get(ADDRESS_WINDOW_SECONDS_KEY),
            DEFAULT_ADDRESS_WINDOW_SECONDS,
            SECONDS,
        ),
        clause_window_seconds=_span(
            CLAUSE_WINDOW_SECONDS_KEY,
            raw.get(CLAUSE_WINDOW_SECONDS_KEY),
            DEFAULT_CLAUSE_WINDOW_SECONDS,
            SECONDS,
        ),
        post_transcripts=bool(raw.get(POST_TRANSCRIPTS_KEY, POST_TRANSCRIPTS)),
        transcript_lines=_whole(
            TRANSCRIPT_LINES_KEY,
            raw.get(TRANSCRIPT_LINES_KEY),
            DEFAULT_TRANSCRIPT_LINES,
        ),
        transcript_refresh_seconds=_paced(
            raw.get(TRANSCRIPT_REFRESH_SECONDS_KEY), DEFAULT_TRANSCRIPT_REFRESH_SECONDS
        ),
    )


def _paced(value: Any, default: float) -> float:
    """
    How often a feed may be written, held above the fastest that is sensible.

    A file asking for a twentieth of a second is not asking for a faster feed:
    discord.py sleeps out the rate limit it would earn rather than raising, so
    what it gets is a message running a minute behind a room that believes it is
    watching itself live. Held at the floor and said so, rather than refused,
    because what was meant is plain and the nearest thing to it is a working
    feed.
    """
    asked = _span(
        TRANSCRIPT_REFRESH_SECONDS_KEY, value, default, SECONDS
    )

    if asked <= NEVER or asked >= MINIMUM_TRANSCRIPT_REFRESH_SECONDS:
        return asked

    logger.warning(
        "'%s' of %.2f is faster than Discord will take; holding it at %.2f.",
        TRANSCRIPT_REFRESH_SECONDS_KEY,
        asked,
        MINIMUM_TRANSCRIPT_REFRESH_SECONDS,
    )

    return MINIMUM_TRANSCRIPT_REFRESH_SECONDS


def _said(utterance: Utterance) -> str:
    """
    One utterance as a line of the feed.

    Backticks come out rather than being escaped, because what they would break
    out of is the fence the whole block depends on and an ASR that returned one
    was transcribing a sound rather than a character. Whitespace collapses for
    the same reason one line further down: a line break inside an utterance
    would be a second line of the block, and the ring counts lines rather than
    utterances.

    Cut to a length that leaves the other nine lines on the message, since one
    person reading a paragraph out loud should not clear the room's last minute
    off the screen.
    """
    said = WORD_SEPARATOR.join(utterance.text.replace(BACKTICK, NOTHING).split())

    if len(said) > TRANSCRIPT_LINE_LIMIT:
        said = said[: TRANSCRIPT_LINE_LIMIT - len(ELLIPSIS)] + ELLIPSIS

    return TRANSCRIPT_LINE.format(user=utterance.user, text=said)


def _fenced(lines: Iterable[str]) -> str:
    """
    The feed as it goes on the message.

    Fenced, which is doing three things at once: it stops a transcript of
    somebody saying "at everyone" from pinging the server, stops an asterisk the
    ASR returned from italicising everything after it, and renders in the
    monospace a column of names reads best in.
    """
    return TRANSCRIPT_BODY.format(lines=LINE_BREAK.join(lines))


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


def _loudness(key: str, value: Any, default: float) -> float:
    """
    A fraction of the channel's own loudness, or the default it did not set.

    Clamped rather than refused. Everything either side of the range means the
    same as its nearest end — silence below, and as loud as the channel gets
    above — so there is nothing to tell somebody that they do not already know
    from what they hear.
    """
    if value is None:
        return default

    try:
        return min(UNITY_VOLUME, max(SILENT_VOLUME, float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"'{key}' must be a fraction between {SILENT_VOLUME} and "
            f"{UNITY_VOLUME}, not {value!r}: {exc}"
        ) from exc
