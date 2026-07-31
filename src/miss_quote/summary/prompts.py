"""
What the model is told to do with a transcript, and with a summary.

A closed set shipped with the image, read from `resources/prompts.yaml`, which a
server adds to under `prompts:` and selects from by name. Named rather than
written inline at the point of use so that the two places a prompt is chosen —
the summary and the retelling — are one word each in the config file, and so a
server that wants a different wording writes it once and uses it in both.

Prose lives in the YAML and the rules for filling it live here. The file carries
the text, the fragments shared between prompts, and which prompt does each job
when a channel does not say; this module carries the two substitutions and the
refusal to run on a name nothing answers to.

Where a prompt's output **goes** is the thing that decides how it is written, and
the difference is not cosmetic:

- `recap` and `minutes` are read, in a Discord message. Markdown renders there,
  so they are free to use it.
- `bard` is **spoken**, by a synthesizer, which reads an asterisk out as a word
  and a bullet as nothing at all. It says so at some length for that reason, and
  it ends the story itself rather than trailing off — a channel's `closing` is
  off unless somebody asks for it, so the sign-off is the only thing telling the
  room the tale finished rather than stopped.

Why `bard` spends several lines establishing that the narrator was not there is
written above the prompt itself, which is where somebody would delete it.

Every prompt states the shape of what it is given, because the script it
receives has no timestamps in it: the order is the order things were said, and
nothing in the text says so on its own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

NAME_SEPARATOR = ", "

# The set the image ships with, found relative to this file so a checkout and a
# container agree without either of them being told where they are.
BUNDLED_PROMPTS = Path(__file__).resolve().parent.parent / "resources" / "prompts.yaml"

DEFAULTS_KEY = "defaults"
FRAGMENTS_KEY = "fragments"
PROMPTS_KEY = "prompts"
SUMMARY_DEFAULT_KEY = "summary"
RETELLING_DEFAULT_KEY = "retelling"

FILE_ENCODING = "utf-8"

# What a placeholder looks like, for finding one in a shipped prompt that
# nothing filled. Deliberately narrow: a brace followed by anything other than a
# plain name is somebody's JSON rather than a fragment they misspelled.
UNFILLED = re.compile(r"\{[a-z_][a-z0-9_]*\}", re.IGNORECASE)


def _placeholder(name: str) -> str:
    """What a prompt writes to ask for one piece of shared text."""
    return f"{{{name}}}"


# The one thing a prompt interpolates that is not a fragment, filled from a
# channel's `retelling_words`. Substituted rather than formatted, so a custom
# prompt is free to contain braces — an example of the JSON somebody wants back,
# say — without the substitution turning them into a placeholder it cannot fill.
WORDS_FRAGMENT = "words"
WORDS_PLACEHOLDER = _placeholder(WORDS_FRAGMENT)


class UnknownPrompt(LookupError):
    """A prompt was asked for by a name nothing answers to."""


@dataclass(frozen=True)
class Bundled:
    """The shipped file, parsed and checked."""

    prompts: Mapping[str, str]
    fragments: Mapping[str, str]
    summary: str
    retelling: str


def _load(path: Path) -> Bundled:
    """
    The shipped prompts, or a complaint naming the file.

    Raised on rather than reported, and at import rather than at the moment a
    prompt is asked for. This file ships inside the image, so one that is missing
    a key or names a default nothing answers to is a broken build: the run that
    should not start is every run, and the message wants to be the first thing in
    the log rather than the last.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding=FILE_ENCODING))
    except OSError as exc:
        raise ValueError(f"Could not read the prompts at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"The prompts at {path} are not valid YAML: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ValueError(f"The prompts at {path} must be a mapping, not {type(raw).__name__}.")

    fragments = _strings(path, raw.get(FRAGMENTS_KEY) or {}, FRAGMENTS_KEY)
    prompts = _strings(path, raw.get(PROMPTS_KEY), PROMPTS_KEY)

    if not prompts:
        raise ValueError(f"The prompts at {path} have no '{PROMPTS_KEY}' to choose from.")

    # `{words}` is filled per channel, long after this, so a fragment claiming
    # the name would quietly answer to it everywhere and the length would stop
    # being a setting.
    if WORDS_FRAGMENT in fragments:
        raise ValueError(
            f"'{WORDS_FRAGMENT}' cannot be a fragment in {path}: "
            f"{WORDS_PLACEHOLDER} is filled per channel from 'retelling_words'."
        )

    filled = {name: _filled(text, fragments) for name, text in prompts.items()}
    _check_filled(path, filled)

    defaults = raw.get(DEFAULTS_KEY) or {}

    return Bundled(
        prompts=filled,
        fragments=fragments,
        summary=_default(path, defaults, SUMMARY_DEFAULT_KEY, filled),
        retelling=_default(path, defaults, RETELLING_DEFAULT_KEY, filled),
    )


def _strings(path: Path, raw: object, key: str) -> Mapping[str, str]:
    """One section of the file, with every value checked to be text."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"'{key}' in {path} must be a mapping of names to text, not {raw!r}.")

    for name, text in raw.items():
        if not isinstance(text, str):
            raise ValueError(f"'{name}' under '{key}' in {path} must be text, not {text!r}.")

    return {str(name): text for name, text in raw.items()}


def _check_filled(path: Path, prompts: Mapping[str, str]) -> None:
    """
    A shipped prompt asking for something nothing fills.

    Checked here rather than at the moment the model is asked, by which point the
    prompt has one job and a misspelled fragment is going out in the instructions.
    Only the shipped prompts are checked: braces in a custom one are deliberate,
    and a server writing an example of the JSON it wants back is not making a
    mistake.
    """
    for name, text in prompts.items():
        asked = UNFILLED.search(text.replace(WORDS_PLACEHOLDER, ""))

        if asked is not None:
            raise ValueError(
                f"'{name}' in {path} asks for '{asked.group()}' and nothing fills it."
            )


def _default(
    path: Path,
    defaults: Mapping[str, object],
    key: str,
    prompts: Mapping[str, str],
) -> str:
    """Which prompt does one of the two jobs, checked to be one that exists."""
    named = defaults.get(key)

    if not isinstance(named, str) or named not in prompts:
        raise ValueError(
            f"'{DEFAULTS_KEY}.{key}' in {path} is {named!r}, which is not one of "
            f"{NAME_SEPARATOR.join(repr(known) for known in sorted(prompts))}."
        )

    return named


def _filled(text: str, fragments: Mapping[str, str]) -> str:
    """
    Every fragment written into a prompt by name.

    One pass in the order the file lists them, so a fragment naming another is
    left as it was written rather than expanded to a depth nobody declared.
    """
    for name, value in fragments.items():
        text = text.replace(_placeholder(name), value)

    return text


_BUNDLED = _load(BUNDLED_PROMPTS)

# What a server gets without saying anything: an account of the session in the
# channel, and a spoken retelling of that account when somebody asks for one.
DEFAULT_SUMMARY_PROMPT = _BUNDLED.summary
DEFAULT_RETELLING_PROMPT = _BUNDLED.retelling

BUILTIN: Mapping[str, str] = _BUNDLED.prompts


def library(extra: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """
    The prompts one server can choose from.

    Custom prompts are laid over the built-ins rather than replacing them, so a
    server that wants one extra wording writes one block instead of restating
    the shipped set. A custom prompt under a shipped name replaces it, which is
    how a server that likes the structure of `recap` and not its tone changes
    the tone without inventing a name for it.

    Fragments are written into a custom prompt too, which is what lets one
    retelling prompt of somebody's own ask for the same ending discipline the
    shipped one has rather than going without it.
    """
    merged = {**BUILTIN, **(extra or {})}

    return {name: _filled(text, _BUNDLED.fragments) for name, text in merged.items()}


def resolve(name: str, available: Mapping[str, str], words: int) -> str:
    """
    One prompt, as the model will be given it.

    Raises rather than falling back on a name nothing answers to. A tool running
    on a prompt nobody asked for produces summaries that look fine and are not
    what the file requested, which is worse than a tool the runner reports as
    having refused to start.

    `words` is substituted into any prompt that asks for it. Prompts that do not
    are unaffected, which is what lets a custom prompt be plain text.
    """
    prompt = available.get(name)
    if prompt is None:
        raise UnknownPrompt(
            f"no prompt named '{name}'; there is "
            f"{NAME_SEPARATOR.join(repr(known) for known in sorted(available))}"
        )

    return prompt.replace(WORDS_PLACEHOLDER, str(words))
