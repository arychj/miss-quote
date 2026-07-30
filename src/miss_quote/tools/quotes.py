"""
Answers the channel with the film line it just walked into.

Listens for a trigger phrase and, on hearing one, says the associated quote out
loud where it was said. The pairs come from a CSV — a film, the phrase that sets
it off, and the line — so adding a quote is a row rather than a deployment.

A trigger may appear on more than one row, and one of them is drawn at random
each time it fires. That is how a phrase worth answering several ways says so —
the file lists the answers and the channel gets one of them — and it is why the
list is keyed on the trigger rather than the row. See `_load`.

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

Because both the triggers and the lines are a closed set known before anybody
speaks, the whole list can be rendered at startup rather than while the channel
waits for it, and so can both wordings for everybody on the roster. See `prewarm`.
"""

from __future__ import annotations

import csv
import random
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from miss_quote.config import quotes_cfg, scoreboard_cfg
from miss_quote.tools.base import Tool, ToolContext
from miss_quote.tools.scoreboard import Scoreboard
from miss_quote.transcript.writer import TranscriptSession, Utterance
from miss_quote.tts.cache import shared_cache
from miss_quote.utils.logging import get_logger
from miss_quote.utils.stems import plural

logger = get_logger(__name__)

T = TypeVar("T")

MOVIE_COLUMN = "movie"
TRIGGER_COLUMN = "trigger"
QUOTE_COLUMN = "quote"
COLUMNS = (MOVIE_COLUMN, TRIGGER_COLUMN, QUOTE_COLUMN)

FILE_ENCODING = "utf-8"
COLUMN_SEPARATOR = ", "

# Where `DictReader` files the fields a row has beyond its header. It is not a
# column and nothing reads it, which is what makes an unquoted comma in a line
# the one mistake that loads cleanly: the quote is cut at the comma and the rest
# of the sentence lands here.
OVERFLOW_COLUMN = None

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

# What a server may say about the round, and what it gets for saying nothing.
# The defaults live here rather than in the config file so that electing into
# the tool is the whole decision a server has to make.
ANSWER_SECONDS_KEY = "answer_seconds"
TIE_SECONDS_KEY = "tie_seconds"
DEFAULT_ANSWER_SECONDS = 5.0
DEFAULT_TIE_SECONDS = 1.0

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
# None of them says "film". The column is called `movie` because it was, but
# what a row points at is a series, a game, or a book as often as not, and an
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

# Everything that is not a letter or a digit, for the normalizing that lets a
# title be written with the punctuation it deserves. An apostrophe closes the
# gap rather than opening one, so "what's" is "whats" and a possessive title
# answers to a transcript that dropped the mark: "hitchhikers guide".
ELIDED = re.compile(r"['‘’ʼ`]+")
UNSPOKEN = re.compile(r"[^a-z0-9]+")
SPACE = " "
NOTHING = ""

# What holds the words of a title apart in the pattern built from it. Whitespace
# rather than a literal space, so the pattern reads the same as the rest of them.
WORD_SEPARATOR = r"\s+"


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
        if not self._naming.search(_normalized(utterance.text)):
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

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)

        config = self.config
        self._quotes = _load(quotes_cfg.file)
        self._triggers = _pattern(self._quotes)
        self._speech = shared_cache()
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

        Serial, and unhurried. Nothing is waiting on this, and a synthesizer
        asked for fifty phrases at once is one not answering whoever is speaking
        right now.
        """
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

        names = sorted(set(self.users.values()))
        wordings = [
            wording
            for answers in self._quotes.values()
            for quote in answers
            for wording in self._wordings(quote, names)
        ]

        if self._asking():
            wordings += [saying for name in names for saying in self._sayings(name)]

        rendered = 0
        for wording in wordings:
            if await self._speech.warm(wording):
                rendered += 1

        logger.info(
            "[%s] Pre-warmed %d phrase(s) for %d quote(s) and %d speaker(s): "
            "%d rendered, %d already cached.",
            self.server,
            len(wordings),
            _counted(self._quotes),
            len(names),
            rendered,
            len(wordings) - rendered,
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

        await self.speaker.play(session.source, self._speech.stream(wording))
        self._ask(quote, utterance.user_id)

    # ── the round ─────────────────────────────────

    def _asking(self) -> bool:
        """Whether a line said here is also a question worth answering."""
        return self._window > NEVER

    def _ask(self, quote: Quote, asker: int) -> None:
        """
        Give the channel its window to name the title the line came from.

        A row that names no title asks nothing: there is no question in it, and
        the round would be one nobody could answer. Two lines said within a few
        seconds of each other are two rounds rather than one replacing the
        other — an answer names its own title, so neither question is made
        ambiguous by the other being open.

        Whoever set the line off is barred from their own round unless the
        server has said not to police it, in which case the round is told
        `ANYBODY` and they are an answerer like anybody else.
        """
        if not self._asking() or not _normalized(quote.movie):
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

        Announced with no chime in front of it. A fine opens with one because it
        interrupts a conversation that was about something else; this answers a
        question the channel is already sitting in, and a flourish ahead of it
        would be announcing what everybody is waiting for.

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

        await self.speaker.play(
            session.source,
            self._speech.stream(self._wording(utterance.user, answer.wording)),
        )

        return True

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

    What a credit is called is `CREDIT_CURRENCY`, and the plural is grown from
    it rather than configured beside it, so a deployment counting in something
    other than credits cannot end up awarding people "2 credit". The count stays
    a numeral, which every synthesizer worth pointing this at reads as a number;
    the noun does not get the same treatment — "1 credits" is wrong in a way a
    listener hears.
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


def _normalized(text: str) -> str:
    """
    Text as it is matched: letters and digits, lowercase, single-spaced.

    Punctuation is dropped rather than escaped, which is what makes "What's
    Firefly?" and "what is firefly" the same answer, and what lets a title be
    written with the apostrophes and colons it deserves.
    """
    return UNSPOKEN.sub(SPACE, ELIDED.sub(NOTHING, text.casefold())).strip()


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
    title = LEADING_ARTICLE.sub(NOTHING, _normalized(movie))
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

    A trigger may appear on several rows, and each of them is kept: a phrase
    worth answering more than one way says so by being written down more than
    once, and which answer the channel gets is drawn when the trigger fires. The
    reverse also holds — two rows may share an answer, which is how the file says
    that two phrases deserve the same reply.

    Rows keep the order the file lists them in, so a run is reproducible for
    anything that seeds the draw. The trigger is folded for matching, so a file
    may write it however it reads best, and so `Cool` and `cool` are two answers
    to one trigger rather than two triggers.

    A row that is unusable is reported and dropped rather than raised on: a
    typo in one of fifty lines should cost that line. A file that is missing,
    unreadable, or holds no usable row at all is raised on, because a tool
    listening for nothing is enabled and useless, which is worth a line at
    startup instead of silence forever.
    """
    rows = _rows(path)
    quotes: dict[str, list[Quote]] = {}

    for number, row in enumerate(rows, start=FIRST_ROW):
        quote = _quote(path, number, row)
        if quote is None:
            continue

        quotes.setdefault(quote.trigger, []).append(quote)

    if not quotes:
        raise ValueError(f"{path} holds no usable quotes, so there is nothing to listen for.")

    answers = {trigger: tuple(found) for trigger, found in quotes.items()}

    logger.info(
        "Loaded %d quotes across %d triggers from %s.",
        _counted(answers),
        len(answers),
        path,
    )

    return answers


def _counted(quotes: Mapping[str, tuple[Quote, ...]]) -> int:
    """How many rows the file gave, where `len` gives how many triggers they set off."""
    return sum(len(answers) for answers in quotes.values())


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

    So is a row with more fields than columns, which is what an unquoted comma
    in a line looks like from here. That one is dropped rather than kept because
    what survives it is the quote cut at the comma — "Boy" for "Boy, that
    escalated quickly." — and a film line delivered with its second half missing
    is worse out loud than not being said at all. `scripts/validate_quotes.py`
    catches it before a merge; this catches it in a file mounted over the shipped
    one, which never goes past CI.
    """
    trigger = (row.get(TRIGGER_COLUMN) or "").strip()
    text = (row.get(QUOTE_COLUMN) or "").strip()
    movie = (row.get(MOVIE_COLUMN) or "").strip()

    overflow = row.get(OVERFLOW_COLUMN)
    if overflow:
        logger.warning(
            "%s line %d: %r has %d field(s) beyond %s, so %r would be cut at the "
            "comma. Quote a value that contains one. Skipping it.",
            path,
            number,
            trigger or movie,
            len(overflow),
            COLUMN_SEPARATOR.join(COLUMNS),
            text,
        )
        return None

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
