"""
Answers the channel with the film line it just walked into.

Listens for a trigger phrase and, on hearing one, says the associated quote out
loud where it was said. The pairs come from a YAML file — a film, and under it
the phrases that set its lines off — so adding a quote is a key rather than a
deployment.

That file is the deployment's list, and a server may write its own on top of it
under `additional_quotes`, in the same shape and read by the same rules. A
trigger written there is what that server hears whatever the shipped file says
the phrase answers with: the shared list is what a deployment agrees on rather
than what it is held to. Titles are not what collides — the list is keyed on the
trigger and carries the title on each quote — so a title written in both places
is one title with everything either of them said under it. See `_added` and
`_merged`.

A server whose own list is long enough to be worth its own file writes, in place
of the quotes, one string saying where to go and get them — a path on disk, or a
URL, downloaded once on the way up. What comes back is the same list in the same
shape, held to the same rules and merged the same way; all that has changed is
that the config file names it rather than holding it. See `_elsewhere`.

A trigger appears once within one of those. Nesting under the title makes it a
key, so a repeat under one title is not something the format can express at all;
a repeat across two titles is refused for the same reason rather than being
allowed to mean something a repeat under one title could not. A phrase worth
answering several ways says so with a list of lines, and one of them is drawn
each time it fires — written out rather than inferred from a repeated key, which
would have relied on the parser keeping something the format does not promise to
keep. See `_load`.

A trigger that has just fired goes quiet for a while — five minutes by default.
The joke is the recognition, and a channel that says "cool" four times in a
minute does not want "Shiny." four times back. The backoff is per trigger rather
than per speaker: what wears out is the line, not the person who set it off, and
a trigger with several answers spends all of them at once for the same reason.
See `RecentQuotes`.

A line that has just been said is also a question. For a few seconds afterwards
the channel can name the title it came from — "what is Firefly" — and whoever
does is paid a credit through the server's `scoreboard`, which is the same board
`verbal-morality` takes them off. The first correct answer takes the round, and a
second inside the tie window is paid as well: two people arriving at the same
title half a second apart both knew it. See `Round`.

Whoever set the line off is barred from their own round. They have the trigger
and the title in front of them and had to recall neither, so a round they could
win is one anybody can farm by reading the quote file out loud. An attempt costs
them credits and is said so out loud, because a rule nobody is told about is one
everybody keeps testing.

Every one of those is announced, and unlike a fine none of them opens with a
chime: a flourish is for an interruption, and these answer a question the channel
was already being asked. Somebody paid on a tie gets the second wording — "you
are also awarded".

Nothing said here is dropped for landing while something else is playing, which
is the other difference from a fine. A fine interrupts a conversation that was
about something else, so a backlog of them is a channel being read things it has
moved on from; everything this tool says is an answer to something it just said
itself, and a round that pays somebody without saying so reads as having missed
them. Announcements wait their turn on the speaker and come out in the order they
were earned.

The saying is the server's `tts` tool, which owns the words and the voice
connection; this one decides what they are. A server with no `tts` still runs its
rounds and still pays them, silently, and is told so at startup.

Because both the triggers and the lines are a closed set known before anybody
speaks, the whole list can be rendered at startup rather than while the channel
waits for it, and so can both wordings for everybody on the roster. See `prewarm`.
"""

from __future__ import annotations

import random
import re
import time
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import yaml

from miss_quote.config import quotes_cfg, scoreboard_cfg
from miss_quote.tools.base import Tool, ToolContext
from miss_quote.tools.scoreboard import Scoreboard
from miss_quote.tools.tts import Tts
from miss_quote.transcript.writer import TranscriptSession, Utterance
from miss_quote.utils.logging import get_logger
from miss_quote.utils.phrases import NOTHING, WORD_BOUNDARY, normalized, pattern
from miss_quote.utils.stems import plural

logger = get_logger(__name__)

T = TypeVar("T")

MOVIE_LABEL = "movie"
TRIGGER_LABEL = "trigger"
QUOTE_LABEL = "quote"

FILE_ENCODING = "utf-8"

# What holds a title and a trigger apart in a line of the log, so a dropped
# entry says where it is in the file's own words as well as by line number.
KEY_SEPARATOR = " → "

# The one tag a key or a value may resolve to. YAML reads an unquoted `no` as a
# boolean and an unquoted `1917` as an integer, and neither is text the matcher
# can compare against or the synthesizer can say.
STRING_TAG = "tag:yaml.org,2002:str"

# A node's mark counts lines from zero and an editor counts them from one.
# Counting the way an editor does is the point: a reported line number nobody
# can go and look at is not worth reporting.
EDITOR_OFFSET = 1

# How a dropped entry says where it was written. A file has a line to point at;
# a server's own additions have the server and the key they sit under, the
# config file having been parsed by something that kept no line numbers. A file
# a server pointed at has both, since one deployment may point several servers
# at several files.
FILE_LOCATION = "{path} line {line}"
SERVER_LOCATION = "[{server}] {key}"
SOURCED_LOCATION = "[{server}] {path}"

# A line that names whoever set it off. The only field a quote can interpolate:
# the roster is the one thing knowable about a speaker, and a quote that could
# reach anything else would be a template rather than a line from a film.
USER_FIELD = "user"
USER_PLACEHOLDER = f"{{{USER_FIELD}}}"

# Stands in for a speaker while a quote is checked at load.
PROBE_NAME = "someone"

TRIGGER_SEPARATOR = ", "

# What a server may say about the round, and what it gets for saying nothing.
# The defaults live here rather than in the config file so that electing into
# the tool is the whole decision a server has to make.
ANSWER_SECONDS_KEY = "answer_seconds"
TIE_SECONDS_KEY = "tie_seconds"
DEFAULT_ANSWER_SECONDS = 5.0
DEFAULT_TIE_SECONDS = 1.0

# Quotes one server hears and the others do not, written where the rest of its
# tool config is and in the shape the file uses. Merged over the deployment's
# list rather than beside it: a trigger written here is what that server hears,
# whatever the shipped file says the phrase answers with.
#
# Written as the quotes themselves, or as one string saying where to read them
# from instead; see `_elsewhere`.
ADDITIONAL_QUOTES_KEY = "additional_quotes"

# What tells the two apart, where a server wrote a string. A URL is downloaded
# and everything else is a path on disk, which is the whole rule: a deployment
# is as likely to serve its quotes alongside the rest of its configuration as to
# mount them into the container, and neither is worth a second key to say which.
URL_SCHEMES = ("http", "https")

# How long a downloaded list has to arrive before the server goes on without it.
# Nothing is waiting on this — the tools are built before the bot connects — but
# a deployment whose quote server has gone quiet should start late rather than
# not at all.
DOWNLOAD_TIMEOUT_SECONDS = 10.0

# A window of this or less is off rather than instantaneous: no answer window is
# a deployment that wants the lines and not the game, and no tie window is one
# where being second is being late.
NEVER = 0.0

# What naming it is worth. One, because the round is a few seconds of recall
# rather than a wager, and it is the same credit a fine takes off.
SINGLE_CREDIT = 1
NO_CREDITS = 0

# The three things that can be said about an answer. A wording is known by the
# setting it comes from, so nothing has to keep a second set of names in step
# with the config file.
ANNOUNCEMENT_KEY = "announcement"
TIE_ANNOUNCEMENT_KEY = "tie_announcement"
SELF_ANSWER_ANNOUNCEMENT_KEY = "self_answer_announcement"

# The defaults live here rather than in the config file so a server electing
# into the tool only has to say that it wants it.
DEFAULT_ANNOUNCEMENT = "Correct! {user}, you are awarded {credits} for {remark}"

# What somebody paid on a tie is told. The whole sentence again reads as though
# the bot had lost track of what it just said, where "also" is what a person
# would say — and "at the same time" is the only part of the round worth
# remarking on, since being second is otherwise being late.
DEFAULT_TIE_ANNOUNCEMENT = (
    "{user}, you are also awarded {credits}, for getting there at the same time."
)

# What somebody naming their own line is told. It is the one answer nobody has
# to know anything to give — the trigger and the title are both in front of
# them — so it is the one worth being rude about.
DEFAULT_SELF_ANSWER_ANNOUNCEMENT = (
    "Nuh uh uh. {user}, you set it off, so you don't get to name it. "
    "You are fined {credits} for being a dick."
)

DEFAULT_ANNOUNCEMENTS = {
    ANNOUNCEMENT_KEY: DEFAULT_ANNOUNCEMENT,
    TIE_ANNOUNCEMENT_KEY: DEFAULT_TIE_ANNOUNCEMENT,
    SELF_ANSWER_ANNOUNCEMENT_KEY: DEFAULT_SELF_ANSWER_ANNOUNCEMENT,
}

# Whether naming your own line is worth taking credits off somebody for, and how
# many. On by default: the whole round is a few seconds of recall, and somebody
# answering the question they just asked has recalled nothing.
PENALIZE_SELF_ANSWERS_KEY = "penalize_self_answers"
SELF_ANSWER_PENALTY_KEY = "self_answer_penalty"
PENALIZE_SELF_ANSWERS = True

# Enough to be worth more than the credit it was an attempt to win, so gaming
# the round is a losing trade however many times it is tried.
DEFAULT_SELF_ANSWER_PENALTY = 5

# What a round is told to bar nobody from answering it.
ANYBODY = None

# How the announcement ends, chosen afresh each time. One fixed sentence is a
# joke told once and then endured, and the tool says this every time anybody
# gets one right.
#
# None of them says "film". The key is called `movie` because it was, but what
# an entry points at is a series, a game, or a book as often as not, and an
# announcement that gets that wrong is wrong out loud in front of everybody.
REMARKS_KEY = "remarks"
REMARK_FIELD = "remark"
REMARK_PLACEHOLDER = f"{{{REMARK_FIELD}}}"

DEFAULT_REMARKS = (
    "knowing exactly where that came from, which explains a great deal.",
    "quoting along at home.",
    "a display of recall that has never once been useful.",
    "having excellent taste and nothing better to do.",
    "being the sort of person who knows that.",
    "spending your formative years exactly as you did.",
)

CREDITS_FIELD = "credits"
FIELD_SEPARATOR = ", "

# What the log says instead of a balance where no scoreboard is keeping one.
UNCOUNTED = "uncounted"

# Naming the title the way the game show does. The apostrophe in "what's" is
# gone by the time this is matched, so the contraction is spelled without one.
QUESTION = r"what(?:s|\s+is)"

# An article in front of a title is optional in both directions. The file writes
# the title the way the poster does — "The Matrix", "Hitchhiker's Guide" — and a
# channel says whichever of the two sounds right out loud. Stripped from the
# title and allowed back in the answer, so "The Matrix" answers to both.
ARTICLE = r"(?:the|an?)"
LEADING_ARTICLE = re.compile(rf"^{ARTICLE}\s+")

# Words a poster writes one way and a channel says another. The abbreviation is
# what a title carries and the word is what comes out of somebody's mouth, and
# an answer should not turn on which of the two the transcriber wrote down.
VERSUS = r"(?:vs|versus)"
SAID_ALIKE = {"vs": VERSUS, "versus": VERSUS}

# What holds the words of a title apart in the pattern built from it. Whitespace
# rather than a literal space, so the pattern reads the same as the rest of them.
WORD_SEPARATOR = r"\s+"


@dataclass(frozen=True)
class Written:
    """
    One thing a source wrote down, and somewhere whoever has to fix it can go.

    `text` is what it says where what it says is text, and None where it is
    not — an unquoted `no` YAML read as a boolean, a mapping where a line
    belonged. Deciding that here is what lets everything after it be one piece
    of code for the shipped file and for a server's own additions, which are
    read by two parsers that agree about very little else.

    `raw` is what was written whether or not it was text, so that a report about
    something the parser turned into a boolean can quote it back the way it was
    typed rather than the way it was read.
    """

    where: str
    raw: Any
    text: str | None


@dataclass(frozen=True)
class Entry:
    """One trigger and every line it answers with, as some source wrote them."""

    movie: str
    trigger: Written
    answers: tuple[Written, ...]

    # Where the answers were written, which is the only place left to point at
    # for a trigger that lists none.
    where: str


@dataclass(frozen=True)
class Quote:
    """One entry in a list: what sets a line off, and the line."""

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
    many keys as the file has triggers.
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


@dataclass(frozen=True)
class Answer:
    """
    One utterance that named the title, and what it has coming.

    The wording is the setting it will be read from, settled inside the round
    rather than worked out afterwards: whether an answer was second by half a
    second, or came from whoever set the line off, is known to the round and to
    nothing else.
    """

    movie: str
    wording: str

    @property
    def penalized(self) -> bool:
        """Whether this is somebody naming their own line rather than winning it."""
        return self.wording == SELF_ANSWER_ANNOUNCEMENT_KEY


class Round:
    """
    One line put to the channel as a question, and who has answered it.

    Opened when the quote has finished playing rather than when the trigger was
    heard. The window is for the channel to answer in, and until the line has
    been said there is nothing to answer: transcription and synthesis take as
    long as they take, and a window that started at the trigger could be over
    before anybody had heard the question.

    The first correct answer takes the round. A second inside `tie` is paid as
    well, because two people arriving at the same title half a second apart both
    knew it, and which of them the transcriber happened to return first is not a
    fact about who was faster. Anything after that has been beaten to it.

    `asker` is whoever set the line off, and is barred from answering. They have
    the trigger and the title in front of them and had to recall neither, so a
    round they could win is one anybody can farm by reading the quote file out
    loud. They are not merely ignored — an attempt costs them, and is said so out
    loud — because a rule nobody is told about is one everybody keeps testing.
    `ANYBODY` leaves the round open to them, for a server that would rather not
    police it.

    Nobody is paid, or charged, twice for the same title, however many times they
    say it.
    """

    def __init__(
        self,
        movie: str,
        window: float,
        tie: float,
        asker: int | None = ANYBODY,
        opened: float | None = None,
    ) -> None:
        self._movie = movie
        self._naming = _naming(movie)
        self._window = window
        self._tie = tie
        self._asker = asker
        self._opened = time.monotonic() if opened is None else opened
        self._claimed: float | None = None
        self._settled: set[int] = set()

    @property
    def movie(self) -> str:
        """The title being asked about, for whoever has to read the log."""
        return self._movie

    def expired(self, now: float | None = None) -> bool:
        """Whether the window has passed, so nothing said now can earn anything."""
        moment = time.monotonic() if now is None else now

        # Monotonic rather than wall clock, so a clock correction cannot park a
        # round in the future and leave it open until the clock arrives.
        return moment - self._opened > self._window

    def answered_by(self, utterance: Utterance, now: float | None = None) -> Answer | None:
        """
        What an utterance has coming for naming the title in time, or None.

        The claim is recorded on the way past, so the tie window is measured
        from the answer that arrived first rather than from the moment the
        question was asked, and whoever comes in behind it is told they tied
        rather than having to work it out from a round that has moved on.

        Whoever set the line off is settled before any of that. They cannot
        claim the round and cannot start the tie window, so an attempt of theirs
        neither wins anything nor spoils it for the channel — it costs them, and
        the round goes on being open to everybody else.
        """
        if not self._naming.search(normalized(utterance.text)):
            return None

        moment = time.monotonic() if now is None else now
        if self.expired(moment):
            return None

        if utterance.user_id in self._settled:
            return None

        if utterance.user_id == self._asker:
            self._settled.add(utterance.user_id)

            return Answer(movie=self._movie, wording=SELF_ANSWER_ANNOUNCEMENT_KEY)

        tied = self._claimed is not None
        if not tied:
            self._claimed = moment
        elif moment - self._claimed > self._tie:
            return None

        self._settled.add(utterance.user_id)

        return Answer(
            movie=self._movie,
            wording=TIE_ANNOUNCEMENT_KEY if tied else ANNOUNCEMENT_KEY,
        )


class Quotes(Tool):
    """Answers a trigger phrase with the film line it belongs to."""

    name = "quotes"
    requires = (Scoreboard, Tts)

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)

        config = self.config
        self._quotes = _merged(
            self.server,
            _load(quotes_cfg.file),
            _added(self.server, config.get(ADDITIONAL_QUOTES_KEY)),
        )
        self._triggers = pattern(self._quotes)
        self._recent = RecentQuotes()
        self._window = _seconds(
            ANSWER_SECONDS_KEY, config.get(ANSWER_SECONDS_KEY), DEFAULT_ANSWER_SECONDS
        )
        self._tie = _seconds(
            TIE_SECONDS_KEY, config.get(TIE_SECONDS_KEY), DEFAULT_TIE_SECONDS
        )
        self._announcements = {
            key: _checked(key, config.get(key) or default)
            for key, default in DEFAULT_ANNOUNCEMENTS.items()
        }
        self._remarks = _remarks(config.get(REMARKS_KEY))
        self._policing = bool(
            config.get(PENALIZE_SELF_ANSWERS_KEY, PENALIZE_SELF_ANSWERS)
        )
        self._penalty = _credits(
            SELF_ANSWER_PENALTY_KEY,
            config.get(SELF_ANSWER_PENALTY_KEY),
            DEFAULT_SELF_ANSWER_PENALTY,
        )
        self._rounds: dict[str, Round] = {}

        logger.debug(
            "[%s] Listening for %d triggers across %d quotes: %s",
            self.server,
            len(self._quotes),
            _counted(self._quotes),
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

        What a round says is knowable in the same way, and on the same terms:
        every wording the server can hear, for every name on the roster, against
        every remark it can end with. What a round pays and what it costs are
        both fixed, and the endings are a list rather than something composed on
        the spot. Which one comes up is decided when somebody answers; that all
        of them are already rendered is decided here.

        Every answer a trigger can give is rendered, not the one it happens to
        draw first. Which of them a trigger comes back with is decided when
        somebody says it, so warming any less than all of them would leave the
        channel waiting on a coin toss.

        Handed over as a list rather than rendered here. What it costs to say
        something is the speaking tool's business, and it is the one that knows
        what has already been said.
        """
        speech = self._tts()

        if self._asking() and self._scoreboard() is None:
            # The first moment at which every tool on the server exists, so the
            # first at which the absence of one means anything.
            logger.warning(
                "[%s] No scoreboard is enabled, so naming a title will earn nothing. "
                "Enable the 'scoreboard' tool to pay for it, or set '%s' to 0 to stop "
                "asking.",
                self.server,
                ANSWER_SECONDS_KEY,
            )

        if speech is None:
            logger.warning(
                "[%s] No '%s' tool is enabled, so quotes will be recognised and not "
                "said. Enable it to answer out loud.",
                self.server,
                Tts.name,
            )
            return

        names = sorted(set(self.users.values()))
        wordings = [
            wording
            for answers in self._quotes.values()
            for quote in answers
            for wording in self._wordings(quote, names)
        ]

        if self._asking():
            wordings += [saying for name in names for saying in self._sayings(name)]

        logger.info(
            "[%s] Queued %d phrase(s) for %d quote(s) and %d speaker(s) to be "
            "rendered in advance.",
            self.server,
            speech.enqueue(wordings),
            _counted(self._quotes),
            len(names),
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

        An utterance that answers an open round is an answer and nothing else,
        whatever trigger it also happens to contain. Otherwise a channel naming
        a title could set off the line that asks about the next one, which is a
        loop the tool would be driving rather than following.
        """
        if await self._settled(utterance, session):
            return

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

        await self._say(session, wording)
        self._ask(quote, utterance.user_id)

    # ── the round ─────────────────────────────────

    def _asking(self) -> bool:
        """Whether a line said here is also a question worth answering."""
        return self._window > NEVER

    def _ask(self, quote: Quote, asker: int) -> None:
        """
        Give the channel its window to name the title the line came from.

        An entry that names no title asks nothing: there is no question in it, and
        the round would be one nobody could answer. Two lines said within a few
        seconds of each other are two rounds rather than one replacing the
        other — an answer names its own title, so neither question is made
        ambiguous by the other being open.

        Whoever set the line off is barred from their own round unless the
        server has said not to police it, in which case the round is told
        `ANYBODY` and they are an answerer like anybody else.
        """
        if not self._asking() or not normalized(quote.movie):
            return

        self._rounds[quote.movie] = Round(
            quote.movie,
            self._window,
            self._tie,
            asker if self._policing else ANYBODY,
        )

    async def _settled(self, utterance: Utterance, session: TranscriptSession) -> bool:
        """
        Settle up with whoever named a title, saying whether that is what this was.

        Nothing here is dropped for arriving while something else is playing,
        which is what a fine does. A fine interrupts a conversation that was
        about something else, so a backlog of them is a channel being read
        things it has moved on from; everything this tool says is an answer to
        something it just said itself, and a round that pays somebody without
        saying so reads as having missed them. The speaker holds one turn per
        server, so a second announcement waits for the first and the two come
        out in the order they were earned.
        """
        answer = self._answered(utterance)
        if answer is None:
            return False

        credits = self._stake(answer.wording)
        standing = self._settle(utterance.user_id, utterance.user, answer)

        logger.info(
            "%s [%s] %s named %s; %s %s (%s).",
            "🚫" if answer.penalized else "🏆",
            self.server,
            utterance.user,
            answer.movie,
            "docking them" if answer.penalized else "awarding them",
            _denominated(credits),
            standing,
        )

        await self._say(session, self._wording(utterance.user, answer.wording))

        return True

    def _tts(self) -> Tts | None:
        """
        The tool that says things out loud, if the server has one.

        Looked for on the way past rather than held, on the same terms as the
        scoreboard: a tool's neighbours are only all built once every one of
        them is; see `Toolbox`.
        """
        return self.tools.find(Tts)

    async def _say(self, session: TranscriptSession, wording: str) -> None:
        """
        Put one line where it was earned, if the server has anything to say it
        with.

        No chime in front of it, ever. A fine opens with one because it
        interrupts a conversation that was about something else; everything here
        answers a question the channel is already sitting in, and a flourish
        ahead of it would be announcing what everybody is waiting for.

        A server with no speaking tool has already been told so at startup, so
        this is silent rather than a line per quote.
        """
        speech = self._tts()
        if speech is None:
            return

        await speech.play(session.source, wording)

    def _wording(
        self, user: str, key: str = ANNOUNCEMENT_KEY, remark: str | None = None
    ) -> str:
        """
        One announcement as it will be said, for one person.

        The remark is drawn afresh unless one is named, which is what the
        pre-warm does to walk every ending rather than gambling on which one
        comes up. The two render through here for exactly that reason: they must
        agree down to the character, and a phrase that differs by a space is one
        that was synthesized at startup and then synthesized again on the way to
        being played.
        """
        return self._announcements[key].format(
            **{
                USER_FIELD: user,
                CREDITS_FIELD: _denominated(self._stake(key)),
                REMARK_FIELD: _chosen(self._remarks) if remark is None else remark,
            }
        )

    def _stake(self, key: str) -> int:
        """What one wording is denominated in: what a round pays, or what it costs."""
        return (
            self._penalty if key == SELF_ANSWER_ANNOUNCEMENT_KEY else SINGLE_CREDIT
        )

    def _sayings(self, name: str) -> tuple[str, ...]:
        """
        Every way an announcement can come out for one person.

        Each wording the server can hear, and for whichever of them ends in a
        remark, one phrase per ending it can take. A template carrying no remark
        is one phrase however many the server has written.
        """
        return tuple(
            self._wording(name, key, remark)
            for key in self._sayable()
            for remark in self._endings(key)
        )

    def _sayable(self) -> tuple[str, ...]:
        """
        Which wordings this server can actually hear.

        A server that is not policing its rounds never says the third, and
        rendering it at startup would be paying a synthesizer for a phrase
        nothing can reach.
        """
        if self._policing:
            return tuple(DEFAULT_ANNOUNCEMENTS)

        return tuple(
            key for key in DEFAULT_ANNOUNCEMENTS if key != SELF_ANSWER_ANNOUNCEMENT_KEY
        )

    def _endings(self, key: str) -> tuple[str, ...]:
        """The remarks one wording can take, or a single blank where it takes none."""
        if REMARK_PLACEHOLDER in self._announcements[key]:
            return self._remarks

        return (NOTHING,)

    def _answered(self, utterance: Utterance) -> Answer | None:
        """
        What an utterance has coming from whichever round it answered, or None.

        Rounds that have run out are dropped on the way past rather than swept:
        nothing else reads this, and there are only ever as many of them as the
        channel has been quoted at in the last few seconds.
        """
        for movie, round_ in list(self._rounds.items()):
            if round_.expired():
                del self._rounds[movie]
                continue

            answer = round_.answered_by(utterance)
            if answer is not None:
                return answer

        return None

    def _scoreboard(self) -> Scoreboard | None:
        """
        The server's board, if it keeps one.

        Looked for on the way past rather than held, because a tool's neighbours
        are only all built once every one of them is; see `Toolbox`.
        """
        return self.tools.find(Scoreboard)

    def _settle(self, user_id: int, user: str, answer: Answer) -> str:
        """
        Move the balance of whoever named the title, as the log would put it.

        A server with no scoreboard asks the question, says the same things, and
        moves nothing, which is a whole working configuration rather than a
        failure: saying the line is this tool's job, and keeping score is
        somebody else's.
        """
        board = self._scoreboard()
        if board is None:
            return UNCOUNTED

        if answer.penalized:
            return f"balance {board.debit(user_id, user, self._penalty)}"

        return f"balance {board.credit(user_id, user, SINGLE_CREDIT)}"

    def _match(self, text: str) -> Quote | None:
        """
        The quote to answer an utterance with, or None.

        Matches are walked in the order they were said rather than the order the
        file lists them, so the line that answers is the one whoever spoke
        arrived at first.

        Where a trigger has more than one answer, which of them comes back is
        drawn here rather than at load: the point of listing several is that the
        channel does not get the same one twice, and a choice made once at
        startup would be the same one until the next restart.
        """
        for match in self._triggers.finditer(text):
            trigger = match.group().casefold()
            answers = self._quotes.get(trigger)

            if not answers:
                continue

            if self._recent.ready(trigger):
                return _chosen(answers)

            logger.debug(
                "[%s] '%s' has been quoted inside the last %.0f seconds; letting it lie.",
                self.server,
                trigger,
                self._recent.window,
            )

        return None


def _seconds(key: str, value: Any, default: float) -> float:
    """
    A window from the server's settings, or the default it did not set.

    Raised on rather than defaulted past: a server that wrote a window down
    meant something by it, and quietly ignoring a typo would leave a channel
    wondering why naming a title pays nothing.
    """
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"'{key}' must be a number of seconds, not {value!r}: {exc}"
        ) from exc


def _credits(key: str, value: Any, default: int) -> int:
    """
    A number of credits from the server's settings, or the default it did not set.

    Floored at nothing, since a penalty below zero is a reward and a server that
    wants one of those has a flag for turning the rule off instead.
    """
    if value is None:
        return default

    try:
        return max(NO_CREDITS, int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"'{key}' must be a whole number of credits, not {value!r}: {exc}"
        ) from exc


def _denominated(credits: int) -> str:
    """
    A number of credits as it will be said out loud, won or lost.

    What a credit is called is `settings.credits.currency`, and the plural is
    grown from it rather than configured beside it, so a deployment counting in
    something other than credits cannot end up awarding people "2 credit". The
    count stays a numeral, which every synthesizer worth pointing this at reads
    as a number; the noun does not get the same treatment — "1 credits" is wrong
    in a way a listener hears.
    """
    currency = scoreboard_cfg.currency
    noun = currency if credits == SINGLE_CREDIT else plural(currency)

    return f"{credits} {noun}"


def _checked(key: str, announcement: str) -> str:
    """
    An announcement template that will interpolate.

    Checked at construction because the alternative is discovering a stray brace
    at the moment somebody wins, by which point there is a credit paid and
    nothing to say about it. The key is carried in so a server told which
    setting is wrong does not have to work out which of them it was.
    """
    announcement = str(announcement)

    try:
        announcement.format(
            **{
                USER_FIELD: PROBE_NAME,
                CREDITS_FIELD: _denominated(SINGLE_CREDIT),
                REMARK_FIELD: DEFAULT_REMARKS[0],
            }
        )
    except (IndexError, KeyError, ValueError) as exc:
        available = FIELD_SEPARATOR.join(
            f"'{{{field}}}'" for field in (USER_FIELD, CREDITS_FIELD, REMARK_FIELD)
        )
        raise ValueError(
            f"'{key}' has a placeholder nothing fills: {exc}. "
            f"Only {available} are available."
        ) from exc

    return announcement


def _remarks(extra: Any) -> tuple[str, ...]:
    """
    Everything an announcement can end with: what the tool carries, and whatever
    the server has added to it.

    Added rather than replaced. A server writing a line of its own wants that
    line as well, and a list that replaced the defaults would make saying one
    extra thing cost writing out all of them — which is how a list ends up with
    six of the seven and nobody remembering why.
    """
    if extra is None:
        return DEFAULT_REMARKS

    if isinstance(extra, str):
        extra = [extra]

    if not isinstance(extra, Sequence):
        raise ValueError(f"'{REMARKS_KEY}' must be a list of things to say.")

    added = tuple(
        str(remark).strip() for remark in extra if str(remark).strip()
    )

    return DEFAULT_REMARKS + added


def _chosen(options: Sequence[T]) -> T:
    """
    One of several, at random.

    Its own function so a test can settle what comes up without seeding the
    process-wide generator out from under whatever else is using it. Used for
    both things this tool leaves to chance: which ending an announcement takes,
    and which answer a trigger with several of them gives.
    """
    return random.choice(options)


def _naming(movie: str) -> re.Pattern[str]:
    """
    An expression matching an utterance that names one title as a question.

    Matched against normalized text, so the pattern is spared having to allow
    for punctuation an ASR transcript may or may not have supplied. A leading
    article is optional on both sides, and the answer may be anywhere in the
    sentence: somebody who has it has said so whether or not they said
    anything else in the same breath.

    Built a word at a time rather than escaped whole, because a few of them are
    written one way and said another; see `SAID_ALIKE`.
    """
    title = LEADING_ARTICLE.sub(NOTHING, normalized(movie))
    spoken = WORD_SEPARATOR.join(_spoken(word) for word in title.split())

    return re.compile(
        rf"{WORD_BOUNDARY}{QUESTION}\s+(?:{ARTICLE}\s+)?{spoken}{WORD_BOUNDARY}"
    )


def _spoken(word: str) -> str:
    """One word of a title, as any of the ways a channel might say it."""
    return SAID_ALIKE.get(word, re.escape(word))


def _load(path: Path) -> Mapping[str, tuple[Quote, ...]]:
    """
    Every quote in the file, by the trigger that sets it off.

    A trigger appears once in the whole file. It is a key under its title, so
    writing it twice under one title is not something the file can say, and
    writing it under two titles is refused here rather than allowed to mean
    something the first form could not express. The first is kept, because a
    file is read top to bottom and the line somebody has to delete should be the
    later one.

    A phrase worth answering several ways says so with a list, and one of them
    is drawn when the trigger fires. Written out rather than inferred from a
    repeated key: a list says what it means where a repeat would have relied on
    the parser keeping something the format does not promise to keep. The
    reverse also holds — two triggers may share an answer, which is how the file
    says that two phrases deserve the same reply.

    Answers keep the order the file lists them in, so a run is reproducible for
    anything that seeds the draw. The trigger is folded for matching, so a file
    may write it however it reads best, and so `Cool` and `cool` are one trigger
    rather than two.

    An entry that is unusable is reported and dropped rather than raised on: a
    typo in one of fifty lines should cost that line. A file that is missing,
    unreadable, unparseable, or holds no usable entry at all is raised on,
    because a tool listening for nothing is enabled and useless, which is worth
    a line at startup instead of silence forever.
    """
    quotes = _quotes(_read(str(path), _from_disk(path)))

    if not quotes:
        raise ValueError(f"{path} holds no usable quotes, so there is nothing to listen for.")

    logger.info(
        "Loaded %d quotes across %d triggers from %s.",
        _counted(quotes),
        len(quotes),
        path,
    )

    return quotes


def _added(server: str, raw: Any) -> Mapping[str, tuple[Quote, ...]]:
    """
    Every quote one server wrote for itself, by the trigger that sets it off.

    The same shape as the file and the same rules, read from the server's own
    tool config rather than from `QUOTES_FILE` — a title, and under it the
    phrases that set its lines off. A server that would rather keep its list
    somewhere else writes one string instead of the quotes, and it is read from
    there; see `_elsewhere`.

    Nothing here raises, which is the one place it parts company with `_load`. A
    deployment whose quote file is unusable has a tool listening for nothing and
    should be told so on the way up; a server whose additions are unusable still
    has the whole shipped list, and taking its `quotes` tool down over a block it
    did not have to write is a worse answer than dropping the block and saying
    so. A file it pointed at and cannot read is the same thing said one step
    further away.
    """
    if raw is None:
        return {}

    quotes = _quotes(
        _elsewhere(server, raw) if isinstance(raw, str) else _offered(server, raw)
    )

    if quotes:
        logger.info(
            "[%s] Added %d quotes across %d triggers from '%s': %s",
            server,
            _counted(quotes),
            len(quotes),
            raw if isinstance(raw, str) else ADDITIONAL_QUOTES_KEY,
            TRIGGER_SEPARATOR.join(quotes),
        )

    return quotes


def _merged(
    server: str,
    shared: Mapping[str, tuple[Quote, ...]],
    added: Mapping[str, tuple[Quote, ...]],
) -> Mapping[str, tuple[Quote, ...]]:
    """
    One server's list: the deployment's, with its own additions over the top.

    A trigger the shipped file already answers is answered by the server's line
    instead, for that server alone. The shared list is what a deployment agrees
    on rather than what it is held to, and a server that wants its own line for
    a phrase everybody has should not have to pick a different phrase.

    Titles are not what collides here and never were: the list is keyed on the
    trigger and carries the title on each quote, so a title written in both
    places is one title with everything either of them said under it.
    """
    overridden = [trigger for trigger in added if trigger in shared]

    if overridden:
        logger.info(
            "[%s] %d trigger(s) answer with this server's line rather than the "
            "shipped one: %s",
            server,
            len(overridden),
            TRIGGER_SEPARATOR.join(overridden),
        )

    return {**shared, **added}


def _quotes(entries: Iterator[Entry]) -> dict[str, tuple[Quote, ...]]:
    """
    Every usable entry a source offered, by the trigger that sets it off.

    A trigger appears once within one source. It is a key under its title, so
    writing it twice under one title is not something either source can say, and
    writing it under two titles is refused here rather than allowed to mean
    something the first form could not express. The first is kept, because a
    source is read top to bottom and the line somebody has to delete should be
    the later one.
    """
    quotes: dict[str, tuple[Quote, ...]] = {}
    titles: dict[str, str] = {}

    for entry in entries:
        trigger = _trigger(entry)
        if trigger is None:
            continue

        if trigger in quotes:
            logger.warning(
                "%s: %s already answers under %r; a trigger answers for one title. "
                "Skipping it.",
                entry.trigger.where,
                _where(entry.movie, str(entry.trigger.text)),
                titles[trigger],
            )
            continue

        answers = _answers(entry, trigger)
        if not answers:
            continue

        quotes[trigger] = answers
        titles[trigger] = entry.movie

    return quotes


def _counted(quotes: Mapping[str, tuple[Quote, ...]]) -> int:
    """How many lines a list holds, where `len` gives how many triggers they set off."""
    return sum(len(answers) for answers in quotes.values())


def _where(*keys: str) -> str:
    """Where in the source something is, in the source's own keys."""
    return KEY_SEPARATOR.join(keys)


def _at(source: str, node: yaml.Node) -> str:
    """
    Where in a source a node was written, as an editor counts lines.

    A node's mark counts from zero, and a reported line number nobody can go and
    look at is not worth reporting. The source is whatever names the text the
    node was composed from — the deployment's file, or the server and the file
    it pointed at.
    """
    return FILE_LOCATION.format(path=source, line=node.start_mark.line + EDITOR_OFFSET)


def _text(node: yaml.Node) -> str | None:
    """
    What a node says, if what it says is text.

    `compose` hands back the characters as written whatever the tag resolved to,
    so an unquoted `no` would arrive here as a perfectly usable `"no"`. Reading
    the tag rather than the value is what keeps this agreeing with
    `scripts/validate_quotes.py`, which refuses the same thing before a merge.
    """
    if not isinstance(node, yaml.ScalarNode) or node.tag != STRING_TAG:
        return None

    return str(node.value)


def _contents(reference: str) -> str:
    """
    The YAML one reference holds, downloaded or read off disk.

    Which of the two it is, is the scheme and nothing else; see `URL_SCHEMES`.
    Raised on either way, so that a deployment's own file and a server's stand
    or fall by the same read and only their callers differ about what to do
    about it.
    """
    if _remote(reference):
        return _fetched(reference)

    return _from_disk(Path(reference).expanduser())


def _from_disk(path: Path) -> str:
    """
    A quote list read off the filesystem, as text.

    Where the deployment's own file is read, so `QUOTES_FILE` stays a path and
    nothing else: a `Path` collapses the double slash out of a URL, and a
    variable that quietly half-downloaded one would be worse than a variable
    that cannot.
    """
    try:
        return path.read_text(encoding=FILE_ENCODING)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read the quotes at {path}: {exc}") from exc


def _remote(reference: str) -> bool:
    """Whether a reference is somewhere to download from rather than a path."""
    return urlsplit(reference).scheme in URL_SCHEMES


def _fetched(url: str) -> str:
    """
    A quote list served over HTTP, as text.

    Downloaded here rather than by anything async, and blocking on purpose. The
    tools are built before the bot connects and before there is a loop to hold
    up, and a list fetched once at startup does not need a constructor shaped
    around an event loop that is not running yet.

    It is fetched once. What a server hears is what was served at the moment it
    started, and a list that has since changed reaches the channel at the next
    restart — which is the same promise the mounted file makes.
    """
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read().decode(FILE_ENCODING)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not download the quotes at {url}: {exc}") from exc


def _read(source: str, text: str) -> Iterator[Entry]:
    """
    Every trigger the text holds, with the title it sits under.

    Composed rather than loaded, for three things a parsed mapping cannot say. A
    key written twice survives composition and is dropped by `safe_load` without
    a word, and the tool keeps the first rather than the last. Every key and
    value carries the line it was written on, which is what lets a dropped entry
    name somewhere an editor can go. And the tag survives, which is the only way
    to tell an unquoted `no` from the word.

    Text that will not parse, or that is not a mapping of titles, is raised on
    rather than reported: it is not a file with a bad entry in it, it is not
    this file. Whether that stops the tool is the caller's to decide; `_load`
    lets it through and `_elsewhere` does not.
    """
    try:
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{source} is not valid YAML: {exc}") from exc

    if not isinstance(document, yaml.MappingNode):
        raise ValueError(
            f"{source} must be a mapping of titles, each holding the triggers that "
            f"set its lines off"
        )

    for title, entries in document.value:
        movie = _text(title)

        if movie is None:
            logger.warning(
                "%s: %r is not a title in text; quote it, or YAML reads it as "
                "something else. Skipping it.",
                _at(source, title),
                title.value,
            )
            continue

        if not isinstance(entries, yaml.MappingNode):
            logger.warning(
                "%s: %r does not hold a mapping of triggers to lines; skipping it.",
                _at(source, title),
                movie,
            )
            continue

        for key, value in entries.value:
            nodes = tuple(value.value) if isinstance(value, yaml.SequenceNode) else (value,)

            yield Entry(
                movie=movie,
                trigger=Written(where=_at(source, key), raw=key.value, text=_text(key)),
                answers=tuple(
                    Written(where=_at(source, node), raw=node.value, text=_text(node))
                    for node in nodes
                ),
                where=_at(source, value),
            )


def _elsewhere(server: str, reference: str) -> Iterator[Entry]:
    """
    Every trigger a server keeps somewhere other than its config file.

    A path on disk or a URL, holding exactly what the block would have held: a
    mapping of titles, each holding the triggers that set its lines off. It is
    read by the composer the deployment's file is read by rather than by the
    config parser, so an entry dropped out of it names the line it was written
    on — which is the one thing an inline block cannot say, `config.yaml` having
    been parsed by something that kept no line numbers.

    Nothing here raises, for the reason `_added` gives. A file that has gone
    missing, a server that will not answer, and a document that is not a mapping
    of titles are each a line in the log and a server that still hears the whole
    shipped list.
    """
    where = SOURCED_LOCATION.format(server=server, path=reference)

    if not reference.strip():
        logger.warning(
            "[%s] '%s' names nowhere to read quotes from; ignoring it.",
            server,
            ADDITIONAL_QUOTES_KEY,
        )
        return

    try:
        text = _contents(reference)
    except ValueError as exc:
        # The server, because nothing about a path knows which one wrote it
        # down. What `_read` raises has already been through `where` and says
        # so itself.
        logger.warning("[%s] %s; keeping the shipped list.", server, exc)
        return

    try:
        yield from _read(where, text)
    except ValueError as exc:
        logger.warning("%s; keeping the shipped list.", exc)


def _offered(server: str, raw: Any) -> Iterator[Entry]:
    """
    Every trigger a server added for itself, with the title it sits under.

    The config file is read once, whole, by `config.FileConfig`, so what arrives
    here is what `safe_load` made of it rather than anything carrying a line
    number. That costs the line numbers a dropped entry would otherwise name and
    nothing else: `safe_load` has already turned an unquoted `no` into a boolean
    and an unquoted `1917` into an integer, so asking whether a value is a string
    refuses exactly what the file loader's tag check refuses. Somewhere to go is
    the server and the key, which is as much as a config file can offer.

    A block that is not a mapping of titles is reported and dropped rather than
    raised on, for the reason `_added` gives.
    """
    where = SERVER_LOCATION.format(server=server, key=ADDITIONAL_QUOTES_KEY)

    if not isinstance(raw, Mapping):
        logger.warning(
            "%s: is neither a mapping of titles, each holding the triggers that set "
            "its lines off, nor a path or URL to a file holding one; ignoring it.",
            where,
        )
        return

    for title, entries in raw.items():
        movie = title if isinstance(title, str) else None

        if movie is None:
            logger.warning(
                "%s: %r is not a title in text; quote it, or YAML reads it as "
                "something else. Skipping it.",
                where,
                title,
            )
            continue

        if not isinstance(entries, Mapping):
            logger.warning(
                "%s: %r does not hold a mapping of triggers to lines; skipping it.",
                where,
                movie,
            )
            continue

        for key, value in entries.items():
            values = tuple(value) if isinstance(value, list) else (value,)

            yield Entry(
                movie=movie,
                trigger=Written(
                    where=where, raw=key, text=key if isinstance(key, str) else None
                ),
                answers=tuple(
                    Written(
                        where=where, raw=line, text=line if isinstance(line, str) else None
                    )
                    for line in values
                ),
                where=where,
            )


def _trigger(entry: Entry) -> str | None:
    """
    The phrase an entry listens for, folded for matching, or None with a reason.

    A trigger the parser did not read as text is dropped along with it. An
    unquoted `no` is a boolean and an unquoted `1917` is an integer, and both
    look entirely correct in the file while being something the matcher can never
    compare against. `scripts/validate_quotes.py` catches it before a merge; this
    catches it in a file mounted over the shipped one, which never goes past CI.
    """
    trigger = entry.trigger.text

    if trigger is None:
        logger.warning(
            "%s: %s is not a %s written in text; quote it, or YAML reads it as "
            "something else. Skipping it.",
            entry.trigger.where,
            _where(entry.movie, str(entry.trigger.raw)),
            TRIGGER_LABEL,
        )
        return None

    if not trigger.strip():
        logger.warning(
            "%s: a quote needs a %s to listen for; skipping it.",
            entry.trigger.where,
            TRIGGER_LABEL,
        )
        return None

    return trigger.strip().casefold()


def _answers(entry: Entry, trigger: str) -> tuple[Quote, ...]:
    """
    Every line one trigger can answer with.

    A trigger worth answering one way writes its line; one worth answering
    several writes a list of them. Both arrive here as a sequence, so that what
    the source chose is not something the rest of the tool has to know about.
    Each answer is reported and dropped on its own, because one bad line in a
    list of four should cost that line rather than the trigger.
    """
    if not entry.answers:
        logger.warning(
            "%s: %s lists no lines to answer with; skipping it.",
            entry.where,
            _where(entry.movie, trigger),
        )
        return ()

    return tuple(
        quote
        for quote in (
            _quote(entry.movie, trigger, answer) for answer in entry.answers
        )
        if quote is not None
    )


def _quote(movie: str, trigger: str, answer: Written) -> Quote | None:
    """
    One answer as a quote, or None with a line in the log saying why not.

    An answer with nothing to say is dropped, as is a line carrying a
    placeholder nothing fills — which is checked here rather than at the moment
    somebody says the trigger, by which point the tool has one job and cannot do
    it. So is anything the parser did not read as text, for the reason `_trigger`
    gives.
    """
    text = answer.text

    if text is None:
        logger.warning(
            "%s: %s does not answer with text; quote it, or YAML reads it as "
            "something else. Skipping it.",
            answer.where,
            _where(movie, trigger),
        )
        return None

    text = text.strip()

    if not text:
        logger.warning(
            "%s: a quote needs a %s to say; skipping it.",
            answer.where,
            QUOTE_LABEL,
        )
        return None

    try:
        text.format(**{USER_FIELD: PROBE_NAME})
    except (IndexError, KeyError, ValueError) as exc:
        logger.warning(
            "%s: %r has a placeholder nothing fills (%s); "
            "only '%s' is available. Skipping it.",
            answer.where,
            text,
            exc,
            USER_PLACEHOLDER,
        )
        return None

    return Quote(movie=movie.strip(), trigger=trigger, text=text)
