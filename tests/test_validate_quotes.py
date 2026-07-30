"""What the quote-file validator catches, and that it agrees with the loader about the file it is checking."""

import pytest

import validate_quotes
from miss_quote.config import BUNDLED_QUOTES
from miss_quote.tools import quotes as tool
from validate_quotes import (
    FAILED,
    MAXIMUM_MOVIE_LENGTH,
    MAXIMUM_QUOTE_LENGTH,
    MAXIMUM_TRIGGER_LENGTH,
    OK,
    main,
    problems,
)

HEADER = "movie,trigger,quote"
MOVIE = "Firefly"
TRIGGER = "cool"
QUOTE = "Shiny."
ROW = f"{MOVIE},{TRIGGER},{QUOTE}"

OTHER_TRIGGER = "behave"
OTHER_QUOTE = "I aim to misbehave."

# The first line of data, which is the second line of the file.
FIRST_ROW = 2

NOTHING = 0
ONE_PROBLEM = 1


def _file(tmp_path, *lines: str, newline: bool = True):
    path = tmp_path / "quotes.csv"
    path.write_text("\n".join(lines) + ("\n" if newline else ""), encoding="utf-8")

    return path


def _details(path) -> list[str]:
    return [problem.detail for problem in problems(path)]


# ── the file the image ships ──────────────────────


def test_the_shipped_file_is_valid():
    """The one check that would have to fail before anything reached a channel."""
    assert problems(BUNDLED_QUOTES) == []


def test_the_validator_and_the_loader_agree_on_the_columns():
    """
    The validator names the columns rather than importing them, so that it can
    run without the runtime installed. This is what stops the two drifting.
    """
    assert validate_quotes.COLUMNS == tool.COLUMNS
    assert validate_quotes.USER_PLACEHOLDER == tool.USER_PLACEHOLDER
    assert validate_quotes.FIRST_ROW == tool.FIRST_ROW


# ── the shape of the file ─────────────────────────


def test_a_good_file_has_nothing_wrong_with_it(tmp_path):
    assert problems(_file(tmp_path, HEADER, ROW)) == []


def test_a_missing_file_is_reported(tmp_path):
    assert "no such file" in _details(tmp_path / "absent.csv")[0]


def test_a_wrong_header_is_reported(tmp_path):
    assert "header" in _details(_file(tmp_path, "film,trigger,quote", ROW))[0]


def test_a_file_of_only_a_header_is_reported(tmp_path):
    assert "no quotes" in _details(_file(tmp_path, HEADER))[0]


def test_a_missing_final_newline_is_reported(tmp_path):
    assert "newline at end of file" in _details(
        _file(tmp_path, HEADER, ROW, newline=False)
    )[0]


def test_crlf_line_endings_are_reported(tmp_path):
    path = tmp_path / "quotes.csv"
    path.write_bytes(f"{HEADER}\r\n{ROW}\r\n".encode())

    assert "CRLF" in _details(path)[0]


# ── one row ───────────────────────────────────────


def test_an_unquoted_comma_is_reported(tmp_path):
    """
    The mistake the whole check exists for.

    The loader takes the fourth field as an overflow it never reads, so the row
    loads, the tool starts, and the line is silently cut at the comma.
    """
    detail = _details(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},Boy, that escalated."))[0]

    assert "4 field(s), not 3" in detail
    assert "quoted" in detail


def test_a_properly_quoted_comma_is_fine(tmp_path):
    assert problems(_file(tmp_path, HEADER, f'{MOVIE},{TRIGGER},"Boy, that escalated."')) == []


def test_too_few_fields_is_reported(tmp_path):
    assert "2 field(s), not 3" in _details(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER}"))[0]


def test_a_broken_row_is_reported_once(tmp_path):
    """Every other check reads a column by position, so they would all say it again."""
    assert len(_details(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER}"))) == ONE_PROBLEM


@pytest.mark.parametrize(
    ("row", "column"),
    (
        (f",{TRIGGER},{QUOTE}", "movie"),
        (f"{MOVIE},,{QUOTE}", "trigger"),
        (f"{MOVIE},{TRIGGER},", "quote"),
        (f"{MOVIE},   ,{QUOTE}", "trigger"),
    ),
)
def test_an_empty_column_is_reported(tmp_path, row, column):
    assert f"empty {column}" in _details(_file(tmp_path, HEADER, row))[0]


def test_surrounding_whitespace_is_reported(tmp_path):
    """The loader strips it, so the file and what it produces disagree quietly."""
    assert "whitespace" in _details(_file(tmp_path, HEADER, f"{MOVIE}, {TRIGGER},{QUOTE}"))[0]


@pytest.mark.parametrize(
    ("column", "limit", "row"),
    (
        ("trigger", MAXIMUM_TRIGGER_LENGTH, f"{MOVIE},{'x' * 99},{QUOTE}"),
        ("quote", MAXIMUM_QUOTE_LENGTH, f"{MOVIE},{TRIGGER},{'x' * 999}"),
        ("movie", MAXIMUM_MOVIE_LENGTH, f"{'x' * 99},{TRIGGER},{QUOTE}"),
    ),
)
def test_an_overlong_column_is_reported(tmp_path, column, limit, row):
    detail = _details(_file(tmp_path, HEADER, row))[0]

    assert column in detail
    assert str(limit) in detail


def test_a_column_at_its_limit_is_fine(tmp_path):
    row = f"{MOVIE},{'x' * MAXIMUM_TRIGGER_LENGTH},{'x' * MAXIMUM_QUOTE_LENGTH}"

    assert problems(_file(tmp_path, HEADER, row)) == []


# ── a trigger that could never fire ───────────────


def test_a_trigger_of_pure_punctuation_is_reported(tmp_path):
    """It compiles into the pattern happily and then matches nothing."""
    assert "never match" in _details(_file(tmp_path, HEADER, f"{MOVIE},!!!,{QUOTE}"))[0]


def test_a_trigger_with_a_double_space_is_reported(tmp_path):
    assert "repeated whitespace" in _details(
        _file(tmp_path, HEADER, f"{MOVIE},game  over,{QUOTE}")
    )[0]


def test_a_placeholder_in_a_trigger_is_reported(tmp_path):
    """Triggers are matched as written; nothing interpolates them."""
    assert "interpolate nothing" in _details(
        _file(tmp_path, HEADER, f"{MOVIE},hey {{user}},{QUOTE}")
    )[0]


# ── what a line interpolates ──────────────────────


def test_the_user_placeholder_is_allowed(tmp_path):
    assert problems(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},Hello {{user}}.")) == []


def test_any_other_placeholder_is_reported(tmp_path):
    """The loader drops the row, so the symptom is a line that is never said."""
    detail = _details(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},Hello {{tally}}."))[0]

    assert "{tally}" in detail
    assert "{user}" in detail


def test_an_unclosed_brace_is_reported(tmp_path):
    assert any(
        "interpolate" in detail
        for detail in _details(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},Hello {{user."))
    )


# ── one row against the rest ──────────────────────


def test_a_trigger_may_be_repeated_with_a_different_answer(tmp_path):
    """Which is how the file asks for one of several, drawn when it fires."""
    path = _file(
        tmp_path, HEADER, f"{MOVIE},{TRIGGER},{QUOTE}", f"{MOVIE},{TRIGGER},{OTHER_QUOTE}"
    )

    assert problems(path) == []


def test_a_trigger_repeated_with_the_same_answer_is_reported(tmp_path):
    """A row pasted and half-edited, whose only effect is to weight the draw."""
    path = _file(
        tmp_path, HEADER, f"{MOVIE},{TRIGGER},{QUOTE}", f"{MOVIE},{TRIGGER},{QUOTE}"
    )

    assert "more than one row" in _details(path)[0]


def test_a_repeat_differing_only_in_case_is_reported(tmp_path):
    """The loader folds the trigger, so these are the same row said twice."""
    path = _file(
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{TRIGGER.upper()},{QUOTE.upper()}",
    )

    assert "more than one row" in _details(path)[0]


def test_two_triggers_may_share_an_answer(tmp_path):
    path = _file(
        tmp_path, HEADER, f"{MOVIE},{TRIGGER},{QUOTE}", f"{MOVIE},{OTHER_TRIGGER},{QUOTE}"
    )

    assert problems(path) == []


def test_a_title_out_of_order_is_reported(tmp_path):
    path = _file(
        tmp_path, HEADER, f"The Matrix,{TRIGGER},{QUOTE}", f"Aliens,{OTHER_TRIGGER},{OTHER_QUOTE}"
    )

    assert "grouped by title" in _details(path)[0]


def test_rows_for_one_title_may_be_in_any_order(tmp_path):
    """Only the titles are ordered; the triggers under one are the author's business."""
    path = _file(
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{OTHER_TRIGGER},{OTHER_QUOTE}",
        f"The Matrix,spoon,There is no spoon.",
    )

    assert problems(path) == []


# ── what it reports ───────────────────────────────


def test_problems_are_reported_in_line_order(tmp_path):
    path = _file(
        tmp_path,
        HEADER,
        f"{MOVIE},{TRIGGER},{QUOTE}",
        f"{MOVIE},{OTHER_TRIGGER},",
        f"{MOVIE},,{QUOTE}",
    )

    assert [problem.line for problem in problems(path)] == [3, 4]


def test_a_problem_names_the_file_and_the_line(tmp_path):
    path = _file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},")

    assert str(problems(path)[0]).startswith(f"{path}:{FIRST_ROW}:")


def test_a_good_file_exits_zero(tmp_path):
    assert main([str(_file(tmp_path, HEADER, ROW))]) == OK


def test_a_bad_file_exits_non_zero(tmp_path):
    assert main([str(_file(tmp_path, HEADER, f"{MOVIE},{TRIGGER},"))]) == FAILED


def test_every_named_file_is_checked(tmp_path):
    good = _file(tmp_path, HEADER, ROW)
    bad = tmp_path / "bad.csv"
    bad.write_text(f"{HEADER}\n{MOVIE},{TRIGGER},\n", encoding="utf-8")

    assert main([str(good), str(bad)]) == FAILED


def test_the_shipped_file_is_what_it_checks_by_default():
    assert main([]) == OK
