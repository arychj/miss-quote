"""Getting the tally onto disk and into the channel topic."""

import asyncio
from pathlib import Path

import discord
import pytest

import miss_quote.bot.client as client_module
import miss_quote.bot.scoreboard as scoreboard_module
from miss_quote.bot.scoreboard import Scoreboard
from miss_quote.config import FileConfig, ServerConfig
from miss_quote.ledger.credits import CreditLedger

SERVER_ID = 123456789012345678
SERVER = "first-server"
UNCONFIGURED_SERVER = "nobody-configured-this"

ELI, ELI_ID = "Eli", 1
ERIK, ERIK_ID = "Erik", 2

ROSTER = {ELI_ID: ELI, ERIK_ID: ERIK}

LEDGER_NAME = "credits.json"
INTERVAL_SECONDS = 0.01
PATIENCE_SECONDS = 2.0

# The topic's own interval, as the deployment sets it, against a fixed clock.
TOPIC_SECONDS = 300.0
NO_TOPIC = 0.0
NOW = 1_000.0

FORBIDDEN_STATUS = 403
REJECTED_STATUS = 400
SERVER_ERROR_STATUS = 500


class FakeChannel:
    """
    A voice channel that keeps what it was asked to change.

    The whole edit is kept rather than just the value, because which field is
    written is the thing most worth guarding: a voice channel has no topic, and
    Discord refuses one with an error that reads like a profanity filter.
    """

    def __init__(self, failure: Exception | None = None) -> None:
        self.edits: list[dict] = []
        self.failure = failure
        self.name = "general-voice"

    async def edit(self, **fields) -> None:
        if self.failure is not None:
            raise self.failure

        self.edits.append(fields)

    @property
    def statuses(self) -> list[str]:
        return [edit["status"] for edit in self.edits]

    def __str__(self) -> str:
        return self.name


class FakeVoiceClient:
    def __init__(self, channel: FakeChannel, connected: bool = True) -> None:
        self.channel = channel
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class FakeGuild:
    def __init__(self, voice_client) -> None:
        self.voice_client = voice_client


def _response(status: int) -> discord.HTTPException:
    """A failure shaped the way discord.py raises one."""
    return discord.HTTPException(
        type("Response", (), {"status": status, "reason": "because"})(), "no"
    )


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """One server in the mounted file, so an alias resolves back to an ID."""
    monkeypatch.setattr(
        scoreboard_module,
        "file_cfg",
        FileConfig(
            path=Path("/config/config.yaml"),
            servers={
                SERVER_ID: ServerConfig(alias=SERVER, users={ELI_ID: ELI}, tools={})
            },
            problems=(),
            found=True,
        ),
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / LEDGER_NAME


@pytest.fixture
def ledger(path) -> CreditLedger:
    return CreditLedger(path)


@pytest.fixture
def board(ledger) -> CreditLedger:
    """
    A ledger with the server's roster enrolled, as a built tool leaves one.

    Only the roster may appear in a topic, so a test about what reaches the
    channel has to have one; a test about what reaches the disk does not.
    """
    ledger.enroll(SERVER, ROSTER)

    return ledger


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


def _scoreboard(
    ledger, channel=None, connected: bool = True, topic_seconds: float = INTERVAL_SECONDS
) -> Scoreboard:
    voice_client = None if channel is None else FakeVoiceClient(channel, connected)

    return Scoreboard(
        ledger,
        lambda server_id: FakeGuild(voice_client),
        INTERVAL_SECONDS,
        topic_seconds,
    )


# ── publishing ────────────────────────────────────


async def test_a_changed_tally_reaches_the_channel_topic(board, channel):
    board.fine(SERVER, ELI_ID, ELI, 2)

    await _scoreboard(board, channel).publish()

    assert channel.statuses == [f"{ELI}: -2 {ERIK}: 0"]


async def test_an_unchanged_tally_is_not_published_twice(board, channel):
    """A topic edit is rate limited; spending one to say the same thing is waste."""
    board.fine(SERVER, ELI_ID, ELI, 2)
    scoreboard = _scoreboard(board, channel)

    await scoreboard.publish()
    await scoreboard.publish()

    assert len(channel.statuses) == 1


async def test_a_further_change_is_published(board, channel):
    scoreboard = _scoreboard(board, channel)
    board.fine(SERVER, ELI_ID, ELI, 1)
    await scoreboard.publish()

    board.fine(SERVER, ERIK_ID, ERIK, 1)
    await scoreboard.publish()

    assert channel.statuses == [f"{ELI}: -1 {ERIK}: 0", f"{ELI}: -1 {ERIK}: -1"]


async def test_several_changes_between_ticks_are_one_edit(board, channel):
    board.fine(SERVER, ELI_ID, ELI, 1)
    board.fine(SERVER, ELI_ID, ELI, 1)
    board.fine(SERVER, ERIK_ID, ERIK, 1)

    await _scoreboard(board, channel).publish()

    assert channel.statuses == [f"{ELI}: -2 {ERIK}: -1"]


async def test_nothing_is_published_when_the_bot_is_in_no_voice_channel(board):
    board.fine(SERVER, ELI_ID, ELI, 1)

    await _scoreboard(board).publish()  # Reaching this without raising is the test.


async def test_a_tally_missed_for_want_of_a_channel_is_published_later(board, channel):
    """Otherwise the topic waits for the next fine to catch up."""
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = Scoreboard(board, lambda server_id: FakeGuild(None), INTERVAL_SECONDS)
    await scoreboard.publish()

    scoreboard._guilds = lambda server_id: FakeGuild(FakeVoiceClient(channel))
    await scoreboard.publish()

    assert channel.statuses == [f"{ELI}: -1 {ERIK}: 0"]


async def test_nothing_is_published_while_the_bot_is_disconnected(board, channel):
    board.fine(SERVER, ELI_ID, ELI, 1)

    await _scoreboard(board, channel, connected=False).publish()

    assert channel.statuses == []


async def test_a_server_nobody_configured_is_not_published(ledger, channel):
    """The alias cannot be resolved to a guild, so there is nowhere to put it."""
    ledger.enroll(UNCONFIGURED_SERVER, ROSTER)
    ledger.fine(UNCONFIGURED_SERVER, ELI_ID, ELI, 1)

    await _scoreboard(ledger, channel).publish()

    assert channel.statuses == []


async def test_the_tally_is_set_as_the_status_and_not_the_topic(board, channel):
    """
    A voice channel has no topic.

    Discord refuses one with CHANNEL_TOPIC_INVALID, "Field contains at least one
    word that is not allowed", which reads like a profanity filter and is not
    one — it refuses a topic of "test" the same way. The status is the line the
    client shows under the channel name.
    """
    board.fine(SERVER, ELI_ID, ELI, 1)

    await _scoreboard(board, channel).publish()

    assert channel.edits == [{"status": f"{ELI}: -1 {ERIK}: 0"}]


async def test_a_forbidden_edit_is_not_retried(board, caplog):
    """The permission is not going to appear on its own, and retries cost the bucket."""
    channel = FakeChannel(failure=discord.Forbidden(_response(FORBIDDEN_STATUS).response, "no"))
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = _scoreboard(board, channel)

    with caplog.at_level("WARNING"):
        await scoreboard.publish()
        await scoreboard.publish()

    assert any("Set Voice Channel Status" in record.message for record in caplog.records)


async def test_a_rejected_status_is_not_retried(board, caplog):
    """
    A request Discord will not parse cannot come good on the next tick.

    Retrying one spends the channel's rate limit, every interval, for the life of
    the process.
    """
    channel = FakeChannel(failure=_response(REJECTED_STATUS))
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = _scoreboard(board, channel)

    with caplog.at_level("ERROR"):
        await scoreboard.publish()
        await scoreboard.publish()

    assert len(caplog.records) == 1


async def test_a_rejection_says_what_it_tried_to_set(board, caplog):
    """A rejection caused by a name in the tally cannot be diagnosed without it."""
    channel = FakeChannel(failure=_response(REJECTED_STATUS))
    board.fine(SERVER, ELI_ID, ELI, 1)

    with caplog.at_level("ERROR"):
        await _scoreboard(board, channel).publish()

    assert f"{ELI}: -1" in caplog.records[0].getMessage()


async def test_a_rejected_tally_is_published_once_it_changes(board, channel, caplog):
    """What was refused was that text; the next text is not that text."""
    channel.failure = _response(REJECTED_STATUS)
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = _scoreboard(board, channel)

    with caplog.at_level("ERROR"):
        await scoreboard.publish()

    channel.failure = None
    board.fine(SERVER, ERIK_ID, ERIK, 1)
    await scoreboard.publish()

    assert channel.statuses == [f"{ELI}: -1 {ERIK}: -1"]


async def test_a_failed_edit_is_tried_again(board, channel, caplog):
    channel.failure = _response(SERVER_ERROR_STATUS)
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = _scoreboard(board, channel)

    with caplog.at_level("WARNING"):
        await scoreboard.publish()

    channel.failure = None
    await scoreboard.publish()

    assert channel.statuses == [f"{ELI}: -1 {ERIK}: 0"]


# ── persisting ────────────────────────────────────


async def test_a_changed_tally_is_written_to_disk(ledger, path):
    ledger.fine(SERVER, ELI_ID, ELI, 2)

    await _scoreboard(ledger).persist()

    assert CreditLedger(path).total(SERVER, ELI_ID) == -2


async def test_an_unchanged_tally_is_not_written_again(ledger, path):
    scoreboard = _scoreboard(ledger)
    ledger.fine(SERVER, ELI_ID, ELI, 2)
    await scoreboard.persist()
    written = path.stat().st_mtime_ns

    await scoreboard.persist()

    assert path.stat().st_mtime_ns == written


async def test_nothing_is_written_for_a_tally_nobody_has_touched(ledger, path):
    await _scoreboard(ledger).persist()

    assert not path.exists()


async def test_a_fine_landing_during_a_write_is_not_marked_saved(ledger, path):
    """The revision is read before the write, so the next tick picks it up."""
    scoreboard = _scoreboard(ledger)
    ledger.fine(SERVER, ELI_ID, ELI, 1)

    await scoreboard.persist()
    ledger.fine(SERVER, ERIK_ID, ERIK, 1)
    await scoreboard.persist()

    assert CreditLedger(path).total(SERVER, ERIK_ID) == -1


# ── the topic's own interval ──────────────────────


def test_the_first_turn_comes_immediately(ledger):
    """A restart should not sit on the tally for the length of an interval."""
    scoreboard = _scoreboard(ledger, topic_seconds=TOPIC_SECONDS)

    assert scoreboard._topic_turn_has_come(now=NOW)


def test_a_turn_does_not_come_round_again_inside_the_interval(ledger):
    """The rate limit is the reason the interval exists; ticking past it is waste."""
    scoreboard = _scoreboard(ledger, topic_seconds=TOPIC_SECONDS)
    scoreboard._topic_turn_has_come(now=NOW)

    assert not scoreboard._topic_turn_has_come(now=NOW + TOPIC_SECONDS - 1)


def test_a_turn_comes_round_once_the_interval_has_passed(ledger):
    scoreboard = _scoreboard(ledger, topic_seconds=TOPIC_SECONDS)
    scoreboard._topic_turn_has_come(now=NOW)

    assert scoreboard._topic_turn_has_come(now=NOW + TOPIC_SECONDS)


def test_a_turn_never_comes_when_the_topic_is_switched_off(ledger):
    scoreboard = _scoreboard(ledger, topic_seconds=NO_TOPIC)

    assert not scoreboard._topic_turn_has_come(now=NOW)


async def test_a_tally_is_still_saved_with_the_topic_switched_off(board, channel, path):
    board.fine(SERVER, ELI_ID, ELI, 1)
    task = asyncio.create_task(_scoreboard(board, channel, topic_seconds=NO_TOPIC).run())

    async with asyncio.timeout(PATIENCE_SECONDS):
        while not path.is_file():
            await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert channel.statuses == []


# ── the loop ──────────────────────────────────────


async def test_the_loop_publishes_and_saves(board, channel, path):
    board.fine(SERVER, ELI_ID, ELI, 1)
    task = asyncio.create_task(_scoreboard(board, channel).run())

    async with asyncio.timeout(PATIENCE_SECONDS):
        while not channel.statuses or not path.is_file():
            await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert channel.statuses == [f"{ELI}: -1 {ERIK}: 0"]


async def test_a_failing_tick_does_not_stop_the_loop(board, channel, caplog):
    board.fine(SERVER, ELI_ID, ELI, 1)
    scoreboard = _scoreboard(board, channel)
    failures = []

    async def once() -> None:
        failures.append(True)
        raise RuntimeError("the ledger is on fire")

    scoreboard.persist = once
    task = asyncio.create_task(scoreboard.run())

    with caplog.at_level("ERROR"):
        async with asyncio.timeout(PATIENCE_SECONDS):
            while len(failures) < 2:
                await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    assert any("on fire" in record.message for record in caplog.records)


async def test_the_loop_stops_when_it_is_cancelled(ledger, channel):
    task = asyncio.create_task(_scoreboard(ledger, channel).run())
    await asyncio.sleep(INTERVAL_SECONDS)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── the bot ───────────────────────────────────────


@pytest.fixture
def stt_bot(monkeypatch, ledger):
    """
    An STTBot with the transcript, STT, and tool machinery stubbed out.

    The tools included, so what is on the board is only ever what a test put
    there rather than whatever the machine running the tests has mounted at
    /config.
    """
    monkeypatch.setattr(client_module, "TranscriptWriter", lambda: object())
    monkeypatch.setattr(client_module, "STTProcessor", lambda tools: object())
    monkeypatch.setattr(client_module, "ToolRunner", lambda speaker: object())
    monkeypatch.setattr(client_module, "shared_ledger", lambda: ledger)

    return client_module.STTBot()


async def test_the_bot_starts_publishing_when_something_is_counting(stt_bot, ledger):
    ledger.enroll(SERVER, {ELI_ID: ELI})

    stt_bot._start_scoreboard()

    assert stt_bot._tally in stt_bot._bot.background_tasks
    stt_bot._tally.cancel()


async def test_the_bot_does_not_publish_an_empty_ledger(stt_bot):
    """The ordinary case for a deployment that fines nobody."""
    stt_bot._start_scoreboard()

    assert stt_bot._tally is None


async def test_the_bot_starts_publishing_once(stt_bot, ledger):
    ledger.enroll(SERVER, {ELI_ID: ELI})
    stt_bot._start_scoreboard()
    started = stt_bot._tally

    stt_bot._start_scoreboard()

    assert stt_bot._tally is started
    stt_bot._tally.cancel()


async def test_shutting_down_writes_the_tally(stt_bot, ledger, path):
    """The interval task is cancelled by then; the file is what is left."""
    ledger.fine(SERVER, ELI_ID, ELI, 5)

    await stt_bot._shutdown()

    assert CreditLedger(path).total(SERVER, ELI_ID) == -5
