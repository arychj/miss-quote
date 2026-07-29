from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config import ServerConfig, ToolSettings
from tools.base import Tool
from tools.runner import ToolRunner
from transcript.writer import Source, Transcript, Utterance

FIRST_SERVER = 123456789012345678
SECOND_SERVER = 876543210987654321

FIRST_ALIAS = "first-server"
SECOND_ALIAS = "second-server"

TOOL_NAME = "recorder"
OTHER_TOOL_NAME = "second-recorder"

FIRST_SOURCE = Source(
    guild_id=FIRST_SERVER, guild_alias=FIRST_ALIAS, channel_id=1, channel="general-voice"
)
SECOND_SOURCE = Source(
    guild_id=SECOND_SERVER, guild_alias=SECOND_ALIAS, channel_id=2, channel="general-voice"
)


class Recorder(Tool):
    """A tool that handles both moments and remembers being called."""

    name = TOOL_NAME

    def __init__(self, server, config):
        super().__init__(server, config)
        self.utterances = []
        self.transcripts = []

    async def handle_utterance(self, utterance, session) -> None:
        self.utterances.append(utterance)

    async def handle_finished(self, transcript) -> None:
        self.transcripts.append(transcript)


class UtteranceOnly(Tool):
    name = "utterance-only"

    def __init__(self, server, config):
        super().__init__(server, config)
        self.calls = 0

    async def handle_utterance(self, utterance, session) -> None:
        self.calls += 1


class FinishedOnly(Tool):
    name = "finished-only"

    def __init__(self, server, config):
        super().__init__(server, config)
        self.calls = 0

    async def handle_finished(self, transcript) -> None:
        self.calls += 1


class Inert(Tool):
    """A tool that handles nothing. Configured, but it can never run."""

    name = "inert"


class Exploding(Tool):
    name = "exploding"

    async def handle_utterance(self, utterance, session) -> None:
        raise RuntimeError("no")

    async def handle_finished(self, transcript) -> None:
        raise RuntimeError("still no")


class Unbuildable(Tool):
    name = "unbuildable"

    def __init__(self, server, config):
        raise ValueError("missing something it needed")


class FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source


def _servers(**tools_by_server: dict[str, ToolSettings]) -> dict[int, ServerConfig]:
    """Build a servers mapping from `alias=<tools>` keyword arguments."""
    ids = {FIRST_ALIAS: FIRST_SERVER, SECOND_ALIAS: SECOND_SERVER}
    aliases = {"first": FIRST_ALIAS, "second": SECOND_ALIAS}

    return {
        ids[aliases[key]]: ServerConfig(alias=aliases[key], users={}, tools=tools)
        for key, tools in tools_by_server.items()
    }


def _enabled(config: dict | None = None) -> ToolSettings:
    return ToolSettings(enabled=True, config=config or {})


def _utterance(text: str = "something") -> Utterance:
    return Utterance(
        timestamp=datetime.now().astimezone(), user_id=1, user="someone", text=text
    )


def _transcript(source: Source, path: Path) -> Transcript:
    opened = datetime.now().astimezone()
    return Transcript(
        path=path,
        source=source,
        opened=opened,
        closed=opened + timedelta(minutes=5),
        utterances=0,
    )


def _only_tool(runner: ToolRunner, server_id: int, moment: str) -> Tool:
    bucket = getattr(runner, moment)
    return bucket[server_id][0]


# ── construction ──────────────────────────────────


def test_an_enabled_tool_is_built_with_its_config():
    config = {"some-setting": "a value"}
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled(config)}), {TOOL_NAME: Recorder}
    )

    tool = _only_tool(runner, FIRST_SERVER, "_on_utterance")

    assert tool.server == FIRST_ALIAS
    assert tool.config == config
    assert runner.problems == []


def test_a_disabled_tool_is_not_built():
    runner = ToolRunner(
        _servers(first={TOOL_NAME: ToolSettings(enabled=False, config={})}),
        {TOOL_NAME: Recorder},
    )

    assert runner.describe() == {}
    assert runner.problems == []


def test_a_tool_nobody_registered_is_reported_and_skipped():
    runner = ToolRunner(_servers(first={"nonexistent": _enabled()}), {})

    assert runner.describe() == {}
    assert any("nonexistent" in problem for problem in runner.problems)


def test_a_tool_that_will_not_start_is_reported_and_skipped():
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Unbuildable}
    )

    assert runner.describe() == {}
    assert any("missing something it needed" in problem for problem in runner.problems)


def test_a_tool_handling_neither_moment_is_reported():
    """Configured but inert is a mistake worth naming, not a silent no-op."""
    runner = ToolRunner(_servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Inert})

    assert runner.describe() == {}
    assert any("never run" in problem for problem in runner.problems)


def test_tools_are_sorted_by_the_moments_they_handle():
    registry = {"utterance-only": UtteranceOnly, "finished-only": FinishedOnly}
    runner = ToolRunner(
        _servers(first={"utterance-only": _enabled(), "finished-only": _enabled()}),
        registry,
    )

    assert [type(tool) for tool in runner._on_utterance[FIRST_SERVER]] == [UtteranceOnly]
    assert [type(tool) for tool in runner._on_finished[FIRST_SERVER]] == [FinishedOnly]
    assert runner.describe() == {FIRST_ALIAS: ("finished-only", "utterance-only")}


def test_each_server_gets_its_own_instance():
    """A tool holds per-server state, so two servers must not share one."""
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}, second={TOOL_NAME: _enabled()}),
        {TOOL_NAME: Recorder},
    )

    first = _only_tool(runner, FIRST_SERVER, "_on_utterance")
    second = _only_tool(runner, SECOND_SERVER, "_on_utterance")

    assert first is not second
    assert first.server == FIRST_ALIAS
    assert second.server == SECOND_ALIAS


# ── dispatch ──────────────────────────────────────


async def test_an_utterance_reaches_its_servers_tool():
    runner = ToolRunner(_servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Recorder})
    tool = _only_tool(runner, FIRST_SERVER, "_on_utterance")

    await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance("hello"))

    assert [utterance.text for utterance in tool.utterances] == ["hello"]


async def test_a_finished_transcript_reaches_its_servers_tool(tmp_path):
    runner = ToolRunner(_servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Recorder})
    tool = _only_tool(runner, FIRST_SERVER, "_on_finished")
    transcript = _transcript(FIRST_SOURCE, tmp_path / "session.jsonl")

    await runner.dispatch_finished(transcript)

    assert tool.transcripts == [transcript]


async def test_one_servers_tool_never_sees_anothers_transcript(tmp_path):
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}, second={TOOL_NAME: _enabled()}),
        {TOOL_NAME: Recorder},
    )
    first = _only_tool(runner, FIRST_SERVER, "_on_utterance")
    second = _only_tool(runner, SECOND_SERVER, "_on_utterance")

    await runner.dispatch_utterance(FakeSession(SECOND_SOURCE), _utterance())
    await runner.dispatch_finished(_transcript(SECOND_SOURCE, tmp_path / "s.jsonl"))

    assert first.utterances == []
    assert first.transcripts == []
    assert len(second.utterances) == 1


async def test_a_server_with_no_tools_dispatches_nothing(tmp_path):
    runner = ToolRunner({}, {TOOL_NAME: Recorder})

    await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance())
    await runner.dispatch_finished(_transcript(FIRST_SOURCE, tmp_path / "s.jsonl"))


# ── failure isolation ─────────────────────────────


async def test_a_raising_tool_does_not_stop_the_others(tmp_path, caplog):
    registry = {TOOL_NAME: Recorder, OTHER_TOOL_NAME: Exploding}
    runner = ToolRunner(
        _servers(first={OTHER_TOOL_NAME: _enabled(), TOOL_NAME: _enabled()}), registry
    )
    recorder = next(
        tool
        for tool in runner._on_utterance[FIRST_SERVER]
        if isinstance(tool, Recorder)
    )

    await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance("survives"))
    await runner.dispatch_finished(_transcript(FIRST_SOURCE, tmp_path / "s.jsonl"))

    assert [utterance.text for utterance in recorder.utterances] == ["survives"]
    assert len(recorder.transcripts) == 1


async def test_a_raising_tool_is_logged(caplog):
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Exploding}
    )

    with caplog.at_level("ERROR"):
        await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance())

    assert any("exploding" in record.message.lower() for record in caplog.records)


async def test_a_raising_tool_does_not_reach_the_caller():
    """Nothing a tool does may cost an utterance."""
    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Exploding}
    )

    await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance())


async def test_cancellation_is_not_swallowed():
    """Shutdown must not be mistaken for a tool failing."""
    import asyncio

    class Cancelling(Tool):
        name = "cancelling"

        async def handle_utterance(self, utterance, session) -> None:
            raise asyncio.CancelledError()

    runner = ToolRunner(
        _servers(first={TOOL_NAME: _enabled()}), {TOOL_NAME: Cancelling}
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.dispatch_utterance(FakeSession(FIRST_SOURCE), _utterance())
