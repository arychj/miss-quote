"""
A transcript as the text a model reads.

The file on disk carries four fields per line and a summarizer wants two of
them. `user_id` is there because display names change and the path does not
encode the speaker; a model has no use for a number it cannot look anybody up
by. The timestamp goes for a subtler reason: the lines are already in the order
they were spoken, and every prompt says so, which makes a stamp on each one a
token per line spent restating the one thing the shape of the input already
guarantees.

What comes out is `Name: what they said`, which every chat model has read more
of than any other transcript format.

Consecutive lines from one speaker are joined. The segmenter cuts on a pause
rather than on a sentence, so somebody thinking out loud arrives as three lines
that were one thought, and three attributions in a row reads as an exchange that
never happened.
"""

from __future__ import annotations

from collections.abc import Sequence

from miss_quote.transcript.writer import Utterance

SPEAKER_SEPARATOR = ": "
LINE_SEPARATOR = "\n"
CONTINUATION_SEPARATOR = " "


def script(utterances: Sequence[Utterance]) -> str:
    """
    One conversation, as the model is given it.

    Empty transcriptions are dropped rather than emitted as a speaker who said
    nothing. They should not be here — an empty transcription is not dispatched
    and not written — but a hand-edited file is a file, and a bare `Name:` in
    the middle of a script reads as somebody being cut off.
    """
    lines: list[list[str]] = []
    speaking: str | None = None

    for utterance in utterances:
        said = utterance.text.strip()
        if not said:
            continue

        if utterance.user == speaking and lines:
            lines[-1].append(said)
            continue

        lines.append([f"{utterance.user}{SPEAKER_SEPARATOR}{said}"])
        speaking = utterance.user

    return LINE_SEPARATOR.join(
        CONTINUATION_SEPARATOR.join(pieces) for pieces in lines
    )
