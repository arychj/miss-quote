#!/usr/bin/env python3
"""
Checks a quotes CSV before it can reach a deployment.

The tool reads this file at startup and *reports rather than raises*: a row with
no trigger, no line, or a placeholder nothing fills is logged and dropped, and
the bot carries on listening for everything else. That is the right behaviour at
three in the morning and the wrong one in review, because the only place the
mistake shows up is a log line nobody reads, on a phrase nobody notices is
missing.

So the same rules are checked here, where a broken row fails a pull request
instead, along with the ones the loader has no opinion about — a field the CSV
parser silently swallowed because a comma was left unquoted, a trigger too long
to be a phrase anybody says, a line too long to be a callback.

Standard library only, and it imports nothing from `miss_quote`: this runs on a
bare checkout with no dependencies installed, which is what keeps it a
thirty-second job on every pull request rather than a build.

    python scripts/validate_quotes.py [path ...]

Exits non-zero having printed one line per problem, each naming the line number
as an editor counts it.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# Must match `tools/quotes.py`. Named here rather than imported because
# importing the tool would pull in discord.py, onnxruntime, and the rest of the
# runtime for a job that reads a text file. `tests/test_validate_quotes.py`
# asserts the two agree, so the duplication cannot drift unnoticed.
MOVIE_COLUMN = "movie"
TRIGGER_COLUMN = "trigger"
QUOTE_COLUMN = "quote"
COLUMNS = (MOVIE_COLUMN, TRIGGER_COLUMN, QUOTE_COLUMN)

USER_FIELD = "user"
USER_PLACEHOLDER = f"{{{USER_FIELD}}}"
PROBE_NAME = "someone"

DEFAULT_PATH = Path("src/miss_quote/resources/quotes.csv")

FILE_ENCODING = "utf-8"
COLUMN_SEPARATOR = ", "

# Row 1 is the header, so the first row of data is the second line of the file.
# Counting the way an editor does is the point: a reported line number nobody
# can go and look at is not worth reporting.
HEADER_ROW = 1
FIRST_ROW = 2

# A trigger has to be said out loud in passing, and a line has to land before
# the channel has moved on. Both are generous against what the shipped file
# holds — the longest trigger in it is nineteen characters and the longest line
# ninety-six — because the limits are here to catch a pasted paragraph, not to
# hold anybody to a style.
MAXIMUM_TRIGGER_LENGTH = 30
MAXIMUM_QUOTE_LENGTH = 150

# The title is never spoken, only matched, so it is bounded loosely and only to
# catch a line whose columns have shifted.
MAXIMUM_MOVIE_LENGTH = 60

# What the loader folds a trigger to before matching, so two rows differing only
# in case are two answers to one trigger rather than two triggers.
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
        return [Problem(path, HEADER_ROW, "no such file")]
    except OSError as exc:
        return [Problem(path, HEADER_ROW, f"could not be read: {exc}")]
    except UnicodeDecodeError as exc:
        return [Problem(path, HEADER_ROW, f"is not valid {FILE_ENCODING}: {exc}")]

    found = list(_whole_file(path, text))

    rows = list(csv.reader(text.splitlines(), strict=True))
    if not rows:
        return found + [Problem(path, HEADER_ROW, "is empty")]

    found += list(_header(path, rows[0]))

    data = rows[FIRST_ROW - 1 :]
    if not data:
        found.append(Problem(path, HEADER_ROW, "has a header and no quotes"))

    for offset, row in enumerate(data):
        found.extend(_row(path, FIRST_ROW + offset, row))

    found.extend(_across_rows(path, data))

    return sorted(found, key=lambda problem: problem.line)


def _whole_file(path: Path, text: str) -> Iterator[Problem]:
    """
    What is wrong with the bytes rather than with any one row.

    A missing final newline and a stray carriage return both show up as a diff
    touching a line nobody edited, which is worth catching once here rather than
    arguing about in every review.
    """
    if CARRIAGE_RETURN in text:
        yield Problem(path, HEADER_ROW, "has CRLF line endings; write it with LF")

    if text and not text.endswith(NEWLINE):
        yield Problem(path, len(text.splitlines()), "has no newline at end of file")


def _header(path: Path, header: Sequence[str]) -> Iterator[Problem]:
    if tuple(header) != COLUMNS:
        yield Problem(
            path,
            HEADER_ROW,
            f"header is {COLUMN_SEPARATOR.join(header) or '(empty)'}; "
            f"it must be exactly {','.join(COLUMNS)}",
        )


def _row(path: Path, line: int, row: Sequence[str]) -> Iterator[Problem]:
    """
    What is wrong with one row.

    The field count is checked before anything else and stops the row: every
    other check reads a column by position, and on a row whose columns have
    shifted each of them would report the same one mistake in different words.
    """
    if len(row) != len(COLUMNS):
        yield Problem(
            path,
            line,
            f"has {len(row)} field(s), not {len(COLUMNS)} — "
            f"a value containing a comma must be \"quoted\"",
        )
        return

    movie, trigger, quote = row

    yield from _field(path, line, MOVIE_COLUMN, movie, MAXIMUM_MOVIE_LENGTH)
    yield from _field(path, line, TRIGGER_COLUMN, trigger, MAXIMUM_TRIGGER_LENGTH)
    yield from _field(path, line, QUOTE_COLUMN, quote, MAXIMUM_QUOTE_LENGTH)

    # Only what is there. An empty column is one mistake, and asking what else
    # is wrong with a trigger that is not there would report it twice.
    if trigger.strip():
        yield from _trigger(path, line, trigger)

    if quote.strip():
        yield from _quote(path, line, quote)


def _field(path: Path, line: int, column: str, value: str, limit: int) -> Iterator[Problem]:
    """
    What every column has in common: something in it, no more than `limit` of
    it, and no whitespace around it.

    Surrounding whitespace is a problem rather than a shrug because the loader
    strips it, so a file and the thing it produces disagree about what a trigger
    is and nothing says so.
    """
    if not value.strip():
        yield Problem(path, line, f"has an empty {column}")
        return

    if value != value.strip():
        yield Problem(path, line, f"{column} has leading or trailing whitespace")

    if len(value) > limit:
        yield Problem(
            path,
            line,
            f"{column} is {len(value)} characters; the limit is {limit}",
        )


def _trigger(path: Path, line: int, trigger: str) -> Iterator[Problem]:
    """
    What a trigger has to be to ever fire.

    Every one of these compiles into the match pattern happily and then matches
    nothing, which is the worst way for a row to be wrong: the tool starts, the
    log says it is listening, and one phrase in the file is dead.
    """
    if not WORD_CHARACTER.search(trigger):
        yield Problem(
            path,
            line,
            f"trigger {trigger!r} has no letters or digits, so it can never match",
        )

    if REPEATED_WHITESPACE.search(trigger):
        yield Problem(
            path,
            line,
            f"trigger {trigger!r} has repeated whitespace; a transcript holds single spaces",
        )

    for placeholder in PLACEHOLDER.findall(trigger):
        yield Problem(
            path,
            line,
            f"trigger {trigger!r} contains {placeholder}; "
            f"triggers are matched as written and interpolate nothing",
        )


def _quote(path: Path, line: int, quote: str) -> Iterator[Problem]:
    """
    That a line will interpolate, and that the only thing it interpolates is a name.

    Checked here as well as at load because the loader's answer is to drop the
    row: a quote reaching production with `{users}` in it is a phrase that never
    gets said and nothing that fails.
    """
    for placeholder in PLACEHOLDER.findall(quote):
        if placeholder != USER_PLACEHOLDER:
            yield Problem(
                path,
                line,
                f"quote has {placeholder}, which nothing fills; "
                f"only {USER_PLACEHOLDER} is available",
            )

    try:
        quote.format(**{USER_FIELD: PROBE_NAME})
    except (IndexError, KeyError, ValueError) as exc:
        yield Problem(path, line, f"quote will not interpolate ({exc})")


def _across_rows(path: Path, data: Sequence[Sequence[str]]) -> Iterator[Problem]:
    """
    What is only wrong about a row given the rest of them.

    A trigger repeated is fine and deliberate — the tool draws one of the
    answers at random — but the *same* answer to the same trigger twice is not.
    It is a row pasted and half-edited, and its only effect is to weight the
    draw towards a line somebody meant to change.

    The `movie` column is required to be non-decreasing so the file stays
    grouped by title. Nothing reads it in order; it is for whoever has to review
    a diff and whoever has to resolve the conflict when two branches both add a
    row.
    """
    usable = [row for row in data if len(row) == len(COLUMNS)]

    seen = Counter(
        (_folded(trigger.strip()), _folded(quote.strip()))
        for _, trigger, quote in usable
    )

    previous = ""
    for offset, row in enumerate(data):
        if len(row) != len(COLUMNS):
            continue

        movie, trigger, quote = row
        line = FIRST_ROW + offset

        if seen[(_folded(trigger.strip()), _folded(quote.strip()))] > 1:
            yield Problem(
                path,
                line,
                f"trigger {trigger!r} answers with {quote!r} on more than one row; "
                f"repeat a trigger to add a different answer, not the same one",
            )

        folded = _folded(movie.strip())
        if folded < previous:
            yield Problem(
                path,
                line,
                f"movie {movie!r} sorts before {previous!r} on the line above; "
                f"keep the file grouped by title",
            )
        previous = max(previous, folded)


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
