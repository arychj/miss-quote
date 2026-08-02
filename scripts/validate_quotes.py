#!/usr/bin/env python3
"""
Checks a quotes file before it can reach a deployment.

The tool reads this file at startup and *reports rather than raises*: an entry
with no trigger, no line, or a placeholder nothing fills is logged and dropped,
and the bot carries on listening for everything else. That is the right
behaviour at three in the morning and the wrong one in review, because the only
place the mistake shows up is a log line nobody reads, on a phrase nobody
notices is missing.

So the same rules are checked here, where a broken entry fails a pull request
instead, along with the ones the loader has no opinion about — a trigger too
long to be a phrase anybody says, a line too long to be a callback, a title out
of order.

PyYAML and nothing else. It imports nothing from `miss_quote`: pulling in the
tool would mean discord.py, onnxruntime and the rest of the runtime for a job
that reads a text file, and what keeps this a thirty-second answer on every pull
request rather than a build is that the gap between them is one pure wheel.

    python scripts/validate_quotes.py [path ...]

Exits non-zero having printed one line per problem, each naming the line number
as an editor counts it.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# Must match `tools/quotes.py`. Named here rather than imported because
# importing the tool would pull in the whole runtime.
# `tests/test_validate_quotes.py` asserts the two agree, so the duplication
# cannot drift unnoticed.
MOVIE_LABEL = "movie"
TRIGGER_LABEL = "trigger"
QUOTE_LABEL = "quote"

USER_FIELD = "user"
USER_PLACEHOLDER = f"{{{USER_FIELD}}}"
PROBE_NAME = "someone"

# What holds a title and a trigger apart in a report, so a problem says where it
# is in the file's own words as well as in line numbers.
KEY_SEPARATOR = " → "

# The one tag a key or a value may resolve to. YAML reads an unquoted `no` as a
# boolean and an unquoted `1917` as an integer, and neither is a string the
# matcher can ever fire — see `_scalar`.
STRING_TAG = "tag:yaml.org,2002:str"

# A tag is `tag:yaml.org,2002:<kind>`, and only the kind is worth reporting.
TAG_SEPARATOR = ":"

DEFAULT_PATH = Path("src/miss_quote/resources/quotes.yaml")

FILE_ENCODING = "utf-8"

# A node's mark counts lines from zero and an editor counts them from one. A
# reported line number nobody can go and look at is not worth reporting.
EDITOR_OFFSET = 1

# Where a problem with the file rather than with anything named in it is
# reported, there being no key to point at.
FIRST_LINE = 1

# A trigger has to be said out loud in passing, and a line has to land before
# the channel has moved on. Both are generous against what the shipped file
# holds — the longest trigger in it is nineteen characters and the longest line
# ninety-six — because the limits are here to catch a pasted paragraph, not to
# hold anybody to a style.
MAXIMUM_TRIGGER_LENGTH = 30
MAXIMUM_QUOTE_LENGTH = 150

# The title is never spoken, only matched, so it is bounded loosely and only to
# catch an entry that has been pasted into the wrong place.
MAXIMUM_MOVIE_LENGTH = 60


# What the loader folds a trigger to before matching, so two entries differing
# only in case are one trigger rather than two.
def _folded(value: str) -> str:
    return value.casefold()


# Matching is on whole words, which needs at least one word character to sit
# between the boundaries: a trigger of pure punctuation compiles and then never
# fires.
WORD_CHARACTER = re.compile(r"\w")

# An ASR transcript holds single spaces, so a trigger written with two can never
# match however carefully it was spelled.
REPEATED_WHITESPACE = re.compile(r"\s\s")

# Anything in braces, so a placeholder that is not `{user}` is named in the
# report rather than left to `str.format` to describe.
PLACEHOLDER = re.compile(r"\{[^{}]*\}")

NEWLINE = "\n"
CARRIAGE_RETURN = "\r"

NOTHING = ""

OK = 0
FAILED = 1


@dataclass(frozen=True)
class Problem:
    """One thing wrong with one line, as it will be printed."""

    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def problems(path: Path) -> list[Problem]:
    """Everything wrong with one file, in the order an editor would meet it."""
    try:
        # newline="" rather than the default, which would translate CRLF to LF
        # on the way in and hide exactly what `_whole_file` is looking for.
        with path.open(encoding=FILE_ENCODING, newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        return [Problem(path, FIRST_LINE, "no such file")]
    except OSError as exc:
        return [Problem(path, FIRST_LINE, f"could not be read: {exc}")]
    except UnicodeDecodeError as exc:
        return [Problem(path, FIRST_LINE, f"is not valid {FILE_ENCODING}: {exc}")]

    found = list(_whole_file(path, text))

    if not text.strip():
        return found + [Problem(path, FIRST_LINE, "is empty")]

    try:
        document = yaml.compose(text)
    except yaml.MarkedYAMLError as exc:
        return found + [Problem(path, _marked(exc), f"is not valid YAML: {exc.problem}")]
    except yaml.YAMLError as exc:
        return found + [Problem(path, FIRST_LINE, f"is not valid YAML: {exc}")]

    if not isinstance(document, yaml.MappingNode):
        return found + [
            Problem(path, _at(document), "must be a mapping of titles, each holding its triggers")
        ]

    if not document.value:
        return found + [Problem(path, _at(document), "holds no quotes")]

    found.extend(_movies(path, document))

    return sorted(found, key=lambda problem: problem.line)


def _at(node: yaml.Node) -> int:
    """The line a node starts on, as an editor counts it."""
    return node.start_mark.line + EDITOR_OFFSET


def _marked(exc: yaml.MarkedYAMLError) -> int:
    """Where the parser gave up, or the top of the file if it did not say."""
    mark = exc.problem_mark or exc.context_mark
    return FIRST_LINE if mark is None else mark.line + EDITOR_OFFSET


def _where(*keys: str) -> str:
    """Where in the file something is, in the file's own keys."""
    return KEY_SEPARATOR.join(keys)


def _whole_file(path: Path, text: str) -> Iterator[Problem]:
    """
    What is wrong with the bytes rather than with any one entry.

    A missing final newline and a stray carriage return both show up as a diff
    touching a line nobody edited, which is worth catching once here rather than
    arguing about in every review.
    """
    if CARRIAGE_RETURN in text:
        yield Problem(path, FIRST_LINE, "has CRLF line endings; write it with LF")

    if text and not text.endswith(NEWLINE):
        yield Problem(path, len(text.splitlines()), "has no newline at end of file")


def _scalar(path: Path, node: yaml.Node, label: str) -> Iterator[Problem]:
    """
    That something written in the file is text, and not what YAML read it as.

    This is the mistake the format makes possible and a CSV could not. An
    unquoted `no` is a boolean, an unquoted `1917` is an integer, and neither is
    a string the matcher can compare against or the synthesizer can say. Both
    look entirely correct in the file, and neither fails anywhere else.
    """
    if isinstance(node, yaml.ScalarNode) and node.tag == STRING_TAG:
        return

    read_as = node.tag.rpartition(TAG_SEPARATOR)[2]

    if isinstance(node, yaml.ScalarNode):
        yield Problem(
            path,
            _at(node),
            f"{label} {node.value!r} is not text; quote it, or YAML reads it as {read_as}",
        )
        return

    yield Problem(path, _at(node), f"{label} is a {read_as} rather than text")


def _movies(path: Path, document: yaml.MappingNode) -> Iterator[Problem]:
    """
    Every title in the file, and every trigger under it.

    Walked in the order the file wrote them, which is what lets one pass carry
    the three facts that are only knowable across entries: which triggers have
    been met, which titles have, and how far down the alphabet the file has got.

    Titles are required to be non-decreasing so the file stays grouped. Nothing
    reads them in order; it is for whoever has to review a diff and whoever has
    to resolve the conflict when two branches both add a line.
    """
    met: set[str] = set()
    seen: dict[str, str] = {}

    # Folded to compare, as written to report: a message quoting a title back in
    # a case the file does not use is one somebody searches for and cannot find.
    highest = NOTHING
    written = NOTHING

    for key, value in document.value:
        yield from _scalar(path, key, MOVIE_LABEL)

        movie = key.value if isinstance(key.value, str) else str(key.value)
        yield from _field(path, _at(key), MOVIE_LABEL, movie, MAXIMUM_MOVIE_LENGTH)

        folded = _folded(movie.strip())

        if folded in met:
            yield Problem(
                path,
                _at(key),
                f"{MOVIE_LABEL} {movie!r} is written twice; "
                f"keep one title's triggers together under one key",
            )
        met.add(folded)

        if folded < highest:
            yield Problem(
                path,
                _at(key),
                f"{MOVIE_LABEL} {movie!r} sorts before {written!r} above it; "
                f"keep the file grouped by title",
            )
        elif folded > highest:
            highest, written = folded, movie

        if not isinstance(value, yaml.MappingNode):
            yield Problem(
                path,
                _at(value),
                f"{movie!r} does not hold a mapping of triggers to lines",
            )
            continue

        if not value.value:
            yield Problem(path, _at(key), f"{movie!r} holds no triggers")
            continue

        yield from _triggers(path, movie, value, seen)


def _triggers(
    path: Path,
    movie: str,
    entries: yaml.MappingNode,
    seen: dict[str, str],
) -> Iterator[Problem]:
    """
    Every trigger under one title.

    A trigger is unique across the whole file, not merely within a title. It is
    a key, so a repeat under one title is not something the format can express
    at all — and a repeat across two titles would mean the same phrase answering
    with two different lines, which is the inconsistency the key structure
    already rules out everywhere else. `seen` carries what has been met so far,
    so both read as one rule.
    """
    for key, value in entries.value:
        yield from _scalar(path, key, TRIGGER_LABEL)

        trigger = key.value if isinstance(key.value, str) else str(key.value)
        line = _at(key)
        where = _where(movie, trigger)

        yield from _field(path, line, TRIGGER_LABEL, trigger, MAXIMUM_TRIGGER_LENGTH)

        # Only what is there. An empty trigger is one mistake, and asking what
        # else is wrong with a phrase that is not there would report it twice.
        if trigger.strip():
            yield from _trigger(path, line, where, trigger)

            folded = _folded(trigger.strip())
            first = seen.get(folded)

            if first is None:
                seen[folded] = movie
            elif first == movie:
                yield Problem(
                    path, line, f"{where}: {TRIGGER_LABEL} is written twice under this title"
                )
            else:
                yield Problem(
                    path,
                    line,
                    f"{where}: {TRIGGER_LABEL} already answers under {first!r}; "
                    f"a trigger answers with one line, so pick a different phrase",
                )

        yield from _lines(path, where, value)


def _lines(path: Path, where: str, value: yaml.Node) -> Iterator[Problem]:
    """
    What one trigger answers with, however the file wrote it.

    A trigger worth answering one way writes its line; one worth answering
    several writes a list, and which of them the channel gets is drawn when it
    fires. The same answer twice in a list is not a second way of answering: it
    is a line pasted and half-edited, and its only effect is to weight the draw
    towards something somebody meant to change.
    """
    if isinstance(value, yaml.SequenceNode):
        if not value.value:
            yield Problem(path, _at(value), f"{where}: lists no lines to answer with")
            return

        nodes = tuple(value.value)
    else:
        nodes = (value,)

    said: set[str] = set()

    for node in nodes:
        yield from _scalar(path, node, QUOTE_LABEL)

        if not isinstance(node, yaml.ScalarNode):
            continue

        quote = node.value if isinstance(node.value, str) else str(node.value)
        line = _at(node)

        yield from _field(path, line, QUOTE_LABEL, quote, MAXIMUM_QUOTE_LENGTH)

        if not quote.strip():
            continue

        yield from _quote(path, line, where, quote)

        folded = _folded(quote.strip())
        if folded in said:
            yield Problem(
                path,
                line,
                f"{where}: answers with {quote!r} more than once; "
                f"list a different line, not the same one",
            )
        said.add(folded)


def _field(path: Path, line: int, label: str, value: str, limit: int) -> Iterator[Problem]:
    """
    What every part of an entry has in common: something in it, no more than
    `limit` of it, and no whitespace around it.

    Surrounding whitespace is a problem rather than a shrug because the loader
    strips it, so a file and the thing it produces disagree about what a trigger
    is and nothing says so. A plain scalar cannot carry it, but a quoted one
    can, which is the case worth catching.
    """
    if not value.strip():
        yield Problem(path, line, f"has an empty {label}")
        return

    if value != value.strip():
        yield Problem(path, line, f"{label} has leading or trailing whitespace")

    if len(value) > limit:
        yield Problem(path, line, f"{label} is {len(value)} characters; the limit is {limit}")


def _trigger(path: Path, line: int, where: str, trigger: str) -> Iterator[Problem]:
    """
    What a trigger has to be to ever fire.

    Every one of these compiles into the match pattern happily and then matches
    nothing, which is the worst way for an entry to be wrong: the tool starts,
    the log says it is listening, and one phrase in the file is dead.
    """
    if not WORD_CHARACTER.search(trigger):
        yield Problem(path, line, f"{where}: has no letters or digits, so it can never match")

    if REPEATED_WHITESPACE.search(trigger):
        yield Problem(
            path, line, f"{where}: has repeated whitespace; a transcript holds single spaces"
        )

    for placeholder in PLACEHOLDER.findall(trigger):
        yield Problem(
            path,
            line,
            f"{where}: contains {placeholder}; "
            f"triggers are matched as written and interpolate nothing",
        )


def _quote(path: Path, line: int, where: str, quote: str) -> Iterator[Problem]:
    """
    That a line will interpolate, and that the only thing it interpolates is a name.

    Checked here as well as at load because the loader's answer is to drop the
    entry: a quote reaching production with `{users}` in it is a phrase that
    never gets said and nothing that fails.
    """
    for placeholder in PLACEHOLDER.findall(quote):
        if placeholder != USER_PLACEHOLDER:
            yield Problem(
                path,
                line,
                f"{where}: has {placeholder}, which nothing fills; "
                f"only {USER_PLACEHOLDER} is available",
            )

    try:
        quote.format(**{USER_FIELD: PROBE_NAME})
    except (IndexError, KeyError, ValueError) as exc:
        yield Problem(path, line, f"{where}: will not interpolate ({exc})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_PATH],
        help=f"quote files to check (default: {DEFAULT_PATH})",
    )
    arguments = parser.parse_args(argv)

    found: list[Problem] = []
    for path in arguments.paths or [DEFAULT_PATH]:
        against = problems(path)
        found += against

        if not against:
            print(f"{path}: ok")

    for problem in found:
        print(problem, file=sys.stderr)

    if found:
        print(
            f"\n{len(found)} problem(s) found.",
            file=sys.stderr,
        )
        return FAILED

    return OK


if __name__ == "__main__":
    raise SystemExit(main())
