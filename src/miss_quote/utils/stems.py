"""
Stems, and the words they grow into.

A list of words a server objects to is written as stems: `fuck` is meant to
catch `fucking` and `fuckers` without the file having to say so, because a list
that has to be exhaustive is a list that will be incomplete the first time
somebody conjugates.

The rules here are English spelling rather than a dictionary. A final consonant
doubles after a short vowel, so `shit` grows a `shitter` and not a `shiter`; a
silent `e` drops before a vowel; a `y` after a consonant becomes `i`, except
before a suffix that starts with one, where it goes without being replaced —
`shittiness`, not `shittyiness`. Nothing
checks whether the result is a word anyone says, and it does not matter: `fucky`
costs a few bytes in an alternation, while a missing `shitting` costs the tool
the thing it exists to catch.

Expansion happens once, when a tool is built.
"""

from __future__ import annotations

VOWELS = frozenset("aeiou")

# Counting syllables, `y` is doing a vowel's job: "bloody" is two, not one.
SYLLABLE_VOWELS = VOWELS | {"y"}

# It is "boxed" and "flowed", never "boxxed" or "flowwed".
NEVER_DOUBLED = frozenset("wxy")

# Words that carry their doubling into a compound: "dipshit" takes
# "dipshitting" the way "shit" takes "shitting".
#
# English doubles before a vowel-initial suffix when the stress falls on the
# final syllable, and a compound keeps the stress of the word it ends with.
# `_syllables` stands in for stress everywhere else here, and for a compound it
# gives the wrong answer — which is why these are named rather than derived.
#
# Nothing structural can tell them apart. "dipshit" splits as "dip" + "shit" and
# "bugger" splits as "bug" + "ger" with the same shape, so a rule that doubles
# the first doubles the second and produces "buggerred". The difference is that
# "shit" is a word and "ger" is not, which needs a dictionary this does not have.
#
# Add to this when a server's list grows a compound that conjugates wrong. Only
# an entry that doubles on its own does anything, so "fuck" and "ass" would be
# inert here: they end in a consonant cluster and never doubled to begin with.
COMPOUND_ENDINGS = frozenset(
    {"shit", "wit", "bag", "hat", "nut", "wad", "git", "prat", "twat"}
)

PLURAL = "s"
SIBILANT_PLURAL = "es"
SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")
Y_PLURAL = "ies"

PAST = "ed"
GERUND = "ing"
AGENT = "er"
ADJECTIVE = "y"

# What a stem is when it is a quality, a carrying-on, or a state of affairs:
# "fiddlestickity", "fuckery", "shittiness".
QUALITY = "ity"
PRACTICE = "ery"
STATE = "iness"

# The gerund as it is said rather than written: "fuckin", not "fucking". Word
# boundaries make the apostrophe somebody may have typed after it irrelevant.
DROPPED_G = "g"

SILENT_E = "e"
Y = "y"
I = "i"

# Below this there is no consonant-vowel-consonant tail to double.
SHORTEST_DOUBLE = 3
ONE_SYLLABLE = 1


def expand(stem: str) -> list[str]:
    """
    Every form of a stem worth listening for, the stem itself included.

    The suffixes are the ones that turn a swear into another swear: a plural, a
    past tense, a gerund, someone who does it, something that is like it, and
    the three that make it a noun again — a quality, a carrying-on, and a state.
    Comparatives and superlatives are left out — nobody is fined for being the
    shittiest.
    """
    gerund = _suffixed(stem, GERUND)
    agent = _suffixed(stem, AGENT)

    forms = [
        stem,
        plural(stem),
        _suffixed(stem, PAST),
        gerund,
        gerund.removesuffix(DROPPED_G),
        agent,
        plural(agent),
        _suffixed(stem, QUALITY),
        _suffixed(stem, PRACTICE),
        _suffixed(stem, STATE),
    ]

    # A stem that already ends in `y` is one: "bloody" needs no "bloodyy".
    if not stem.endswith(Y):
        forms.append(_suffixed(stem, ADJECTIVE))

    return sorted(set(forms))


def _suffixed(stem: str, suffix: str) -> str:
    """
    A stem and a vowel-initial suffix, joined the way the word is spelled.

    Every suffix this is asked for begins with a vowel, which is what makes the
    three adjustments below apply at all: none of them happen before a plural
    `s`, and `plural` handles that ending itself.
    """
    if len(stem) > 1 and stem.endswith(SILENT_E):
        return stem[:-1] + suffix

    if len(stem) > 1 and stem.endswith(Y) and not _vowel(stem[-2]):
        if suffix == GERUND:
            # "bloodying": the one suffix the `y` survives, because dropping it
            # for an `i` would put two of them together.
            return stem + suffix

        if suffix.startswith(I):
            # "bloodiness": the suffix brought its own `i`, so the `y` just goes.
            return stem[:-1] + suffix

        # "bloodied", "bloodier".
        return stem[:-1] + I + suffix

    if _doubles(stem):
        return stem + stem[-1] + suffix

    return stem + suffix


def plural(word: str) -> str:
    """
    A word in the plural, by the spelling rather than by a dictionary.

    Public because the same rule answers a second question: what to call more
    than one of whatever a deployment fines people in. A currency is a noun like
    any other, and `credits`, `bucks`, and `pennies` all fall out of the three
    cases below.
    """
    if word.endswith(SIBILANT_ENDINGS):
        return word + SIBILANT_PLURAL

    if len(word) > 1 and word.endswith(Y) and not _vowel(word[-2]):
        return word[:-1] + Y_PLURAL

    return word + PLURAL


def _doubles(stem: str) -> bool:
    """
    Whether a final consonant doubles before a vowel-initial suffix.

    A short one-syllable stem doubles — that is the difference between
    "shitting" and "shiting" — and a longer one does not, which is the
    difference between "buggered" and "buggerred". English asks where the stress
    falls rather than how many syllables there are; nothing here knows, and the
    syllable count stands in for it, which is right for the single-syllable
    words this is mostly pointed at.

    Where it is wrong is a compound, which keeps the stress of the word it ends
    with however many syllables that leaves: "dipshit" is two and still takes
    "dipshitting". Those are named in `COMPOUND_ENDINGS` rather than worked out,
    for the reason given there.
    """
    if len(stem) < SHORTEST_DOUBLE:
        return False

    if _syllables(stem) != ONE_SYLLABLE and not _compound(stem):
        return False

    last, vowel, before = stem[-1], stem[-2], stem[-3]

    return (
        not _vowel(last)
        and last not in NEVER_DOUBLED
        and _vowel(vowel)
        and not _vowel(before)
    )


def _compound(stem: str) -> bool:
    """Whether a stem ends in a word that doubles on its own."""
    return any(stem.endswith(ending) for ending in COMPOUND_ENDINGS)


def _syllables(word: str) -> int:
    """Runs of vowels, which is as close to a syllable as this needs to get."""
    syllables = 0
    inside = False

    for letter in word:
        if letter in SYLLABLE_VOWELS:
            if not inside:
                syllables += 1
            inside = True
        else:
            inside = False

    return syllables


def _vowel(letter: str) -> bool:
    return letter in VOWELS
