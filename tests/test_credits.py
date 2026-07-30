"""What everybody has left: counting it, rendering it, and keeping it."""

import json
import re

import pytest

from miss_quote.ledger.credits import (
    CREDITS_FIELD,
    ENTRY_SEPARATOR,
    NAME_FIELD,
    SCOREBOARD_PLACES,
    TOPIC_LIMIT,
    TOPIC_TRUNCATED,
    UNWRITTEN,
    CreditLedger,
)

SERVER = "first-server"
OTHER_SERVER = "second-server"

ELI, ELI_ID = "Eli", 1
ERIK, ERIK_ID = "Erik", 2
LUKE, LUKE_ID = "Luke", 3
RYAN, RYAN_ID = "Ryan", 4

ROSTER = {ELI_ID: ELI, ERIK_ID: ERIK, LUKE_ID: LUKE, RYAN_ID: RYAN}

# Somebody the server never wrote down, known only by what Discord reports.
STRANGER, STRANGER_ID = "Someone Else", 5

LEDGER_NAME = "credits.json"

# More people than the board has places, so a test can tell which of them it
# picked. Hyphenated rather than spaced so a boundary is distinguishable from a
# name.
CONTENDERS = SCOREBOARD_PLACES + 2
NAME = "Speaker"

# Long enough that four entries overrun a channel topic on their own, which four
# names anybody would actually type never would.
SHOUTED_NAME = "A-Speaker-With-A-Rather-Long-Display-Name" * 8


def _contenders() -> dict[int, str]:
    return {number: f"{NAME}-{number}" for number in range(CONTENDERS)}


def _shouting() -> dict[int, str]:
    return {number: f"{SHOUTED_NAME}-{number}" for number in range(CONTENDERS)}


@pytest.fixture
def path(tmp_path):
    return tmp_path / LEDGER_NAME


@pytest.fixture
def ledger(path) -> CreditLedger:
    return CreditLedger(path)


# ── the tally ─────────────────────────────────────


def test_a_fresh_ledger_has_spent_nothing(ledger):
    assert ledger.total(SERVER, ELI_ID) == 0


def test_a_fine_comes_off_a_balance(ledger):
    """A fine is a debit: what it says is what swearing has cost somebody."""
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(SERVER, ELI_ID) == -2


def test_fines_accumulate(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 2)
    ledger.fine(SERVER, ELI_ID, ELI, 3)

    assert ledger.total(SERVER, ELI_ID) == -5


def test_a_fine_reports_the_new_balance(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    assert ledger.fine(SERVER, ELI_ID, ELI, 1) == -3


def test_a_balance_is_per_server(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(OTHER_SERVER, ELI_ID) == 0


def test_a_balance_is_per_person(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(SERVER, ERIK_ID) == 0


def test_a_renamed_speaker_keeps_their_debt(ledger):
    """Identity is the ID; the name is only what gets printed."""
    ledger.enroll(SERVER, {ELI_ID: ELI})
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    ledger.fine(SERVER, ELI_ID, "Elijah", 1)

    assert ledger.topic(SERVER) == "Elijah: -3"


# ── enrolment ─────────────────────────────────────


def test_a_roster_starts_on_the_board_at_nothing_spent(ledger):
    ledger.enroll(SERVER, ROSTER)

    assert ledger.topic(SERVER) == f"{ELI}: 0 {ERIK}: 0 {LUKE}: 0 {RYAN}: 0"


def test_enrolling_does_not_reset_a_balance(ledger):
    """It runs at every startup, and a restart is not an amnesty."""
    ledger.fine(SERVER, ELI_ID, ELI, 4)

    ledger.enroll(SERVER, ROSTER)

    assert ledger.total(SERVER, ELI_ID) == -4


def test_enrolling_picks_up_a_roster_rename(ledger):
    ledger.enroll(SERVER, {ELI_ID: ELI})

    ledger.enroll(SERVER, {ELI_ID: "Elijah"})

    assert ledger.topic(SERVER) == "Elijah: 0"


def test_enrolling_the_same_roster_twice_changes_nothing(ledger):
    ledger.enroll(SERVER, ROSTER)
    revision = ledger.revision

    ledger.enroll(SERVER, ROSTER)

    assert ledger.revision == revision


# ── the board ─────────────────────────────────────


def test_the_board_is_ordered_worst_first(ledger):
    """It is a leaderboard now, and who is winning is what a reader wants."""
    ledger.enroll(SERVER, ROSTER)
    ledger.fine(SERVER, ELI_ID, ELI, 1)
    ledger.fine(SERVER, RYAN_ID, RYAN, 9)

    assert ledger.topic(SERVER) == f"{RYAN}: -9 {ELI}: -1 {ERIK}: 0 {LUKE}: 0"


def test_the_board_holds_four_places(ledger):
    ledger.enroll(SERVER, _contenders())
    for user_id in _contenders():
        ledger.fine(SERVER, user_id, f"{NAME}-{user_id}", user_id)

    entries = re.findall(rf"{NAME}-\d+: -\d+", ledger.topic(SERVER))

    assert len(entries) == SCOREBOARD_PLACES


def test_the_board_holds_the_worst_of_them(ledger):
    ledger.enroll(SERVER, _contenders())
    for user_id in _contenders():
        ledger.fine(SERVER, user_id, f"{NAME}-{user_id}", user_id)

    deepest = sorted(_contenders(), reverse=True)[:SCOREBOARD_PLACES]
    assert ledger.topic(SERVER) == ENTRY_SEPARATOR.join(
        f"{NAME}-{user_id}: -{user_id}" for user_id in deepest
    )


def test_a_tie_breaks_on_the_name(ledger):
    """So two people on one balance do not swap places between edits for nothing."""
    ledger.enroll(SERVER, {ERIK_ID: ERIK, ELI_ID: ELI})
    ledger.fine(SERVER, ERIK_ID, ERIK, 3)
    ledger.fine(SERVER, ELI_ID, ELI, 3)

    assert ledger.topic(SERVER) == f"{ELI}: -3 {ERIK}: -3"


def test_somebody_off_the_roster_is_not_on_the_board(ledger):
    """A nickname its owner can set to anything is not going in a channel topic."""
    ledger.enroll(SERVER, {ELI_ID: ELI})

    ledger.fine(SERVER, STRANGER_ID, STRANGER, 9)

    assert ledger.topic(SERVER) == f"{ELI}: 0"


def test_somebody_off_the_roster_is_still_fined(ledger):
    """Ineligible for the board is not the same as unwatched."""
    ledger.enroll(SERVER, {ELI_ID: ELI})

    ledger.fine(SERVER, STRANGER_ID, STRANGER, 9)

    assert ledger.total(SERVER, STRANGER_ID) == -9


def test_somebody_added_to_the_roster_joins_the_board(ledger):
    ledger.fine(SERVER, STRANGER_ID, STRANGER, 2)

    ledger.enroll(SERVER, {STRANGER_ID: STRANGER})

    assert ledger.topic(SERVER) == f"{STRANGER}: -2"


def test_the_board_of_a_server_nobody_has_enrolled_is_empty(ledger):
    assert ledger.topic(SERVER) == ""


def test_the_board_holds_only_its_own_server(ledger):
    ledger.enroll(SERVER, {ELI_ID: ELI})
    ledger.enroll(OTHER_SERVER, {ERIK_ID: ERIK})
    ledger.fine(SERVER, ELI_ID, ELI, 1)
    ledger.fine(OTHER_SERVER, ERIK_ID, ERIK, 1)

    assert ledger.topic(SERVER) == f"{ELI}: -1"


def test_a_board_too_long_for_discord_is_cut(ledger):
    ledger.enroll(SERVER, _shouting())

    topic = ledger.topic(SERVER)

    assert len(topic) <= TOPIC_LIMIT
    assert topic.endswith(TOPIC_TRUNCATED)


def test_a_cut_board_ends_on_a_whole_entry(ledger):
    """Cut mid-number, it would read as a balance somebody does not have."""
    ledger.enroll(SERVER, _shouting())

    kept = ledger.topic(SERVER).removesuffix(TOPIC_TRUNCATED)

    assert re.fullmatch(
        rf"({SHOUTED_NAME}-\d+: 0{ENTRY_SEPARATOR})*{SHOUTED_NAME}-\d+: 0", kept
    )


# ── revisions ─────────────────────────────────────


def test_nothing_has_happened_to_a_fresh_ledger(ledger):
    assert ledger.revision == UNWRITTEN
    assert ledger.revision_for(SERVER) == UNWRITTEN


def test_a_fine_bumps_the_revision(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 1)

    assert ledger.revision > UNWRITTEN


def test_a_revision_belongs_to_the_server_that_changed(ledger):
    ledger.fine(SERVER, ELI_ID, ELI, 1)
    revision = ledger.revision_for(SERVER)

    ledger.fine(OTHER_SERVER, ELI_ID, ELI, 1)

    assert ledger.revision_for(SERVER) == revision
    assert ledger.revision_for(OTHER_SERVER) > revision


# ── disk ──────────────────────────────────────────


def test_a_saved_tally_is_loaded_again(ledger, path):
    ledger.fine(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert CreditLedger(path).total(SERVER, ELI_ID) == -3


def test_a_loaded_tally_is_not_on_the_board_until_a_roster_is_enrolled(ledger, path):
    """The roster says who may be shown, and no tool has been built to supply one."""
    ledger.enroll(SERVER, {ELI_ID: ELI})
    ledger.fine(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert CreditLedger(path).topic(SERVER) == ""


def test_enrolling_puts_a_loaded_balance_back_on_the_board(ledger, path):
    ledger.enroll(SERVER, {ELI_ID: ELI})
    ledger.fine(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    reloaded = CreditLedger(path)
    reloaded.enroll(SERVER, {ELI_ID: ELI})

    assert reloaded.topic(SERVER) == f"{ELI}: -3"


def test_saving_creates_the_directory(tmp_path):
    path = tmp_path / "nested" / LEDGER_NAME
    ledger = CreditLedger(path)

    ledger.fine(SERVER, ELI_ID, ELI, 1)
    ledger.save()

    assert path.is_file()


def test_a_missing_ledger_is_not_an_error(tmp_path, caplog):
    with caplog.at_level("ERROR"):
        ledger = CreditLedger(tmp_path / "nothing-here.json")

    assert ledger.servers() == ()
    assert caplog.records == []


def test_an_unparseable_ledger_is_reported_and_ignored(path, caplog):
    path.write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level("ERROR"):
        ledger = CreditLedger(path)

    assert ledger.servers() == ()
    assert any("Could not read" in record.message for record in caplog.records)


def test_one_unreadable_entry_costs_one_balance(path, caplog):
    """The rest of the server still stands where it stands."""
    path.write_text(
        json.dumps(
            {
                SERVER: {
                    str(ELI_ID): {NAME_FIELD: ELI, CREDITS_FIELD: "not a number"},
                    str(ERIK_ID): {NAME_FIELD: ERIK, CREDITS_FIELD: -7},
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("ERROR"):
        ledger = CreditLedger(path)

    assert ledger.total(SERVER, ERIK_ID) == -7
    assert ledger.total(SERVER, ELI_ID) == 0
    assert any("Ignoring" in record.message for record in caplog.records)


def test_an_unwritable_path_costs_the_persistence_not_the_counting(tmp_path, caplog):
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")
    ledger = CreditLedger(blocked / LEDGER_NAME)

    ledger.fine(SERVER, ELI_ID, ELI, 1)
    with caplog.at_level("ERROR"):
        ledger.save()

    assert ledger.total(SERVER, ELI_ID) == -1
    assert any("Could not write" in record.message for record in caplog.records)


def test_a_save_leaves_no_partial_file_behind(ledger, path):
    ledger.fine(SERVER, ELI_ID, ELI, 1)
    ledger.save()

    assert [found.name for found in path.parent.iterdir()] == [LEDGER_NAME]


def test_the_file_is_readable_by_a_human(ledger, path):
    """It is a tally of imaginary money; somebody will want to edit it by hand."""
    ledger.fine(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert json.loads(path.read_text(encoding="utf-8")) == {
        SERVER: {str(ELI_ID): {NAME_FIELD: ELI, CREDITS_FIELD: -3}}
    }
