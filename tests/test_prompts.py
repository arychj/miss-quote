"""
The shipped prompts, and the contracts they carry.

A prompt is prose, so nothing here can check that it works — that takes a model
and a transcript. What it can check is that the instructions a prompt exists to
carry are still in it, because those are exactly what a tidy-up removes without
anybody noticing until something reads a stage direction out loud.

The loader is checked the other way round: against files written to be wrong, so
that a shipped file that is wrong says which part and stops.
"""

from pathlib import Path

import pytest
import yaml

from miss_quote.summary import prompts

WORDS = 200

RECAP = "recap"
MINUTES = "minutes"
BARD = "bard"

TRANSCRIPT_FRAGMENT = "transcript_instructions"
CLOSING_FRAGMENT = "retelling_closing"


def _resolved(name: str) -> str:
    return prompts.resolve(name, prompts.library(), WORDS)


def _written(tmp_path: Path, **sections) -> Path:
    """A prompts file of our own, to be wrong on purpose."""
    path = tmp_path / "prompts.yaml"
    path.write_text(yaml.safe_dump(sections), encoding="utf-8")

    return path


def _whole(**overrides) -> dict:
    """A file with nothing wrong with it, before one thing is made wrong."""
    return {
        "defaults": {"summary": RECAP, "retelling": BARD},
        "fragments": {TRANSCRIPT_FRAGMENT: "A transcript."},
        "prompts": {RECAP: f"{{{TRANSCRIPT_FRAGMENT}}} Summarize.", BARD: "Retell it."},
    } | overrides


# ── what the shipped prompts say ──────────────


def test_the_shipped_prompts_are_the_ones_the_defaults_name():
    assert prompts.DEFAULT_SUMMARY_PROMPT in prompts.BUILTIN
    assert prompts.DEFAULT_RETELLING_PROMPT in prompts.BUILTIN


@pytest.mark.parametrize("name", sorted(prompts.BUILTIN))
def test_every_shipped_prompt_resolves(name):
    assert _resolved(name).strip()


@pytest.mark.parametrize("name", (RECAP, MINUTES))
def test_a_summarizing_prompt_describes_the_script_it_is_given(name):
    """
    The script carries no timestamps and the lines are `Name: what they said`.
    A prompt that does not say so is a prompt inventing a format.
    """
    said = _resolved(name)

    assert "Name: what they said" in said
    assert "order they were spoken" in said
    assert "speech recognition" in said


def test_the_retelling_forbids_what_a_synthesizer_would_read_aloud():
    """An asterisk is a word to a synthesizer and a bullet is nothing at all."""
    said = _resolved(BARD).lower()

    for forbidden in ("markdown", "asterisk", "bullet", "emoji", "heading"):
        assert forbidden in said, forbidden


def test_the_retelling_asks_for_an_outside_narrator():
    """
    Told only to retell a conversation to the people who were in it, a model
    concludes it was one of them and says "Ryan and I decided" — a bot claiming
    to have been in the room, out loud, in that room. Third person has to be
    asked for, so it has to stay asked for.
    """
    said = _resolved(BARD).lower()

    assert "third person" in said
    assert "you were not there" in said
    assert '"i"' in said or "'i'" in said


def test_the_retelling_is_told_by_a_bard():
    """The persona is the point of the prompt, and it is one line to delete."""
    said = _resolved(BARD).lower()

    assert "bard" in said
    assert "adventurers" in said


def test_the_retelling_ends_its_own_story():
    """
    A channel's `closing` is off unless somebody asks for it, so this is the only
    thing telling the room the tale finished rather than stopped. It is the line
    in the file most worth protecting from a tidy-up.
    """
    said = _resolved(BARD).lower()

    assert "the tale is over" in said
    assert "do not trail off" in said


def test_the_retelling_is_told_to_leave_things_out():
    """A minute of the parts worth telling, rather than all of it under a cap."""
    said = _resolved(BARD).lower()

    assert "worth telling" in said
    assert "do not pad" in said
    assert "keep it under" not in said


def test_the_retelling_carries_the_length_it_was_given():
    assert str(WORDS) in _resolved(BARD)
    assert prompts.WORDS_PLACEHOLDER not in _resolved(BARD)


# ── fragments ─────────────────────────────────


@pytest.mark.parametrize("fragment", (TRANSCRIPT_FRAGMENT, CLOSING_FRAGMENT))
def test_no_shipped_prompt_still_asks_for_a_fragment(fragment):
    for name in prompts.BUILTIN:
        assert "{" + fragment + "}" not in _resolved(name), name


def test_a_custom_prompt_is_given_the_fragments_too():
    """
    So that a retelling prompt of somebody's own can ask for the same ending
    discipline the shipped one has rather than restating it or going without.
    """
    available = prompts.library({"mine": f"Retell it. {{{CLOSING_FRAGMENT}}}"})

    assert CLOSING_FRAGMENT not in available["mine"]
    assert "the tale is over" in available["mine"]


# ── the library ───────────────────────────────


def test_a_custom_prompt_is_added_to_the_shipped_ones():
    available = prompts.library({"terse": "Three sentences."})

    assert available["terse"] == "Three sentences."
    assert RECAP in available


def test_a_custom_prompt_under_a_shipped_name_replaces_it():
    """How a server keeps the structure of a prompt and changes its tone."""
    available = prompts.library({BARD: "Tell it badly."})

    assert available[BARD] == "Tell it badly."


def test_a_name_nothing_answers_to_is_refused():
    with pytest.raises(prompts.UnknownPrompt, match="no prompt named"):
        prompts.resolve("nonexistent", prompts.library(), WORDS)


def test_the_refusal_lists_what_there_is_instead():
    with pytest.raises(prompts.UnknownPrompt) as raised:
        prompts.resolve("nonexistent", prompts.library(), WORDS)

    for name in prompts.BUILTIN:
        assert name in str(raised.value)


def test_braces_in_a_custom_prompt_are_left_alone():
    """Substituted rather than formatted, so an example of JSON survives."""
    available = prompts.library({"json": 'Answer with {"summary": "..."}'})

    assert prompts.resolve("json", available, WORDS) == 'Answer with {"summary": "..."}'


# ── the file ──────────────────────────────────


def test_a_file_that_is_not_there_names_itself(tmp_path):
    with pytest.raises(ValueError, match="Could not read the prompts"):
        prompts._load(tmp_path / "absent.yaml")


def test_a_file_that_will_not_parse_names_itself(tmp_path):
    path = tmp_path / "prompts.yaml"
    path.write_text("prompts: [unclosed", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid YAML"):
        prompts._load(path)


def test_a_file_with_nothing_to_choose_from_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mapping of names to text"):
        prompts._load(_written(tmp_path, **_whole(prompts=None)))


def test_a_prompt_that_is_not_text_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must be text"):
        prompts._load(_written(tmp_path, **_whole(prompts={RECAP: 5})))


def test_a_fragment_cannot_be_called_words(tmp_path):
    """It is filled per channel, so one answering to the name everywhere would
    quietly stop `retelling_words` being a setting."""
    written = _whole(fragments={"words": "sixty"})

    with pytest.raises(ValueError, match="cannot be a fragment"):
        prompts._load(_written(tmp_path, **written))


def test_a_shipped_prompt_asking_for_something_nothing_fills_is_refused(tmp_path):
    """A misspelled fragment is caught here rather than read out in the instructions."""
    written = _whole(prompts={RECAP: "{transcript_instrutions} Summarize.", BARD: "Retell it."})

    with pytest.raises(ValueError, match=r"asks for '\{transcript_instrutions\}'"):
        prompts._load(_written(tmp_path, **written))


def test_the_length_placeholder_survives_the_file(tmp_path):
    """`{words}` is not a fragment and is not supposed to be filled yet."""
    written = _whole(prompts={RECAP: "Summarize.", BARD: "Retell it in {words} words."})
    loaded = prompts._load(_written(tmp_path, **written))

    assert prompts.WORDS_PLACEHOLDER in loaded.prompts[BARD]


@pytest.mark.parametrize("key", ("summary", "retelling"))
def test_a_default_naming_a_prompt_that_is_not_there_is_refused(tmp_path, key):
    written = _whole(defaults={"summary": RECAP, "retelling": BARD} | {key: "nonexistent"})

    with pytest.raises(ValueError, match=f"'defaults.{key}'"):
        prompts._load(_written(tmp_path, **written))
