from datetime import datetime, timezone

from miss_quote.summary.dialogue import script
from miss_quote.transcript.writer import Utterance

MOMENT = datetime(2026, 7, 26, 21, 14, 3, tzinfo=timezone.utc)

FIRST_SPEAKER = "Erik"
SECOND_SPEAKER = "Eli"


def _said(user: str, text: str, user_id: int = 1) -> Utterance:
    return Utterance(timestamp=MOMENT, user_id=user_id, user=user, text=text)


def test_one_line_per_speaker_turn():
    assert script(
        [
            _said(FIRST_SPEAKER, "that should work"),
            _said(SECOND_SPEAKER, "it did not"),
        ]
    ) == f"{FIRST_SPEAKER}: that should work\n{SECOND_SPEAKER}: it did not"


def test_consecutive_lines_from_one_speaker_are_joined():
    """The segmenter cuts on a pause, so one thought arrives as several lines."""
    assert script(
        [
            _said(FIRST_SPEAKER, "so what I was thinking"),
            _said(FIRST_SPEAKER, "is that we move the whole thing"),
            _said(SECOND_SPEAKER, "no"),
        ]
    ) == (
        f"{FIRST_SPEAKER}: so what I was thinking is that we move the whole thing\n"
        f"{SECOND_SPEAKER}: no"
    )


def test_a_speaker_taking_a_second_turn_gets_a_second_line():
    assert script(
        [
            _said(FIRST_SPEAKER, "one"),
            _said(SECOND_SPEAKER, "two"),
            _said(FIRST_SPEAKER, "three"),
        ]
    ) == f"{FIRST_SPEAKER}: one\n{SECOND_SPEAKER}: two\n{FIRST_SPEAKER}: three"


def test_timestamps_and_ids_are_left_out():
    """The prompt is told the order is chronological; a stamp per line adds nothing."""
    rendered = script([_said(FIRST_SPEAKER, "hello", user_id=1234567890)])

    assert "1234567890" not in rendered
    assert str(MOMENT.year) not in rendered


def test_empty_transcriptions_are_dropped():
    """
    A bare 'Name:' in the middle of a script reads as somebody being cut off.

    Dropped rather than emitted, and dropped before it counts as a turn: the
    speaker either side of it was not interrupted by somebody saying nothing.
    """
    assert script(
        [
            _said(FIRST_SPEAKER, "something"),
            _said(SECOND_SPEAKER, "   "),
            _said(FIRST_SPEAKER, "else"),
        ]
    ) == f"{FIRST_SPEAKER}: something else"


def test_an_empty_transcript_is_an_empty_script():
    assert script([]) == ""
