"""What a stem grows into, and how it is spelled on the way."""

from utils.stems import expand

STEM = "fiddlestick"


def _expand(stem: str) -> set[str]:
    return set(expand(stem))


# ── the forms ─────────────────────────────────────


def test_the_stem_is_one_of_its_own_forms():
    assert STEM in _expand(STEM)


def test_a_stem_grows_the_endings_it_is_said_with():
    grown = _expand(STEM)

    assert {
        f"{STEM}s",
        f"{STEM}ed",
        f"{STEM}ing",
        f"{STEM}er",
        f"{STEM}ers",
        f"{STEM}y",
    } <= grown


def test_the_spoken_gerund_counts_too():
    """Nobody pronounces the g, and a transcript writes down what it heard."""
    assert f"{STEM}in" in _expand(STEM)


def test_a_form_is_listed_once():
    assert len(expand(STEM)) == len(set(expand(STEM)))


# ── spelling ──────────────────────────────────────


def test_a_short_stem_doubles_its_final_consonant():
    """The whole point of the exercise: shitter, not shiter."""
    grown = _expand("shit")

    assert {"shitted", "shitting", "shitter", "shitters", "shitty"} <= grown
    assert "shiter" not in grown


def test_a_longer_stem_does_not_double():
    """A second syllable stands in for the stress rule: buggered, not buggerred."""
    grown = _expand("bugger")

    assert "buggered" in grown
    assert "buggerred" not in grown


def test_a_stem_ending_in_two_consonants_does_not_double():
    grown = _expand("wank")

    assert {"wanked", "wanking", "wanker"} <= grown
    assert "wankked" not in grown


def test_w_and_x_are_never_doubled():
    assert "boxed" in _expand("box")
    assert "boxxed" not in _expand("box")


def test_a_silent_e_is_dropped_before_a_vowel():
    grown = _expand("arse")

    assert {"arsed", "arsing", "arses"} <= grown
    assert "arseed" not in grown


def test_a_y_after_a_consonant_becomes_an_i():
    grown = _expand("bloody")

    assert {"bloodied", "bloodies", "bloodier"} <= grown
    assert "bloodyed" not in grown


def test_a_y_survives_the_ending_that_starts_with_one():
    """An i too many is what the surviving y keeps out of "bloodiing"."""
    assert "bloodying" in _expand("bloody")


def test_a_stem_already_ending_in_y_grows_no_second_one():
    assert "bloodyy" not in _expand("bloody")


def test_a_sibilant_takes_es():
    assert "bitches" in _expand("bitch")
    assert "asses" in _expand("ass")
    assert "bitchs" not in _expand("bitch")


def test_a_stem_of_two_words_is_suffixed_at_the_end():
    assert "god damning" in _expand("god damn")
