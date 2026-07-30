"""What a stem grows into, and how it is spelled on the way."""

from miss_quote.utils.stems import expand

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


def test_a_stem_grows_the_endings_that_make_it_a_noun_again():
    grown = _expand(STEM)

    assert {f"{STEM}ity", f"{STEM}ery", f"{STEM}iness"} <= grown


def test_the_endings_that_make_real_words_make_the_real_words():
    """The point of the exercise: fuckery and shittiness are what people say."""
    assert "fuckery" in _expand("fuck")
    assert {"shittery", "shittiness"} <= _expand("shit")
    assert "buggery" in _expand("bugger")


def test_a_y_becomes_an_i_only_once_before_an_i():
    """It is "bloodiness", not "bloodyiness" and not "bloodiiness"."""
    grown = _expand("bloody")

    assert "bloodiness" in grown
    assert not {"bloodyiness", "bloodiiness"} & grown


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


# ── compounds ─────────────────────────────────────


def test_a_compound_doubles_the_way_the_word_it_ends_with_does():
    """
    "dipshit" is two syllables and still takes "dipshitting".

    The syllable count stands in for stress everywhere else, and a compound is
    where that substitution gets it wrong: the stress goes with the word on the
    end.
    """
    grown = _expand("dipshit")

    assert {"dipshitting", "dipshitter", "dipshitted"} <= grown
    assert "dipshiting" not in grown


def test_the_compound_still_pluralizes_without_doubling():
    """Nothing doubles before a plural `s`, compound or not."""
    assert "dipshits" in _expand("dipshit")
    assert "dipshitts" not in _expand("dipshit")


def test_the_word_on_the_end_is_what_decides():
    assert "bullshitting" in _expand("bullshit")
    assert "horseshitter" in _expand("horseshit")
    assert "asshatting" in _expand("asshat")
    assert "fuckwitted" in _expand("fuckwit")
    assert "scumbagging" in _expand("scumbag")


def test_a_longer_stem_that_is_not_a_compound_still_does_not_double():
    """The counterexamples any fix here has to keep working."""
    assert "buggering" in _expand("bugger")
    assert "buggerring" not in _expand("bugger")

    assert "visiting" in _expand("visit")
    assert "visitting" not in _expand("visit")

    assert "offering" in _expand("offer")
    assert "offerring" not in _expand("offer")


def test_a_compound_ending_that_never_doubled_alone_changes_nothing():
    """`fuck` ends in a cluster, so a compound ending in it doubles nothing."""
    assert "clusterfucking" in _expand("clusterfuck")
    assert "clusterfuckking" not in _expand("clusterfuck")
