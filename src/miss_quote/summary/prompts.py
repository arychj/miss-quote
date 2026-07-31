"""
What the model is told to do with a transcript, and with a summary.

A closed set shipped with the image, which a server adds to under `prompts:` and
selects from by name. Named rather than written inline at the point of use so
that the two places a prompt is chosen — the summary and the retelling — are one
word each in the config file, and so a server that wants a different wording
writes it once and uses it in both.

Where a prompt's output **goes** is the thing that decides how it is written, and
the difference is not cosmetic:

- `recap` and `minutes` are read, in a Discord message. Markdown renders there,
  so they are free to use it.
- `bard` is **spoken**, by a synthesizer, which reads an asterisk out as a word
  and a bullet as nothing at all. It says so at some length for that reason.

`bard` also spends several lines establishing that the narrator was not there,
which is not padding. Told only to retell a conversation warmly to the people
who were in it, a model reasonably concludes it was one of them and writes
"Ryan and I decided" — a bot claiming to have been in the room, out loud, in
that room. Third person has to be asked for explicitly.

Every prompt states the shape of what it is given, because the script it
receives has no timestamps in it: the order is the order things were said, and
nothing in the text says so on its own.
"""

from __future__ import annotations

from collections.abc import Mapping

NAME_SEPARATOR = ", "

# What a script looks like, said once and shared by every prompt that reads one.
# The format is `dialogue.script`'s, and the two have to agree: a prompt that
# describes a field the script does not carry is a prompt inventing one.
SCRIPT_SHAPE = (
    "You are given a transcript of one voice conversation. Each line is one "
    "speaker and what they said, as 'Name: what they said'. The lines are in "
    "the order they were spoken; there are no timestamps. The transcript comes "
    "from automatic speech recognition, so expect mishearings, missing "
    "punctuation, and the occasional word that makes no sense — read through "
    "them rather than quoting them as though they were said."
)

RECAP = f"""{SCRIPT_SHAPE}

Write an account of what happened, for the people who were there. Follow the
conversation in the order it happened. Name people. Say what was decided, what
was argued about, what was funny, and what was left unresolved. Quote a line
directly when the wording is the point.

Do not editorialise about the participants and do not invent anything that is
not in the transcript. If very little happened, say so briefly rather than
padding it out. A few short paragraphs is the right length; Markdown is fine.
"""

MINUTES = f"""{SCRIPT_SHAPE}

Write the minutes of this conversation. Use three headed sections: Topics,
Decisions, and Open questions. Under each, use short bullet points. Attribute
decisions and open questions to the person who made or raised them.

State only what the transcript supports. Leave a section out entirely if the
conversation contained nothing for it. No preamble and no closing remarks —
begin at the first heading. Markdown is fine.
"""

BARD = """You are given a written summary of a conversation that happened
earlier. Retell it as a story, the way a narrator opens the next episode:
"Last time, our adventurers..."

You are the storyteller and you were not there. Write about the people in the
summary in the **third person** — they are "they", and each of them is
whichever name the summary gives them. Never write "I", "me", "my", or "we",
"us", "our" about anything that happened, and never take the side of anybody
in it. Calling them "our adventurers" or "our heroes" is a narrator's flourish
and is welcome; being one of them is not.

The people listening are the ones it happened to. You are telling them their
own story back, so tell it about them rather than as one of them.

Your reply will be read aloud by a speech synthesizer, which reads every
character it is given. That rules out Markdown of every kind: no asterisks, no
underscores, no hash marks, no bullet points, no numbered lists, no headings,
and no emoji. Write full sentences and ordinary paragraphs, the way somebody
telling a story out loud would.

Be warm and a little wry — this is a good evening being recounted, not a report
being filed. Keep the names. Do not add events that are not in the summary, and
do not explain that you are summarizing; just tell it.

Keep it under {words} words.
"""

RECAP_PROMPT = "recap"
MINUTES_PROMPT = "minutes"
BARD_PROMPT = "bard"

# What a server gets without saying anything: an account of the session in the
# channel, and a spoken retelling of that account when somebody asks for one.
DEFAULT_SUMMARY_PROMPT = RECAP_PROMPT
DEFAULT_RETELLING_PROMPT = BARD_PROMPT

BUILTIN: Mapping[str, str] = {
    RECAP_PROMPT: RECAP,
    MINUTES_PROMPT: MINUTES,
    BARD_PROMPT: BARD,
}

# The one thing a prompt can interpolate, filled from a channel's
# `retelling_words`. Substituted rather than formatted, so a custom prompt is
# free to contain braces — an example of the JSON somebody wants back, say —
# without the substitution turning them into a placeholder it cannot fill.
WORDS_PLACEHOLDER = "{words}"


class UnknownPrompt(LookupError):
    """A prompt was asked for by a name nothing answers to."""


def library(extra: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """
    The prompts one server can choose from.

    Custom prompts are laid over the built-ins rather than replacing them, so a
    server that wants one extra wording writes one block instead of restating
    the shipped set. A custom prompt under a shipped name replaces it, which is
    how a server that likes the structure of `recap` and not its tone changes
    the tone without inventing a name for it.
    """
    return {**BUILTIN, **(extra or {})}


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
