"""What the quote-file validator catches, and that it agrees with the loader about the file it is checking."""

import pytest
import yaml

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

MOVIE = "Firefly"
TRIGGER = "cool"
QUOTE = "Shiny."

OTHER_TRIGGER = "behave"
OTHER_QUOTE = "I aim to misbehave."

LATER_MOVIE = "The Matrix"
LATER_TRIGGER = "spoon"
LATER_QUOTE = "There is no spoon."

# The first entry of the file, which is the second line of it: a title takes the
# first line and its triggers are indented under it.
FIRST_ENTRY = 2

# Wide enough that safe_dump never folds a line across two of them.
NO_FOLDING = 4096

NOTHING = 0
ONE_PROBLEM = 1


def _file(tmp_path, quotes, newline: bool = True):
    """A quote file holding whatever a mapping can express."""
    text = yaml.safe_dump(quotes, allow_unicode=True, sort_keys=False, width=NO_FOLDING)

    return _raw(tmp_path, text if newline else text.rstrip("\n"))


def _raw(tmp_path, text: str):
    """
    The same, for a document a mapping cannot express.

    A key written twice, a value YAML reads as something other than text, and a
    file that will not parse at all are each things the validator has an opinion
    about and none of them is something `safe_dump` can be asked for.
    """
    path = tmp_path / "quotes.yaml"
    path.write_text(text, encoding="utf-8")

    return path


def _one(movie: str = MOVIE, trigger: str = TRIGGER, quote: str = QUOTE):
    """The smallest file there is: one title, one trigger, one line."""
    return {movie: {trigger: quote}}


def _details(path) -> list[str]:
    return [problem.detail for problem in problems(path)]


# ── the file the image ships ──────────────────────


def test_the_shipped_file_is_valid():
    """The one check that would have to fail before anything reached a channel."""
    assert problems(BUNDLED_QUOTES) == []


def test_the_validator_and_the_loader_agree_about_the_file():
    """
    The validator names these rather than importing them, so that it can run
    without the runtime installed. This is what stops the two drifting.
    """
    assert validate_quotes.USER_PLACEHOLDER == tool.USER_PLACEHOLDER
    assert validate_quotes.KEY_SEPARATOR == tool.KEY_SEPARATOR
    assert validate_quotes.STRING_TAG == tool.STRING_TAG
    assert validate_quotes.EDITOR_OFFSET == tool.EDITOR_OFFSET
    assert validate_quotes.FILE_ENCODING == tool.FILE_ENCODING


# ── the shape of the file ─────────────────────────


def test_a_good_file_has_nothing_wrong_with_it(tmp_path):
    assert problems(_file(tmp_path, _one())) == []


def test_a_missing_file_is_reported(tmp_path):
    assert "no such file" in _details(tmp_path / "absent.yaml")[0]


def test_an_empty_file_is_reported(tmp_path):
    """Reported as what it is rather than as whatever the parser made of it."""
    assert "is empty" in _details(_raw(tmp_path, ""))[0]


def test_a_document_of_no_quotes_is_reported(tmp_path):
    assert "no quotes" in _details(_raw(tmp_path, "{}\n"))[0]


def test_a_file_that_will_not_parse_is_reported(tmp_path):
    path = _raw(tmp_path, f'{MOVIE}:\n  {TRIGGER}: "unclosed\n')
    problem = problems(path)[0]

    assert "not valid YAML" in problem.detail
    assert problem.line > NOTHING


def test_a_top_level_list_is_reported(tmp_path):
    assert "mapping of titles" in _details(_raw(tmp_path, f"- {MOVIE}\n- {LATER_MOVIE}\n"))[0]


def test_a_title_holding_no_mapping_is_reported(tmp_path):
    assert "does not hold a mapping" in _details(_raw(tmp_path, f"{MOVIE}: just a line\n"))[0]


def test_a_title_holding_no_triggers_is_reported(tmp_path):
    assert "no triggers" in _details(_file(tmp_path, {MOVIE: {}}))[0]


def test_a_missing_final_newline_is_reported(tmp_path):
    assert "newline at end of file" in _details(_file(tmp_path, _one(), newline=False))[0]


def test_crlf_line_endings_are_reported(tmp_path):
    path = tmp_path / "quotes.yaml"
    path.write_bytes(f"{MOVIE}:\r\n  {TRIGGER}: {QUOTE}\r\n".encode())

    assert "CRLF" in _details(path)[0]


# ── what YAML read it as ──────────────────────────


@pytest.mark.parametrize("written", ("no", "yes", "on", "off", "true"))
def test_a_trigger_yaml_reads_as_a_boolean_is_reported(tmp_path, written):
    """
    The mistake this format makes possible and a CSV could not.

    It looks entirely correct in the file, it is not a string the matcher can
    compare against, and nothing else anywhere fails because of it.
    """
    detail = _details(_raw(tmp_path, f"{MOVIE}:\n  {written}: {QUOTE}\n"))[0]

    assert "not text" in detail
    assert "bool" in detail


def test_a_title_yaml_reads_as_a_number_is_reported(tmp_path):
    """`1917` is a real film, and unquoted it is an integer."""
    detail = _details(_raw(tmp_path, f"1917:\n  {TRIGGER}: {QUOTE}\n"))[0]

    assert "not text" in detail
    assert "int" in detail


def test_a_quoted_trigger_yaml_would_coerce_is_fine(tmp_path):
    assert problems(_raw(tmp_path, f"{MOVIE}:\n  'no': {QUOTE}\n")) == []


def test_a_quote_that_is_a_mapping_is_reported(tmp_path):
    """Which is what an unquoted line opening with `{user}` parses as."""
    detail = _details(_raw(tmp_path, f"{MOVIE}:\n  {TRIGGER}:\n    nested: thing\n"))[0]

    assert "rather than text" in detail


# ── one entry ─────────────────────────────────────


@pytest.mark.parametrize(
    ("quotes", "label"),
    (
        ({"": {TRIGGER: QUOTE}}, "movie"),
        ({MOVIE: {"": QUOTE}}, "trigger"),
        ({MOVIE: {TRIGGER: ""}}, "quote"),
        ({MOVIE: {"   ": QUOTE}}, "trigger"),
    ),
)
def test_an_empty_part_of_an_entry_is_reported(tmp_path, quotes, label):
    assert f"empty {label}" in _details(_file(tmp_path, quotes))[0]


def test_surrounding_whitespace_is_reported(tmp_path):
    """
    The loader strips it, so the file and what it produces disagree quietly.

    A plain scalar cannot carry it — YAML strips it on the way in — so the case
    worth catching is the quoted one, which is what `safe_dump` writes here.
    """
    assert "whitespace" in _details(_file(tmp_path, {MOVIE: {f" {TRIGGER} ": QUOTE}}))[0]


@pytest.mark.parametrize(
    ("label", "limit", "quotes"),
    (
        ("trigger", MAXIMUM_TRIGGER_LENGTH, {MOVIE: {"x" * 99: QUOTE}}),
        ("quote", MAXIMUM_QUOTE_LENGTH, {MOVIE: {TRIGGER: "x" * 999}}),
        ("movie", MAXIMUM_MOVIE_LENGTH, {"x" * 99: {TRIGGER: QUOTE}}),
    ),
)
def test_an_overlong_part_of_an_entry_is_reported(tmp_path, label, limit, quotes):
    detail = _details(_file(tmp_path, quotes))[0]

    assert label in detail
    assert str(limit) in detail


def test_a_field_at_its_limit_is_fine(tmp_path):
    quotes = {MOVIE: {"x" * MAXIMUM_TRIGGER_LENGTH: "x" * MAXIMUM_QUOTE_LENGTH}}

    assert problems(_file(tmp_path, quotes)) == []


# ── a trigger that could never fire ───────────────


def test_a_trigger_of_pure_punctuation_is_reported(tmp_path):
    """It compiles into the pattern happily and then matches nothing."""
    assert "never match" in _details(_file(tmp_path, _one(trigger="!!!")))[0]


def test_a_trigger_with_a_double_space_is_reported(tmp_path):
    assert "repeated whitespace" in _details(_file(tmp_path, _one(trigger="game  over")))[0]


def test_a_placeholder_in_a_trigger_is_reported(tmp_path):
    """Triggers are matched as written; nothing interpolates them."""
    assert "interpolate nothing" in _details(_file(tmp_path, _one(trigger="hey {user}")))[0]


# ── what a line interpolates ──────────────────────


def test_the_user_placeholder_is_allowed(tmp_path):
    assert problems(_file(tmp_path, _one(quote="Hello {user}."))) == []


def test_any_other_placeholder_is_reported(tmp_path):
    """The loader drops the entry, so the symptom is a line that is never said."""
    detail = _details(_file(tmp_path, _one(quote="Hello {tally}.")))[0]

    assert "{tally}" in detail
    assert "{user}" in detail


def test_an_unclosed_brace_is_reported(tmp_path):
    assert any(
        "interpolate" in detail
        for detail in _details(_file(tmp_path, _one(quote="Hello {user.")))
    )


# ── a trigger with more than one answer ───────────


def test_a_trigger_may_list_several_answers(tmp_path):
    assert problems(_file(tmp_path, {MOVIE: {TRIGGER: [QUOTE, OTHER_QUOTE]}})) == []


def test_a_trigger_listing_nothing_is_reported(tmp_path):
    assert "lists no lines" in _details(_file(tmp_path, {MOVIE: {TRIGGER: []}}))[0]


def test_the_same_answer_listed_twice_is_reported(tmp_path):
    """A line pasted and half-edited, whose only effect is to weight the draw."""
    assert "more than once" in _details(_file(tmp_path, {MOVIE: {TRIGGER: [QUOTE, QUOTE]}}))[0]


def test_a_repeated_answer_differing_only_in_case_is_reported(tmp_path):
    path = _file(tmp_path, {MOVIE: {TRIGGER: [QUOTE, QUOTE.upper()]}})

    assert "more than once" in _details(path)[0]


def test_every_listed_answer_is_checked(tmp_path):
    """A list of four with one bad line in it is one problem, not none."""
    path = _file(tmp_path, {MOVIE: {TRIGGER: [QUOTE, "Hello {tally}."]}})
    detail = _details(path)[0]

    assert "{tally}" in detail


def test_a_listed_answer_is_reported_at_its_own_line(tmp_path):
    path = _file(tmp_path, {MOVIE: {TRIGGER: [QUOTE, OTHER_QUOTE, "Hello {tally}."]}})

    assert problems(path)[0].line == len(path.read_text(encoding="utf-8").splitlines())


def test_a_listed_answer_yaml_did_not_read_as_text_is_reported(tmp_path):
    detail = _details(_raw(tmp_path, f"{MOVIE}:\n  {TRIGGER}:\n    - {QUOTE}\n    - 1917\n"))[0]

    assert "not text" in detail


# ── one entry against the rest ────────────────────


def test_a_trigger_repeated_under_another_title_is_reported(tmp_path):
    """A trigger answers with one line, wherever in the file it was written."""
    path = _file(tmp_path, {MOVIE: {TRIGGER: QUOTE}, LATER_MOVIE: {TRIGGER: OTHER_QUOTE}})
    detail = _details(path)[0]

    assert "already answers" in detail
    assert MOVIE in detail


def test_the_repeated_trigger_is_reported_at_the_later_one(tmp_path):
    """One problem per line somebody has to go and delete, and it is the second."""
    path = _file(tmp_path, {MOVIE: {TRIGGER: QUOTE}, LATER_MOVIE: {TRIGGER: OTHER_QUOTE}})
    found = problems(path)

    assert len(found) == ONE_PROBLEM
    assert found[0].line > FIRST_ENTRY


def test_a_trigger_repeated_under_one_title_is_reported(tmp_path):
    """Which `safe_dump` cannot write, so only the raw text can say it."""
    path = _raw(tmp_path, f"{MOVIE}:\n  {TRIGGER}: {QUOTE}\n  {TRIGGER}: {OTHER_QUOTE}\n")

    assert "written twice under this title" in _details(path)[0]


def test_a_title_written_twice_is_reported(tmp_path):
    path = _raw(
        tmp_path,
        f"{MOVIE}:\n  {TRIGGER}: {QUOTE}\n{MOVIE}:\n  {OTHER_TRIGGER}: {OTHER_QUOTE}\n",
    )

    assert "written twice" in _details(path)[0]


def test_a_repeat_differing_only_in_case_is_reported(tmp_path):
    """The loader folds the trigger, so these are one trigger written twice."""
    path = _file(
        tmp_path, {MOVIE: {TRIGGER: QUOTE}, LATER_MOVIE: {TRIGGER.upper(): OTHER_QUOTE}}
    )

    assert "already answers" in _details(path)[0]


def test_two_triggers_may_share_an_answer(tmp_path):
    assert problems(_file(tmp_path, {MOVIE: {TRIGGER: QUOTE, OTHER_TRIGGER: QUOTE}})) == []


def test_a_title_out_of_order_is_reported(tmp_path):
    path = _file(
        tmp_path, {LATER_MOVIE: {TRIGGER: QUOTE}, "Aliens": {OTHER_TRIGGER: OTHER_QUOTE}}
    )
    detail = _details(path)[0]

    assert "grouped by title" in detail
    assert LATER_MOVIE in detail


def test_triggers_under_one_title_may_be_in_any_order(tmp_path):
    """Only the titles are ordered; the triggers under one are the author's business."""
    path = _file(
        tmp_path,
        {
            MOVIE: {OTHER_TRIGGER: OTHER_QUOTE, TRIGGER: QUOTE},
            LATER_MOVIE: {LATER_TRIGGER: LATER_QUOTE},
        },
    )

    assert problems(path) == []


# ── what it reports ───────────────────────────────


def test_problems_are_reported_in_line_order(tmp_path):
    path = _file(
        tmp_path,
        {MOVIE: {TRIGGER: QUOTE, OTHER_TRIGGER: "", "": QUOTE}},
    )

    assert [problem.line for problem in problems(path)] == sorted(
        problem.line for problem in problems(path)
    )


def test_a_problem_names_the_file_and_the_line(tmp_path):
    path = _file(tmp_path, _one(quote=""))

    assert str(problems(path)[0]).startswith(f"{path}:{FIRST_ENTRY}:")


def test_a_problem_names_the_line_the_entry_is_actually_on(tmp_path):
    """A reported line number nobody can go and look at is not worth reporting."""
    path = _file(
        tmp_path, {MOVIE: {TRIGGER: QUOTE}, LATER_MOVIE: {LATER_TRIGGER: "Hello {tally}."}}
    )

    assert problems(path)[0].line == len(path.read_text(encoding="utf-8").splitlines())


def test_a_good_file_exits_zero(tmp_path):
    assert main([str(_file(tmp_path, _one()))]) == OK


def test_a_bad_file_exits_non_zero(tmp_path):
    assert main([str(_file(tmp_path, _one(quote="")))]) == FAILED


def test_every_named_file_is_checked(tmp_path):
    good = _file(tmp_path, _one())
    bad = tmp_path / "bad.yaml"
    bad.write_text(f"{MOVIE}:\n  {TRIGGER}: ''\n", encoding="utf-8")

    assert main([str(good), str(bad)]) == FAILED


def test_the_shipped_file_is_what_it_checks_by_default():
    assert main([]) == OK
