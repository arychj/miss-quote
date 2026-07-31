"""
The shipped prompts, and the contracts they carry.

A prompt is prose, so nothing here can check that it works — that takes a model
and a transcript. What it can check is that the instructions a prompt exists to
carry are still in it, because those are exactly what a tidy-up removes without
anybody noticing until something reads a stage direction out loud.
"""

import pytest

from miss_quote.summary import prompts

WORDS = 200


def _resolved(name: str) -> str:
    return prompts.resolve(name, prompts.library(), WORDS)


def test_the_shipped_prompts_are_the_ones_the_defaults_name():
    assert prompts.DEFAULT_SUMMARY_PROMPT in prompts.BUILTIN
    assert prompts.DEFAULT_RETELLING_PROMPT in prompts.BUILTIN


@pytest.mark.parametrize("name", sorted(prompts.BUILTIN))
def test_every_shipped_prompt_resolves(name):
    assert _resolved(name).strip()


@pytest.mark.parametrize("name", (prompts.RECAP_PROMPT, prompts.MINUTES_PROMPT))
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
    said = _resolved(prompts.BARD_PROMPT).lower()

    for forbidden in ("markdown", "asterisk", "bullet", "emoji", "heading"):
        assert forbidden in said, forbidden


def test_the_retelling_asks_for_an_outside_narrator():
    """
    Told only to retell a conversation to the people who were in it, a model
    concludes it was one of them and says "Ryan and I decided" — a bot claiming
    to have been in the room, out loud, in that room. Third person has to be
    asked for, so it has to stay asked for.
    """
    said = _resolved(prompts.BARD_PROMPT).lower()

    assert "third person" in said
    assert "you were not there" in said
    assert '"i"' in said or "'i'" in said


def test_the_retelling_carries_the_length_it_was_given():
    assert str(WORDS) in _resolved(prompts.BARD_PROMPT)
    assert prompts.WORDS_PLACEHOLDER not in _resolved(prompts.BARD_PROMPT)


def test_a_custom_prompt_is_added_to_the_shipped_ones():
    available = prompts.library({"terse": "Three sentences."})

    assert available["terse"] == "Three sentences."
    assert prompts.RECAP_PROMPT in available


def test_a_custom_prompt_under_a_shipped_name_replaces_it():
    """How a server keeps the structure of a prompt and changes its tone."""
    available = prompts.library({prompts.BARD_PROMPT: "Tell it badly."})

    assert available[prompts.BARD_PROMPT] == "Tell it badly."


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
