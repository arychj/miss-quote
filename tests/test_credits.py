"""What everybody owes: counting it, rendering it, and keeping it."""

import json
import re

import pytest

from ledger.credits import (
    CREDITS_FIELD,
    ENTRY_SEPARATOR,
    NAME_FIELD,
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

LEDGER_NAME = "credits.json"

# Enough people, with long enough names, to overrun a channel topic. Hyphenated
# rather than spaced so a test can tell an entry boundary from a name.
CROWD = 200
LONG_NAME = "A-Speaker-With-A-Rather-Long-Display-Name"


def _crowd() -> dict[int, str]:
    return {number: f"{LONG_NAME}-{number}" for number in range(CROWD)}


@pytest.fixture
def path(tmp_path):
    return tmp_path / LEDGER_NAME


@pytest.fixture
def ledger(path) -> CreditLedger:
    return CreditLedger(path)


# ── the tally ─────────────────────────────────────


def test_a_fresh_ledger_owes_nothing(ledger):
    assert ledger.total(SERVER, ELI_ID) == 0


def test_an_award_is_added_to_a_total(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(SERVER, ELI_ID) == 2


def test_awards_accumulate(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 2)
    ledger.award(SERVER, ELI_ID, ELI, 3)

    assert ledger.total(SERVER, ELI_ID) == 5


def test_an_award_reports_the_new_total(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 2)

    assert ledger.award(SERVER, ELI_ID, ELI, 1) == 3


def test_a_total_is_per_server(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(OTHER_SERVER, ELI_ID) == 0


def test_a_total_is_per_person(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 2)

    assert ledger.total(SERVER, ERIK_ID) == 0


def test_a_renamed_speaker_keeps_their_debt(ledger):
    """Identity is the ID; the name is only what gets printed."""
    ledger.award(SERVER, ELI_ID, ELI, 2)

    ledger.award(SERVER, ELI_ID, "Elijah", 1)

    assert ledger.topic(SERVER) == "Elijah: 3"


# ── enrolment ─────────────────────────────────────


def test_a_roster_starts_on_the_board_at_nothing_owed(ledger):
    ledger.enroll(SERVER, ROSTER)

    assert ledger.topic(SERVER) == f"{ELI}: 0 {ERIK}: 0 {LUKE}: 0 {RYAN}: 0"


def test_enrolling_does_not_reset_a_total(ledger):
    """It runs at every startup, and a restart is not an amnesty."""
    ledger.award(SERVER, ELI_ID, ELI, 4)

    ledger.enroll(SERVER, ROSTER)

    assert ledger.total(SERVER, ELI_ID) == 4


def test_enrolling_picks_up_a_roster_rename(ledger):
    ledger.enroll(SERVER, {ELI_ID: ELI})

    ledger.enroll(SERVER, {ELI_ID: "Elijah"})

    assert ledger.topic(SERVER) == "Elijah: 0"


def test_enrolling_the_same_roster_twice_changes_nothing(ledger):
    ledger.enroll(SERVER, ROSTER)
    revision = ledger.revision

    ledger.enroll(SERVER, ROSTER)

    assert ledger.revision == revision


# ── the topic ─────────────────────────────────────


def test_the_topic_is_ordered_by_name(ledger):
    """A leaderboard would rearrange itself every time somebody passed somebody."""
    ledger.award(SERVER, RYAN_ID, RYAN, 9)
    ledger.award(SERVER, ELI_ID, ELI, 1)

    assert ledger.topic(SERVER) == f"{ELI}: 1 {RYAN}: 9"


def test_the_topic_of_a_server_nobody_has_enrolled_is_empty(ledger):
    assert ledger.topic(SERVER) == ""


def test_the_topic_holds_only_its_own_server(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 1)
    ledger.award(OTHER_SERVER, ERIK_ID, ERIK, 1)

    assert ledger.topic(SERVER) == f"{ELI}: 1"


def test_a_topic_too_long_for_discord_is_cut(ledger):
    ledger.enroll(SERVER, _crowd())

    topic = ledger.topic(SERVER)

    assert len(topic) <= TOPIC_LIMIT
    assert topic.endswith(TOPIC_TRUNCATED)


def test_a_cut_topic_ends_on_a_whole_entry(ledger):
    """Cut mid-number, it would read as a total somebody does not owe."""
    ledger.enroll(SERVER, _crowd())

    kept = ledger.topic(SERVER).removesuffix(TOPIC_TRUNCATED)

    assert re.fullmatch(rf"({LONG_NAME}-\d+: 0{ENTRY_SEPARATOR})*{LONG_NAME}-\d+: 0", kept)


# ── revisions ─────────────────────────────────────


def test_nothing_has_happened_to_a_fresh_ledger(ledger):
    assert ledger.revision == UNWRITTEN
    assert ledger.revision_for(SERVER) == UNWRITTEN


def test_an_award_bumps_the_revision(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 1)

    assert ledger.revision > UNWRITTEN


def test_a_revision_belongs_to_the_server_that_changed(ledger):
    ledger.award(SERVER, ELI_ID, ELI, 1)
    revision = ledger.revision_for(SERVER)

    ledger.award(OTHER_SERVER, ELI_ID, ELI, 1)

    assert ledger.revision_for(SERVER) == revision
    assert ledger.revision_for(OTHER_SERVER) > revision


# ── disk ──────────────────────────────────────────


def test_a_saved_tally_is_loaded_again(ledger, path):
    ledger.award(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert CreditLedger(path).total(SERVER, ELI_ID) == 3


def test_a_loaded_tally_still_knows_what_to_call_somebody(ledger, path):
    """The roster is not enrolled until a tool is built; the topic is not empty until then."""
    ledger.award(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert CreditLedger(path).topic(SERVER) == f"{ELI}: 3"


def test_saving_creates_the_directory(tmp_path):
    path = tmp_path / "nested" / LEDGER_NAME
    ledger = CreditLedger(path)

    ledger.award(SERVER, ELI_ID, ELI, 1)
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


def test_one_unreadable_entry_costs_one_total(path, caplog):
    """The rest of the server is still owed what it is owed."""
    path.write_text(
        json.dumps(
            {
                SERVER: {
                    str(ELI_ID): {NAME_FIELD: ELI, CREDITS_FIELD: "not a number"},
                    str(ERIK_ID): {NAME_FIELD: ERIK, CREDITS_FIELD: 7},
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("ERROR"):
        ledger = CreditLedger(path)

    assert ledger.total(SERVER, ERIK_ID) == 7
    assert ledger.total(SERVER, ELI_ID) == 0
    assert any("Ignoring" in record.message for record in caplog.records)


def test_an_unwritable_path_costs_the_persistence_not_the_counting(tmp_path, caplog):
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")
    ledger = CreditLedger(blocked / LEDGER_NAME)

    ledger.award(SERVER, ELI_ID, ELI, 1)
    with caplog.at_level("ERROR"):
        ledger.save()

    assert ledger.total(SERVER, ELI_ID) == 1
    assert any("Could not write" in record.message for record in caplog.records)


def test_a_save_leaves_no_partial_file_behind(ledger, path):
    ledger.award(SERVER, ELI_ID, ELI, 1)
    ledger.save()

    assert [found.name for found in path.parent.iterdir()] == [LEDGER_NAME]


def test_the_file_is_readable_by_a_human(ledger, path):
    """It is a tally of imaginary money; somebody will want to edit it by hand."""
    ledger.award(SERVER, ELI_ID, ELI, 3)
    ledger.save()

    assert json.loads(path.read_text(encoding="utf-8")) == {
        SERVER: {str(ELI_ID): {NAME_FIELD: ELI, CREDITS_FIELD: 3}}
    }
