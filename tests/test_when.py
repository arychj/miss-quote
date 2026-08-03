from datetime import date

from miss_quote.summary import when as clauses
from miss_quote.summary.when import LATEST, UNSAID

# The last day of a long month, so "the thirty first" has somewhere to land, and
# a day early enough in it that most ordinals fall in the month before.
TODAY = date(2026, 7, 31)
EARLY = date(2026, 7, 5)

EXACT_DAY = 0
NEAREST_DAYS = 3


def _after(stem: str, said: str, today: date = TODAY):
    """What one sentence asked for, given where its trigger stem ended."""
    return clauses.parse(f"{stem}{said}", len(stem), today)


# ── the most recent one ───────────────────────


def test_a_stem_on_its_own_is_the_last_one():
    """Somebody who says "Miss Quote, what happened" means last time."""
    assert _after("what happened", "").latest


def test_a_stem_on_its_own_is_assumed_rather_than_said():
    """It is the one answer here the next utterance could still change."""
    assert _after("what happened", "") is UNSAID


def test_a_clause_that_was_said_is_not_assumed():
    """"Last session" is finished, and nothing arriving after it is the rest."""
    assert not _after("what happened", " last session").assumed


def test_the_wordings_for_last_time_all_mean_the_same():
    for said in (" last time", " last session", " last night", " last one"):
        assert _after("what happened", said) is LATEST, said


def test_filler_between_the_stem_and_the_clause_is_ignored():
    assert _after("recap", " the last session") is LATEST


def test_a_clause_can_be_followed_by_the_rest_of_the_sentence():
    """One VAD segment is not always one sentence."""
    assert _after("what happened", " last session i missed all of it") is LATEST


# ── counting back weeks ───────────────────────


def test_counting_back_weeks_lands_that_many_weeks_back():
    asked = _after("what happened", " two weeks ago")

    assert asked == clauses.When(target=date(2026, 7, 17), tolerance_days=NEAREST_DAYS)


def test_a_week_and_one_week_and_last_week_are_the_same_week():
    expected = clauses.When(target=date(2026, 7, 24), tolerance_days=NEAREST_DAYS)

    for said in (" a week ago", " one week ago", " last week"):
        assert _after("what happened", said) == expected, said


def test_counting_back_gets_room_either_side():
    """A channel that meets on a night of the week does not meet on a date."""
    assert _after("what happened", " three weeks ago").tolerance_days == NEAREST_DAYS


# ── naming a day ──────────────────────────────


def test_every_ordinal_word_names_its_day():
    """
    Read back out of the table rather than asserted one at a time, because the
    table is composed and a composed table is where an off-by-one hides.
    """
    for word, day in clauses.ORDINALS.items():
        asked = _after("what happened", f" on the {word}", today=date(2026, 8, 31))

        assert asked is not None, word
        assert asked.target.day == day, word


def test_naming_a_day_asks_for_that_day_exactly():
    assert _after("what happened", " on the twelfth").tolerance_days == EXACT_DAY


def test_a_two_word_ordinal_beats_the_one_inside_it():
    """With "first" ahead of "twenty first", the twenty-first is the first."""
    assert _after("what happened", " on the twenty first").target == date(2026, 7, 21)


def test_a_day_earlier_this_month_is_this_month():
    assert _after("what happened", " on the twelfth").target == date(2026, 7, 12)


def test_a_day_not_yet_reached_is_last_month():
    assert _after("what happened", " on the twelfth", today=EARLY).target == date(2026, 6, 12)


def test_today_is_last_month():
    """A day that has not finished is not an evening anybody has notes from."""
    assert _after("what happened", " on the fifth", today=EARLY).target == date(2026, 6, 5)


def test_a_day_the_month_does_not_have_is_nobodys_evening():
    """The thirty-first of a month with thirty, rather than sliding to the first."""
    assert _after("what happened", " on the thirty first", today=date(2026, 5, 10)) is None


def test_a_transcriber_that_wrote_digits_is_understood_too():
    assert _after("what happened", " on the 12th").target == date(2026, 7, 12)


def test_a_bare_number_is_not_a_date():
    """"Recap the three things" is a request, and it is not about a date."""
    assert _after("recap", " the 3 things") is None


# ── what is not a question ────────────────────


def test_a_stem_followed_by_something_else_is_not_a_question():
    assert _after("what happened", " to my beer") is None


def test_a_clause_further_along_the_sentence_is_not_the_answer():
    """It belongs to a different part of the sentence, so it is not read."""
    assert _after("recap", " the rules i was away until the twelfth") is None


def test_a_wording_this_does_not_know_is_not_guessed_at():
    assert _after("what did we do", " yesterday") is None
