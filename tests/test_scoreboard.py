"""What the board counts, when it writes it down, and when it puts it up."""

import asyncio
from dataclasses import replace

import pytest

from miss_quote.config import ServerConfig, ToolSettings, scoreboard_cfg
from miss_quote.ledger.credits import CreditLedger
from miss_quote.tools.base import ToolContext
from miss_quote.tools.runner import ToolRunner
from miss_quote.tools.scoreboard import Scoreboard
from miss_quote.transcript.writer import Source

SERVER_ID = 123456789012345678
SERVER = "first-server"
OTHER_SERVER = "second-server"

CHANNEL_ID = 987654321098765432
CHANNEL = "The Long Table"

ELI, ELI_ID = "Eli", 1
ERIK, ERIK_ID = "Erik", 2
STRANGER, STRANGER_ID = "Someone Discord Named", 3

ROSTER = {ELI_ID: ELI, ERIK_ID: ERIK}

LEDGER_NAME = "credits.json"
INTERVAL_SECONDS = 0.01
PATIENCE_SECONDS = 2.0

# The topic's own interval, as the deployment sets it, against a fixed clock.
TOPIC_SECONDS = 300.0
OFF = 0.0
NOW = 1_000.0

ONE_CREDIT = 1
TWO_CREDITS = 2
FIVE_CREDITS = 5

NOTHING = 0


class RecordingTopic:
    """Somewhere to publish that keeps the lines instead of sending them."""

    def __init__(self, accepting: bool = True) -> None:
        self.lines: list[tuple[str, str]] = []
        self.accepting = accepting

    async def publish(self, server: str, line: str) -> bool:
        if not self.accepting:
            return False

        self.lines.append((server, line))
        return True

    @property
    def published(self) -> list[str]:
        return [line for _, line in self.lines]


@pytest.fixture
def path(tmp_path):
    return tmp_path / LEDGER_NAME


@pytest.fixture
def ledger(monkeypatch, path) -> CreditLedger:
    """
    A ledger of its own per test, in place of the process-wide one.

    The tool asks for the shared ledger, and one reaching the real one would read
    whatever the machine running the tests happens to have at /credits.
    """
    ledger = CreditLedger(path)
    monkeypatch.setattr("miss_quote.tools.scoreboard.shared_ledger", lambda: ledger)

    return ledger


@pytest.fixture
def topic() -> RecordingTopic:
    return RecordingTopic()


@pytest.fixture(autouse=True)
def intervals(monkeypatch):
    """
    Intervals of this test's own, rather than the deployment's.

    The environment is read at import, so the settings object is replaced rather
    than the variable behind it.
    """
    _set_intervals(monkeypatch, INTERVAL_SECONDS, INTERVAL_SECONDS)


def _set_intervals(monkeypatch, save: float, publish: float) -> None:
    monkeypatch.setattr(
        "miss_quote.tools.scoreboard.scoreboard_cfg",
        replace(
            scoreboard_cfg,
            save_interval_seconds=save,
            topic_interval_seconds=publish,
        ),
    )


def _board(topic=None, server: str = SERVER, users=None) -> Scoreboard:
    return Scoreboard(
        ToolContext(
            server=server,
            users=ROSTER if users is None else users,
            topic=RecordingTopic() if topic is None else topic,
        )
    )


def _joined() -> Source:
    return Source(
        guild_id=SERVER_ID,
        guild_alias=SERVER,
        channel_id=CHANNEL_ID,
        channel=CHANNEL,
    )


# ── counting ──────────────────────────────────────


def test_a_debit_comes_off_a_balance(ledger):
    board = _board()

    assert board.debit(ELI_ID, ELI, TWO_CREDITS) == -TWO_CREDITS
    assert board.balance(ELI_ID) == -TWO_CREDITS


def test_a_credit_goes_back_on(ledger):
    board = _board()
    board.debit(ELI_ID, ELI, TWO_CREDITS)

    assert board.credit(ELI_ID, ELI, TWO_CREDITS) == NOTHING


def test_one_credit_is_what_a_caller_that_does_not_say_means(ledger):
    board = _board()
    board.debit(ELI_ID, ELI)

    assert board.balance(ELI_ID) == -ONE_CREDIT
    assert board.credit(ELI_ID, ELI) == NOTHING


def test_a_balance_starts_at_nothing(ledger):
    assert _board().balance(STRANGER_ID) == NOTHING


def test_a_debit_refreshes_the_name_it_arrived_with(ledger):
    """The board prints whatever it was last told to call somebody."""
    renamed = "Eli Under Another Nickname"
    board = _board()
    board.debit(ELI_ID, renamed, TWO_CREDITS)

    assert renamed in board.standings()


def test_one_servers_board_never_sees_anothers(ledger):
    """A server's tally is its own business, and so are its words."""
    _board().debit(ELI_ID, ELI, TWO_CREDITS)

    assert _board(server=OTHER_SERVER).balance(ELI_ID) == NOTHING


def test_the_roster_is_on_the_board_before_anybody_is_fined(ledger):
    """Which is both the point of a scoreboard and how you tell it is watching."""
    assert _board().standings() == f"{ELI}: 0 {ERIK}: 0"


def test_somebody_off_the_roster_is_counted_and_not_published(ledger):
    """A display name its owner can set to anything is not for a channel topic."""
    board = _board()
    board.debit(STRANGER_ID, STRANGER, FIVE_CREDITS)

    assert board.balance(STRANGER_ID) == -FIVE_CREDITS
    assert STRANGER not in board.standings()


# ── publishing ────────────────────────────────────


async def test_a_changed_tally_reaches_the_topic(ledger, topic):
    board = _board(topic)
    board.debit(ELI_ID, ELI, TWO_CREDITS)

    await board.publish()

    assert topic.lines == [(SERVER, f"{ELI}: -2 {ERIK}: 0")]


async def test_an_unchanged_tally_is_not_published_twice(ledger, topic):
    """A topic edit is rate limited; spending one to say the same thing is waste."""
    board = _board(topic)
    board.debit(ELI_ID, ELI, TWO_CREDITS)

    await board.publish()
    await board.publish()

    assert len(topic.published) == 1


async def test_a_further_change_is_published(ledger, topic):
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    await board.publish()

    board.debit(ERIK_ID, ERIK, ONE_CREDIT)
    await board.publish()

    assert topic.published == [f"{ELI}: -1 {ERIK}: 0", f"{ELI}: -1 {ERIK}: -1"]


async def test_several_changes_between_ticks_are_one_edit(ledger, topic):
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    board.debit(ERIK_ID, ERIK, ONE_CREDIT)

    await board.publish()

    assert topic.published == [f"{ELI}: -2 {ERIK}: -1"]


async def test_a_tally_the_topic_would_not_take_is_published_later(ledger, topic):
    """Otherwise it waits for the next fine to catch up."""
    topic.accepting = False
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    await board.publish()

    topic.accepting = True
    await board.publish()

    assert topic.published == [f"{ELI}: -1 {ERIK}: 0"]


async def test_putting_the_roster_on_the_board_is_itself_worth_publishing(ledger, topic):
    """A channel should say who is being watched before anybody has sworn."""
    await _board(topic).publish()

    assert topic.published == [f"{ELI}: 0 {ERIK}: 0"]


async def test_a_board_with_nobody_on_it_publishes_nothing(ledger, topic):
    await _board(topic, users={}).publish()

    assert topic.published == []


# ── joining a channel ─────────────────────────────


async def test_joining_a_channel_publishes_a_board_that_has_not_changed(ledger, topic):
    """
    The whole point of the moment.

    A revision says whether the board changed, not whether the channel now
    reading it has ever been shown one, so the same line goes up again.
    """
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    await board.publish()

    await board.handle_joined(_joined())

    assert topic.published == [f"{ELI}: -1 {ERIK}: 0"] * 2


async def test_joining_a_channel_with_nobody_on_the_board_publishes_nothing(
    ledger, topic
):
    """A blank status would wipe whatever somebody put there by hand."""
    await _board(topic, users={}).handle_joined(_joined())

    assert topic.published == []


async def test_a_board_the_joined_channel_would_not_take_is_published_later(
    ledger, topic
):
    """A join publishes through the same gate, so a refusal is still owed."""
    topic.accepting = False
    board = _board(topic)
    await board.handle_joined(_joined())

    topic.accepting = True
    await board.publish()

    assert topic.published == [f"{ELI}: 0 {ERIK}: 0"]


async def test_the_interval_does_not_republish_what_a_join_just_put_up(ledger, topic):
    """The join brings the watermark up, so the next tick finds nothing new."""
    board = _board(topic)
    await board.handle_joined(_joined())

    await board.publish()

    assert len(topic.published) == 1


# ── the topic's own interval ──────────────────────


def test_the_first_turn_comes_immediately(ledger, monkeypatch):
    """A restart should not sit on the tally for the length of an interval."""
    _set_intervals(monkeypatch, INTERVAL_SECONDS, TOPIC_SECONDS)

    assert _board()._topic_turn_has_come(now=NOW)


def test_a_turn_does_not_come_round_again_inside_the_interval(ledger, monkeypatch):
    """The rate limit is the reason the interval exists; ticking past it is waste."""
    _set_intervals(monkeypatch, INTERVAL_SECONDS, TOPIC_SECONDS)
    board = _board()
    board._topic_turn_has_come(now=NOW)

    assert not board._topic_turn_has_come(now=NOW + TOPIC_SECONDS - 1)


def test_a_turn_comes_round_once_the_interval_has_passed(ledger, monkeypatch):
    _set_intervals(monkeypatch, INTERVAL_SECONDS, TOPIC_SECONDS)
    board = _board()
    board._topic_turn_has_come(now=NOW)

    assert board._topic_turn_has_come(now=NOW + TOPIC_SECONDS)


def test_a_turn_never_comes_when_the_topic_is_switched_off(ledger, monkeypatch):
    _set_intervals(monkeypatch, INTERVAL_SECONDS, OFF)

    assert not _board()._topic_turn_has_come(now=NOW)


# ── the loop ──────────────────────────────────────


async def test_the_loop_publishes_and_saves(ledger, topic, path):
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    task = asyncio.create_task(board.run())

    async with asyncio.timeout(PATIENCE_SECONDS):
        while not topic.published or not path.is_file():
            await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert topic.published == [f"{ELI}: -1 {ERIK}: 0"]


async def test_a_tally_is_still_saved_with_the_topic_switched_off(
    ledger, topic, path, monkeypatch
):
    _set_intervals(monkeypatch, INTERVAL_SECONDS, OFF)
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    task = asyncio.create_task(board.run())

    async with asyncio.timeout(PATIENCE_SECONDS):
        while not path.is_file():
            await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert topic.published == []


async def test_the_loop_does_not_run_with_saving_switched_off(
    ledger, topic, path, monkeypatch, caplog
):
    """Nothing to wake for; the tally is still counted and still written at close."""
    _set_intervals(monkeypatch, OFF, INTERVAL_SECONDS)
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)

    with caplog.at_level("INFO"):
        await board.run()  # Returning at all is the test.

    assert not path.exists()
    assert any(
        "settings.credits.save_seconds" in record.message for record in caplog.records
    )


async def test_a_failing_tick_does_not_stop_the_loop(ledger, topic, caplog):
    board = _board(topic)
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    failures = []

    async def once() -> None:
        failures.append(True)
        raise RuntimeError("the ledger is on fire")

    board._ledger.flush = once
    task = asyncio.create_task(board.run())

    with caplog.at_level("ERROR"):
        async with asyncio.timeout(PATIENCE_SECONDS):
            while len(failures) < 2:
                await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert any("on fire" in record.message for record in caplog.records)


async def test_the_loop_stops_when_it_is_cancelled(ledger, topic):
    task = asyncio.create_task(_board(topic).run())
    await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── persisting ────────────────────────────────────


async def test_closing_writes_the_tally(ledger, path):
    """The tool's own task is cancelled by then; the file is what is left."""
    board = _board()
    board.debit(ELI_ID, ELI, FIVE_CREDITS)

    await board.close()

    assert CreditLedger(path).total(SERVER, ELI_ID) == -FIVE_CREDITS


async def test_two_boards_write_one_file_once(ledger, path):
    """
    One tally on disk, so the mark for whether it has changed belongs to it.

    Two boards flushing a moment apart over one change would otherwise rewrite
    the whole thing twice.
    """
    first = _board()
    second = _board(server=OTHER_SERVER)
    first.debit(ELI_ID, ELI, ONE_CREDIT)

    await first.close()
    written = path.stat().st_mtime_ns
    await second.close()

    assert path.stat().st_mtime_ns == written


async def test_a_change_landing_after_a_write_is_still_written(ledger, path):
    """The revision is read before the write, so the next flush picks it up."""
    board = _board()
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    await board.close()

    board.debit(ERIK_ID, ERIK, ONE_CREDIT)
    await board.close()

    assert CreditLedger(path).total(SERVER, ERIK_ID) == -ONE_CREDIT


async def test_nothing_is_written_for_a_tally_nobody_has_touched(ledger, path):
    """Not even a file: a board with nobody on it has nothing to say about anyone."""
    await _board(users={}).close()

    assert not path.exists()


async def test_an_unchanged_tally_is_not_written_again(ledger, path):
    board = _board()
    board.debit(ELI_ID, ELI, ONE_CREDIT)
    await board.close()
    written = path.stat().st_mtime_ns

    await board.close()

    assert path.stat().st_mtime_ns == written


# ── the runner ────────────────────────────────────


def _servers() -> dict[int, ServerConfig]:
    return {
        SERVER_ID: ServerConfig(
            alias=SERVER,
            users=ROSTER,
            tools={Scoreboard.name: ToolSettings(enabled=True, config={})},
        )
    }


def test_a_board_runs_and_watches_for_a_join_rather_than_listening(ledger):
    """It hears nothing and says nothing, which is not the same as being inert."""
    runner = ToolRunner(_servers(), {Scoreboard.name: Scoreboard})

    assert runner.describe() == {SERVER: (Scoreboard.name,)}
    assert runner.problems == []
    assert [type(tool) for tool in runner._serving] == [Scoreboard]
    assert [type(tool) for tool in runner._on_joined[SERVER_ID]] == [Scoreboard]
    assert runner._on_utterance == {}


async def test_the_runner_puts_a_board_up_on_the_channel_it_joined(ledger, topic):
    runner = ToolRunner(_servers(), {Scoreboard.name: Scoreboard}, topic=topic)

    await runner.dispatch_joined(_joined())

    assert topic.published == [f"{ELI}: 0 {ERIK}: 0"]


async def test_the_runner_closes_a_board(ledger, path):
    runner = ToolRunner(_servers(), {Scoreboard.name: Scoreboard})
    runner._serving[0].debit(ELI_ID, ELI, ONE_CREDIT)

    await runner.close()

    assert CreditLedger(path).total(SERVER, ELI_ID) == -ONE_CREDIT
